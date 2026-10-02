"""validate 단계: 검증 → review.json (사람 처리결과는 병합 보존).

compute 단계가 남긴 compute_issues.json(법 파라미터 누락 등)도 함께 review.json 에 싣는다.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..context import RunContext, StageResult
from ..models import Line, ReviewItem, Severity, TaxPeriod, VatReturn
from ..period import load_holidays
from ..registry import load_filing_settings
from .checks import CheckInput, run_checks

COMPUTE_ISSUES = "compute_issues.json"
COMPUTE_META = "compute_meta.json"


def previous_period_code(code: str) -> str:
    p = TaxPeriod.parse(code)
    if p.kind == "F":
        return TaxPeriod(p.year, p.half, "P").code
    if p.half == 2:
        return TaxPeriod(p.year, 1, "F").code
    return TaxPeriod(p.year - 1, 2, "F").code


def _load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def find_previous_sales(data_dir: Path, client_id: str, period_code: str, lookback: int = 2) -> tuple[int | None, int | None]:
    """직전 회차부터 거슬러 올라가 return.json 이 있는 첫 회차의 (9번 금액, 집계개월수)."""
    code = period_code
    for _ in range(lookback):
        code = previous_period_code(code)
        root = data_dir / code / client_id
        d = _load_json(root / "return.json")
        if d:
            r = VatReturn.from_dict(d)
            meta = _load_json(root / COMPUTE_META) or {}
            return r.line(Line.S_TOTAL).amount, meta.get("months")
    return None, None


def run(ctx: RunContext) -> StageResult:
    ws = ctx.workspace
    settings = load_filing_settings(ctx.clients_dir, ctx.client.id, ctx.period_code)
    prev_sales, prev_months = settings.previous_period_sales, None
    if prev_sales is None:
        prev_sales, prev_months = find_previous_sales(ws.root.parent.parent, ctx.client.id, ctx.period_code)
    raw_files = [str(p.name) for p in ws.raw_dir.glob("*") if p.is_file()] if ws.raw_dir.exists() else []
    if ctx.inbox_dir and Path(ctx.inbox_dir).exists():
        raw_files += [str(p.name) for p in Path(ctx.inbox_dir).glob("*") if p.is_file()]
    try:
        holidays = load_holidays(ctx.config_dir)
    except Exception:
        holidays = set()
    ci = CheckInput(
        client=ctx.client,
        filing=ctx.filing,
        txns=ws.load_transactions(),
        law=ctx.law,
        policy=ctx.policy,
        ret=ws.load_return(),
        settings=settings,
        parse_issues=ws.load_parse_issues(),
        raw_files=raw_files,
        wehago_return=ws.load_return("wehago_return.json"),
        previous_sales=prev_sales,
        previous_months=prev_months,
        compute_meta=ws.load_json(COMPUTE_META, {}) or {},
        holidays=holidays,
    )
    items = [ReviewItem.from_dict(d) for d in (ws.load_json(COMPUTE_ISSUES, []) or [])]
    items += run_checks(ci)
    # 같은 id 중복 제거(결정적 순서 유지)
    seen, uniq = set(), []
    for it in items:
        if it.id not in seen:
            seen.add(it.id)
            uniq.append(it)
    if not ctx.dry_run:
        uniq = ws.save_review(uniq, merge=True)
    counts = {s.name.lower(): sum(1 for i in uniq if i.severity == s) for s in Severity}
    counts["open"] = sum(1 for i in uniq if i.status.value == "미해결")
    return StageResult(
        ok=True,
        message=f"검토항목 {len(uniq)}건(차단 {counts['blocker']}, 경고 {counts['warn']}, 참고 {counts['info']})",
        counts=counts,
    )

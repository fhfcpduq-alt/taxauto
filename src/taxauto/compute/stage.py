"""compute 단계: 거래 → 신고서(return.json) 독립 재계산.

부산물
  compute_issues.json : 계산 중 생긴 검토항목(법 파라미터 누락 등) - validate 단계가 review.json 에 합친다
  compute_meta.json   : 집계기간·사용 법 파라미터·제외 거래 id 등 - validate 단계가 참조
"""

from __future__ import annotations

from ..context import RunContext, StageResult
from ..models import Line, TaxPeriod, Transaction
from ..registry import load_filing_settings
from .vat_return import compute_return_detailed

COMPUTE_ISSUES = "compute_issues.json"
COMPUTE_META = "compute_meta.json"


def _prior_reported_ids(ctx: RunContext) -> set[str]:
    """예정신고를 한 확정 회차: 같은 기 예정 회차 작업공간의 거래 id(이미 예정신고에 포함된 것)."""
    p = ctx.filing.period
    if p.kind != "F" or not ctx.filing.filed_preliminary:
        return set()
    prelim_root = ctx.workspace.root.parent.parent / TaxPeriod(p.year, p.half, "P").code / ctx.client.id
    f = prelim_root / "transactions.json"
    if not f.exists():
        return set()
    import json

    try:
        return {Transaction.from_dict(d).id for d in json.loads(f.read_text(encoding="utf-8"))}
    except Exception:  # 손상 파일은 무시(검증 V020 이 누락분을 보여 줌)
        return set()


def run(ctx: RunContext) -> StageResult:
    txns = ctx.workspace.load_transactions()
    settings = load_filing_settings(ctx.clients_dir, ctx.client.id, ctx.period_code)
    res = compute_return_detailed(
        txns, ctx.client, ctx.filing, ctx.law, ctx.policy, settings, prior_reported_ids=_prior_reported_ids(ctx)
    )
    if not ctx.dry_run:
        ctx.workspace.save_return(res.ret)
        ctx.workspace.save_json(COMPUTE_ISSUES, [i.to_dict() for i in res.issues])
        ctx.workspace.save_json(COMPUTE_META, res.meta)
    final = res.ret.line(Line.FINAL).tax
    return StageResult(
        ok=True,
        message=f"27번 {'환급' if final < 0 else '납부'}세액 {abs(final):,}원",
        counts={
            "sales_tax": res.ret.line(Line.S_TOTAL).tax,
            "purchase_tax": res.ret.line(Line.P_NET).tax,
            "final_tax": final,
            "issues": len(res.issues),
        },
    )

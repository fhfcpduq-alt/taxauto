"""사람·에이전트 공용 작업 함수 (CLI 와 MCP 서버가 같이 쓴다).

여기 있는 함수는 '검토 처리·재분류·자료 투입·작업기록'까지만 한다.
신고서 제출·데이터 삭제 기능은 의도적으로 없다.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import date
from pathlib import Path
from typing import Any

from .models import (
    Classification,
    DecidedBy,
    Direction,
    ExclusionReason,
    Filing,
    NonDeductibleReason,
    PurchaseCategory,
    ReviewItem,
    ReviewStatus,
    TaxPeriod,
    Transaction,
)
from .pipeline import (
    Paths,
    StageFn,
    lock_for,
    make_context,
    now_iso,
    run_client,
    write_summary,
)
from .redact import redact
from .registry import load_clients

AGENT_NAMES = {"", "agent", "llm", "ai", "claude", "auto"}
INGEST_EXTS = {".xlsx", ".xls", ".csv", ".json", ".zip", ".txt"}


class OpError(ValueError):
    """사용자/에이전트에게 그대로 보여줄 오류."""


def current_user() -> str:
    return os.environ.get("TAXAUTO_USER") or os.environ.get("USERNAME") or os.environ.get("USER") or "human"


# ---------------------------------------------------------------------------
# 조회
# ---------------------------------------------------------------------------


def find_client(paths: Paths, client_id: str):
    for c in load_clients(paths.clients_dir):
        if c.id == str(client_id):
            return c
    raise OpError(f"명부에 없는 거래처: {client_id}")


def load_filing(paths: Paths, client, period_code: str) -> Filing:
    ws = paths.workspace(period_code, client.id)
    d = ws.load_json("filing.json")
    if d:
        return Filing.from_dict(d)
    return make_context(client, period_code, paths=paths).filing


def parse_status(s: str) -> ReviewStatus:
    s = (s or "").strip()
    for st in ReviewStatus:
        if s in (st.value, st.name, st.name.lower()):
            return st
    raise OpError(f"상태값은 {', '.join(x.value for x in ReviewStatus)} 중 하나: {s!r}")


def client_ids_in_period(paths: Paths, period_code: str) -> list[str]:
    pdir = paths.period_dir(period_code)
    if not pdir.exists():
        return []
    return sorted(p.name for p in pdir.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))


def find_review_item(paths: Paths, period_code: str, item_id: str, client_id: str | None = None) -> tuple[str, list[ReviewItem], ReviewItem]:
    """id(앞부분 일치 허용, 4자 이상) → (client_id, 전체 항목, 대상 항목)."""
    item_id = (item_id or "").strip()
    if len(item_id) < 4:
        raise OpError("검토항목 id 는 4자 이상 입력")
    cids = [str(client_id)] if client_id else client_ids_in_period(paths, period_code)
    hits: list[tuple[str, list[ReviewItem], ReviewItem]] = []
    for cid in cids:
        items = paths.workspace(period_code, cid).load_review()
        for it in items:
            if it.id == item_id or it.id.startswith(item_id):
                hits.append((cid, items, it))
    if not hits:
        raise OpError(f"검토항목을 찾을 수 없음: {item_id} ({period_code})")
    if len(hits) > 1:
        raise OpError(f"id 가 여러 항목과 일치: {', '.join(h[2].id for h in hits)} — 더 길게 입력하거나 거래처 지정")
    return hits[0]


# ---------------------------------------------------------------------------
# 리포트 갱신
# ---------------------------------------------------------------------------


def refresh_reports(paths: Paths, period_code: str, client_id: str | None = None, today: date | None = None) -> dict:
    """사람 결정 반영 후 거래처 리포트 + 회차 요약 재작성(계산은 다시 하지 않음)."""
    if client_id:
        try:
            from .report.stage import write_client_reports

            client = find_client(paths, client_id)
            ws = paths.workspace(period_code, client_id)
            from .law import load_policy

            office = str((load_policy(paths.config_dir).get("office") or {}).get("name") or "")
            write_client_reports(ws, client, load_filing(paths, client, period_code), ws.load_return(),
                                 ws.load_review(), ws.load_transactions(), today or date.today(), office_name=office)
        except OpError:
            raise
        except Exception:  # 리포트 실패가 결정 저장을 막으면 안 됨
            import logging

            logging.getLogger("taxauto").exception("리포트 갱신 실패 %s %s", period_code, client_id)
    return write_summary(period_code, paths=paths, today=today)


# ---------------------------------------------------------------------------
# 메모리(학습) — classify 섹터 모듈 지연 import
# ---------------------------------------------------------------------------


def remember_decision(paths: Paths, client_id: str, tx: Transaction, cls: Classification, scope: str, by: str) -> dict:
    try:
        from .classify.memory import add_decision
    except ModuleNotFoundError as e:
        raise OpError("classify.memory 모듈이 없어 메모리에 저장할 수 없음") from e
    return add_decision(paths.clients_dir / client_id, tx, cls, scope=scope, by=by)


# ---------------------------------------------------------------------------
# 검토항목 처리
# ---------------------------------------------------------------------------


def resolve_review_item(
    paths: Paths,
    period_code: str,
    item_id: str,
    status: str,
    note: str,
    *,
    client_id: str | None = None,
    resolved_by: str | None = None,
    remember: bool = False,
    today: date | None = None,
) -> dict:
    st = parse_status(status)
    note = (note or "").strip()
    if st != ReviewStatus.OPEN and not note:
        raise OpError("처리 메모(note)는 필수입니다(감사 추적).")
    by = (resolved_by or current_user()).strip()
    if remember and by.lower() in AGENT_NAMES:
        raise OpError("메모리 저장(remember)은 사람이 승인한 경우에만 — resolved_by 에 승인자 이름을 넣으세요.")
    cid, items, item = find_review_item(paths, period_code, item_id, client_id)
    item.status = st
    item.resolution = redact(note) if st != ReviewStatus.OPEN else ""
    item.resolved_by = by if st != ReviewStatus.OPEN else ""
    ws = paths.workspace(period_code, cid)
    ws.save_review(items, merge=False)

    remembered: list[dict] = []
    if remember:
        txs = {t.id: t for t in ws.load_transactions()}
        for tid in item.tx_ids:
            t = txs.get(tid)
            if t is None or t.classification is None:
                continue
            remembered.append(remember_decision(paths, cid, t, t.classification, "counterparty", by))
    refresh_reports(paths, period_code, cid, today)
    return {"client_id": cid, "item": item.to_dict(), "remembered": len(remembered)}


# ---------------------------------------------------------------------------
# 거래 재분류
# ---------------------------------------------------------------------------


def _enum_lookup(enum_cls, value: str):
    v = (value or "").strip()
    for e in enum_cls:
        if v in (e.value, e.name):
            return e
    return None


def build_classification(category: str, reason: str = "", note: str = "", decided_by: str = "llm",
                         confidence: float | None = None) -> Classification:
    cat = _enum_lookup(PurchaseCategory, category)
    if cat is None:
        raise OpError(f"분류는 {', '.join(c.value for c in PurchaseCategory)} 중 하나: {category!r}")
    ndr = exr = None
    free = note or ""
    if cat == PurchaseCategory.NON_DEDUCTIBLE:
        ndr = _enum_lookup(NonDeductibleReason, reason)
        if ndr is None:
            raise OpError(f"불공제 사유는 {', '.join(r.value for r in NonDeductibleReason)} 중 하나: {reason!r}")
    elif cat == PurchaseCategory.NOT_APPLICABLE:
        exr = _enum_lookup(ExclusionReason, reason)
        if exr is None:
            raise OpError(f"신고제외 사유는 {', '.join(r.value for r in ExclusionReason)} 중 하나: {reason!r}")
    elif reason:
        free = f"{reason} {free}".strip()
    human = (decided_by or "").lower() not in AGENT_NAMES
    return Classification(
        category=cat,
        non_deductible_reason=ndr,
        exclusion_reason=exr,
        decided_by=DecidedBy.HUMAN if human else DecidedBy.LLM,
        rule_id="manual:reclassify",
        confidence=1.0 if human else float(0.5 if confidence is None else confidence),
        needs_review=not human,
        note=redact(free)[:300] + ("" if human else f" (판정: {decided_by or 'agent'})"),
    )


def reclassify_transaction(
    paths: Paths,
    period_code: str,
    client_id: str,
    tx_id: str,
    category: str,
    reason: str = "",
    note: str = "",
    *,
    decided_by: str = "agent",
    confidence: float | None = None,
    remember: bool = False,
    rerun: bool = True,
    stage_fns: dict[str, StageFn] | None = None,
    today: date | None = None,
) -> dict:
    """매입 1건 분류 변경 → (사람 결정이면 거래 단위 메모리 기록) → compute·validate·report 재실행."""
    client = find_client(paths, client_id)
    ws = paths.workspace(period_code, client.id)
    txns = ws.load_transactions()
    hits = [t for t in txns if t.id == tx_id or (len(tx_id) >= 6 and t.id.startswith(tx_id))]
    if len(hits) != 1:
        raise OpError(f"거래를 찾을 수 없거나 여러 건 일치: {tx_id}")
    tx = hits[0]
    if tx.direction != Direction.PURCHASE:
        raise OpError("분류 변경은 매입 거래만 가능합니다.")
    human = (decided_by or "").lower() not in AGENT_NAMES
    if remember and not human:
        raise OpError("메모리 저장(remember)은 사람 결정만 — decided_by 에 결정한 사람 이름을 넣으세요.")
    before = _cls_dict(tx.classification)
    tx.classification = build_classification(category, reason, note, decided_by, confidence)
    ws.save_transactions(txns)

    memory_entry = None
    if human:
        # 사람 결정은 다음 야간 실행(classify 재실행)에도 유지되게 거래 단위로 기록, remember 면 거래처 단위
        try:
            memory_entry = remember_decision(paths, client.id, tx, tx.classification,
                                             "counterparty" if remember else "once", decided_by)
        except (OpError, ValueError) as e:
            if remember:
                raise OpError(f"메모리 저장 실패: {e}") from e

    _audit(ws.root, {"action": "reclassify", "tx_id": tx.id, "before": before,
                     "after": _cls_dict(tx.classification), "by": decided_by, "remember": remember})
    result: dict[str, Any] = {"client_id": client.id, "tx_id": tx.id, "before": before,
                              "after": _cls_dict(tx.classification), "memory": memory_entry}
    if rerun:
        result["rerun"] = rerun_from(paths, period_code, client.id, "compute", stage_fns=stage_fns, today=today)
    else:
        refresh_reports(paths, period_code, client.id, today)
    return result


def _cls_dict(c: Classification | None) -> dict | None:
    if c is None:
        return None
    from .models import _ser

    return _ser(c)


def _audit(root: Path, rec: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    rec = {"ts": now_iso(), **rec}
    with (root / "decisions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 재실행
# ---------------------------------------------------------------------------


def rerun_from(paths: Paths, period_code: str, client_id: str, from_stage: str | None = None,
               stages: list[str] | None = None, stage_fns: dict[str, StageFn] | None = None,
               today: date | None = None) -> dict:
    client = find_client(paths, client_id)
    with lock_for(paths, f"rerun {client_id} {from_stage or ''}".strip()):
        ctx = make_context(client, period_code, paths=paths, today=today)
        state = run_client(ctx, stages, from_stage, stage_fns)
        summary = write_summary(period_code, paths=paths, today=today)
    row = next((r for r in summary.get("clients", []) if r["client_id"] == client_id), None)
    return {"status": state.get("status"), "failed_stage": state.get("failed_stage"),
            "error": state.get("error"), "summary": row}


# ---------------------------------------------------------------------------
# 에이전트 연동 (위하고 조작 에이전트 ↔ 엔진)
# ---------------------------------------------------------------------------


def _sha1(p: Path) -> str:
    h = hashlib.sha1()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest_file(paths: Paths, period_code: str, client_id: str, path: str | Path, *, rerun: bool = True,
                stage_fns: dict[str, StageFn] | None = None, today: date | None = None) -> dict:
    """에이전트가 내려받은 파일 → inbox/{period}/{client_id}/ 복사 → 전체 단계 재실행."""
    TaxPeriod.parse(period_code)
    client = find_client(paths, client_id)
    src = Path(path).expanduser()
    if not src.is_file():
        raise OpError(f"파일이 없음: {src}")
    if src.suffix.lower() not in INGEST_EXTS:
        raise OpError(f"허용 확장자: {', '.join(sorted(INGEST_EXTS))}")
    dest_dir = paths.inbox_root / period_code / client.id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    copied = True
    if dest.exists():
        if _sha1(dest) == _sha1(src):
            copied = False  # 같은 파일 재투입
        else:
            stem, n = dest.stem, 1
            while dest.exists():
                dest = dest_dir / f"{stem}_{n}{src.suffix}"
                n += 1
    if copied:
        shutil.copy2(src, dest)
    out: dict[str, Any] = {"client_id": client.id, "inbox_path": str(dest), "copied": copied}
    if rerun:
        out["rerun"] = rerun_from(paths, period_code, client.id, None, stage_fns=stage_fns, today=today)
    return out


def agent_log_path(paths: Paths, period_code: str, client_id: str) -> Path:
    return paths.workspace(period_code, client_id).root / "wehago" / "agent_log.jsonl"


def record_agent_step(paths: Paths, period_code: str, client_id: str, step: str, status: str, note: str = "",
                      evidence_path: str | None = None, by: str = "agent") -> dict:
    find_client(paths, client_id)
    if not (step or "").strip() or not (status or "").strip():
        raise OpError("step, status 는 필수")
    rec = {"ts": now_iso(), "step": step.strip(), "status": status.strip(), "note": redact(note)[:1000],
           "evidence_path": evidence_path or None, "by": by}
    p = agent_log_path(paths, period_code, client_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    write_summary(period_code, paths=paths)
    return rec


def get_work_order(paths: Paths, period_code: str, client_id: str) -> dict:
    """taxauto.wehago.work_order.build_work_order 지연 import. 없으면 status=not_implemented."""
    client = find_client(paths, client_id)
    try:
        from .wehago.work_order import build_work_order  # type: ignore
    except ModuleNotFoundError as e:
        if (e.name or "").startswith("taxauto.wehago"):
            return {"status": "not_implemented", "message": "taxauto.wehago.work_order 모듈 없음"}
        raise
    import inspect

    ctx = make_context(client, period_code, paths=paths)
    params = list(inspect.signature(build_work_order).parameters.values())
    first = params[0] if params else None
    wants_ctx = first is not None and (first.name == "ctx" or "RunContext" in str(first.annotation))
    wo = build_work_order(ctx if wants_ctx else ctx.workspace.root)
    if hasattr(wo, "to_dict"):
        wo = wo.to_dict()
    return {"status": "ok", "work_order": wo}

"""위하고 작업지시서(work_order.json) 생성.

엔진 결과(data/{period}/{client}/ 의 filing.json·transactions.json·review.json·return.json)를 읽어
'위하고 화면에서 무엇을 어떻게 고치고, 신고서가 어떤 값이 나와야 하는지'를 에이전트용 JSON 으로 만든다.

  - 금액·세액은 전부 엔진 값. 에이전트는 계산하지 않고 이 값만 입력/대조한다.
  - 미해결 차단(BLOCKER) 항목이 있으면 blocked=true → 에이전트는 신고서 작성(60_vat_return) 단계로 가지 않는다.
  - 검토 중인 거래(미해결 검토항목에 걸린 거래)의 수정은 requires_human=true → 에이전트가 반영하지 않는다.
  - 위하고 전표 스타일(계정·유형·적요·분개)은 Classification 의 스타일 필드에서 가져온다.
    비어 있으면 '위하고 기본값 유지'(keep_default 에 필드명 표시).

저장: data/{period}/{client}/wehago/work_order.json
CLI : python -m taxauto.wehago.work_order --client C001 --period 2026-2P [--base-dir .]
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import (
    Client,
    DocType,
    Direction,
    Line,
    NonDeductibleReason,
    PurchaseCategory,
    ReviewStatus,
    Severity,
    Source,
    TaxpayerType,
    Transaction,
)
from ..workspace import Workspace, _dump

SCHEMA = "taxauto.wehago.work_order/v1"

# 신고서 칸 이름(사람·에이전트용). 번호는 Line 값(서식 개정 시 대조 필요)
LINE_LABELS: dict[str, str] = {
    "S_TAX_INVOICE": "과세 세금계산서 발급분",
    "S_BUYER_ISSUED": "과세 매입자발행 세금계산서",
    "S_CARD_CASH": "과세 신용카드·현금영수증 발행분",
    "S_OTHER": "과세 기타(정규영수증 외 매출분)",
    "S_ZR_TAX_INVOICE": "영세율 세금계산서 발급분",
    "S_ZR_OTHER": "영세율 기타",
    "S_PRELIM_OMITTED": "예정신고 누락분(매출)",
    "S_BAD_DEBT": "대손세액 가감",
    "S_TOTAL": "과세표준·매출세액 합계",
    "P_TI_GENERAL": "세금계산서 수취분 일반매입",
    "P_TI_EXPORT_DEFER": "수출기업 수입분 납부유예",
    "P_TI_FIXED": "세금계산서 수취분 고정자산 매입",
    "P_PRELIM_OMITTED": "예정신고 누락분(매입)",
    "P_BUYER_ISSUED": "매입자발행 세금계산서",
    "P_OTHER_DEDUCTIBLE": "그 밖의 공제매입세액",
    "P_TOTAL": "매입세액 합계",
    "P_NON_DEDUCTIBLE": "공제받지 못할 매입세액",
    "P_NET": "매입세액 차감계",
    "PAYABLE": "납부(환급)세액",
    "C_OTHER": "그 밖의 경감·공제세액",
    "C_CARD_ISSUE": "신용카드매출전표등 발행공제 등",
    "C_TOTAL": "경감·공제세액 합계",
    "C_SMALL_BIZ": "소규모 개인사업자 감면세액",
    "PRELIM_UNREFUNDED": "예정신고 미환급세액",
    "PRELIM_NOTICE": "예정고지세액",
    "PROXY_TRANSFEREE": "사업양수자 대리납부",
    "PROXY_BUYER": "매입자 납부특례",
    "PROXY_CARD": "신용카드업자 대리납부",
    "PENALTY": "가산세액계",
    "FINAL": "차가감 납부할 세액(환급받을 세액)",
}

# 공제받지 못할 매입세액 명세서 사유 → 서식상 순번(참고·추정. 위하고 사유코드는 학습모드에서 확정)
NON_DEDUCTIBLE_FORM_ROW: dict[str, int] = {
    NonDeductibleReason.MISSING_INFO.value: 1,
    NonDeductibleReason.UNRELATED.value: 2,
    NonDeductibleReason.PASSENGER_CAR.value: 3,
    NonDeductibleReason.ENTERTAINMENT.value: 4,
    NonDeductibleReason.TAX_FREE_BIZ.value: 5,
    NonDeductibleReason.LAND.value: 6,
    NonDeductibleReason.PRE_REGISTRATION.value: 7,
}

STYLE_FIELDS = ("account_code", "account_name", "entry_type", "settlement", "summary_text")

PROCEDURES = [
    "00_login", "10_switch_company", "20_collect_hometax", "30_auto_journal", "40_export_ledger",
    "50_apply_work_order", "60_vat_return", "70_export_return", "80_efile_prepare",
]

_LEDGER_TYPE_KEYS = ("유형", "부가세유형", "과세유형", "매입매출유형", "전표유형")


def _sha1(p: Path) -> str | None:
    if not p.exists():
        return None
    return hashlib.sha1(p.read_bytes()).hexdigest()[:16]


def _biz_dash(d: str) -> str:
    return f"{d[:3]}-{d[3:5]}-{d[5:]}" if len(d) == 10 else d


def _find_client(ws_root: Path, client_id: str, clients_dir: Path | None) -> Client | None:
    """거래처 정보: 인자 → workspace/client.json → 상위 폴더의 clients/clients.yaml."""
    p = ws_root / "client.json"
    if p.exists():
        try:
            return Client.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    from ..registry import load_clients

    dirs = [clients_dir] if clients_dir else [q / "clients" for q in ws_root.parents]
    for d in dirs:
        if d and (d / "clients.yaml").exists():
            for c in load_clients(d):
                if c.id == client_id:
                    return c
            break
    return None


def _load_limits(ws_root: Path) -> dict:
    import yaml

    for q in [*ws_root.parents, Path(__file__).resolve().parents[3]]:
        p = q / "config" / "wehago" / "limits.yaml"
        if p.exists():
            try:
                return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                return {}
    return {}


def _tx_key(t: Transaction) -> dict:
    """위하고 전표를 찾기 위한 키(화면 검색·엑셀 대조용)."""
    return {
        "date": t.tx_date.isoformat(),
        "counterparty_name": t.counterparty_name,
        "counterparty_biz_no": t.counterparty_biz_no,
        "counterparty_biz_no_dash": _biz_dash(t.counterparty_biz_no),
        "supply_amount": t.supply_amount,
        "vat": t.vat,
        "total": t.total,
        "approval_no": t.approval_no,
        "direction": t.direction.value,
        "doc_type": t.doc_type.value,
        "source": t.source.value,
        "card_kind": t.card_kind.value if t.card_kind else None,
        "card_no_masked": t.card_no_masked or None,
        "item": t.item or None,
    }


def _match_keys(t: Transaction) -> list[tuple]:
    keys = []
    if t.approval_no:
        keys.append(("A", t.approval_no.replace("-", "")))
    keys.append(("K", t.tx_date.isoformat(), t.counterparty_biz_no, t.supply_amount, t.vat))
    return keys


def _ledger_type(t: Transaction) -> str:
    raw = t.raw or {}
    for k in _LEDGER_TYPE_KEYS:
        if raw.get(k):
            return str(raw[k])
    return ""


def _action_for(t: Transaction) -> tuple[str | None, dict]:
    """엔진 판정 → (위하고 수정 동작, 바꿀 값). 바꿀 게 없으면 (None, {})."""
    c = t.classification
    if c is None or t.source == Source.WEHAGO_LEDGER:
        return None, {}
    is_card = t.doc_type in (DocType.CARD, DocType.CASH_RECEIPT)
    st: dict[str, Any] = {}
    if t.direction == Direction.PURCHASE:
        cat = c.category
        if cat == PurchaseCategory.NON_DEDUCTIBLE:
            reason = c.non_deductible_reason.value if c.non_deductible_reason else None
            if is_card:
                return "exclude_card_deduction", {"card_deduction": False, "exclude_reason": reason}
            return "set_non_deductible", {
                "deduction": "불공제",
                "non_deductible_reason": reason,
                "non_deductible_form_row": NON_DEDUCTIBLE_FORM_ROW.get(reason or ""),
                "wehago_reason_code": None,  # config/wehago/upload_template.yaml non_deductible_reason_codes (학습모드 확정)
            }
        if cat == PurchaseCategory.FIXED_ASSET:
            st = {"fixed_asset": True, "asset_kind": None}  # 건물·구축물/기계장치/차량운반구/기타 — 사람/학습 확인
            return "mark_fixed_asset", st
        if cat == PurchaseCategory.NOT_APPLICABLE:
            reason = c.exclusion_reason.value if c.exclusion_reason else None
            if is_card:
                return "exclude_card_deduction", {"card_deduction": False, "exclude_reason": reason}
            return "exclude_from_return", {"exclude_reason": reason}
        if cat == PurchaseCategory.DEEMED_INPUT:
            return "deemed_input_candidate", {"deemed_input": True}
    if any(getattr(c, f, "") for f in STYLE_FIELDS):
        return "set_style", {}
    return None, {}


def _style(t: Transaction) -> tuple[dict, list[str], str]:
    c = t.classification
    vals: dict[str, str] = {}
    keep: list[str] = []
    for f in STYLE_FIELDS:
        v = str(getattr(c, f, "") or "") if c else ""
        if v:
            vals[f] = v
        else:
            keep.append(f)
    return vals, keep, (getattr(c, "style_source", "") or "") if c else ""


def build_work_order(
    workspace_root: Path | str,
    *,
    client: Client | None = None,
    clients_dir: Path | None = None,
    limits: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """엔진 결과 → 위하고 작업지시서 dict (JSON 직렬화 가능)."""
    root = Path(workspace_root)
    ws = Workspace(root)
    filing = ws.load_json("filing.json") or {}
    state = ws.load_state() or {}
    client_id = str(filing.get("client_id") or state.get("client_id") or root.name)
    period = str(filing.get("period") or state.get("period") or root.parent.name)
    client = client or _find_client(root, client_id, clients_dir)
    limits = limits if limits is not None else _load_limits(root)
    agent_cfg = limits.get("agent") or {}
    allow_delete = bool(agent_cfg.get("allow_voucher_delete", False))

    txns = ws.load_transactions()
    items = ws.load_review()
    ret = ws.load_json("return.json")

    block_reasons: list[dict] = []
    warnings: list[dict] = []
    open_by_tx: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        if it.status != ReviewStatus.OPEN:
            continue
        brief = {"id": it.id, "code": it.code, "severity": it.severity.value, "title": it.title,
                 "tax_impact": it.tax_impact, "suggested_action": it.suggested_action}
        if it.severity == Severity.BLOCKER:
            block_reasons.append({**brief, "source": "review"})
        elif it.severity == Severity.WARN:
            warnings.append(brief)
        if it.severity in (Severity.BLOCKER, Severity.WARN):
            for tid in it.tx_ids:
                open_by_tx[tid].append(brief)
    if ret is None:
        block_reasons.append({"code": "ENGINE_NO_RETURN", "title": "엔진 재계산 신고서(return.json) 없음 — 파이프라인 먼저 실행", "source": "engine"})
    if state.get("status") and state.get("status") != "ok":
        block_reasons.append({"code": "ENGINE_NOT_OK", "source": "engine",
                              "title": f"엔진 실행 상태 {state.get('status')} (실패 단계: {state.get('failed_stage')})"})
    if client is None:
        block_reasons.append({"code": "CLIENT_UNKNOWN", "title": "거래처 명부에서 회사 정보를 찾지 못함(회사 전환 불가)", "source": "client"})
    elif client.taxpayer_type == TaxpayerType.SIMPLE:
        block_reasons.append({"code": "SIMPLE_TAXPAYER", "title": "간이과세자 — v1 범위 밖(사람 처리)", "source": "client"})

    # --- 위하고 전표(내보내기) 색인: 이미 반영됐는지·중복 후보 판단
    ledger = [t for t in txns if t.source == Source.WEHAGO_LEDGER]
    ledger_idx: dict[tuple, list[Transaction]] = defaultdict(list)
    for lt in ledger:
        for k in _match_keys(lt):
            ledger_idx[k].append(lt)

    corrections: list[dict] = []
    seq = 0
    for t in sorted(txns, key=lambda x: (x.tx_date, x.direction.value, x.counterparty_name, x.supply_amount, x.id)):
        action, set_vals = _action_for(t)
        if not action:
            continue
        style_vals, keep_default, style_source = _style(t)
        c = t.classification
        match = None
        for k in _match_keys(t):
            if ledger_idx.get(k):
                lt = ledger_idx[k][0]
                match = {"ledger_tx_id": lt.id, "row_no": lt.row_no, "wehago_type": _ledger_type(lt) or None}
                break
        already = None
        if match and match["wehago_type"]:
            wt = match["wehago_type"]
            if action == "set_non_deductible":
                already = ("불공" in wt) or wt.strip().startswith("54")
        human_reasons = [f"{b['code']}: {b['title']}" for b in open_by_tx.get(t.id, [])]
        if action in ("deemed_input_candidate", "exclude_from_return"):
            human_reasons.append("v1 범위 밖/이례적 — 사람 확인")
        if action == "mark_fixed_asset":
            set_vals = {**set_vals, "note": "자산 구분(건물·구축물/기계장치/차량운반구/기타)은 화면에서 확인, 불확실하면 검토항목"}
        seq += 1
        corrections.append({
            "seq": seq,
            "tx_id": t.id,
            "action": action,
            "key": _tx_key(t),
            "set": {**set_vals, **style_vals},
            "keep_default": keep_default,          # 비어 있는 스타일 필드 = 위하고 기본값 유지
            "basis": {
                "category": c.category.value if c else None,
                "decided_by": c.decided_by.value if c else None,
                "rule_id": c.rule_id if c else "",
                "confidence": c.confidence if c else None,
                "style_source": style_source or None,
                "note": c.note if c else "",
            },
            "ledger_match": match,
            "already_applied": already,
            "requires_human": bool(human_reasons),
            "human_reasons": human_reasons,
        })

    # --- 위하고 전표 중복(삭제 후보)
    seen: dict[tuple, Transaction] = {}
    for lt in sorted(ledger, key=lambda x: (x.row_no, x.id)):
        k = _match_keys(lt)[0]
        if k in seen:
            seq += 1
            corrections.append({
                "seq": seq, "tx_id": lt.id, "action": "delete_candidate", "key": {**_tx_key(lt), "row_no": lt.row_no},
                "set": {}, "keep_default": [], "basis": {"duplicate_of_row": seen[k].row_no, "decided_by": "rule"},
                "ledger_match": {"ledger_tx_id": lt.id, "row_no": lt.row_no, "wehago_type": _ledger_type(lt) or None},
                "already_applied": None,
                "requires_human": not allow_delete,
                "human_reasons": [] if allow_delete else ["삭제는 사람 확인(limits.yaml agent.allow_voucher_delete=false)"],
            })
        else:
            seen[k] = lt

    max_corr = int(agent_cfg.get("max_corrections_per_client") or 0)
    auto_count = sum(1 for x in corrections if not x["requires_human"] and not x["already_applied"])

    # --- 신고서 기대값(대사 기준)
    expected: dict[str, Any] = {"available": ret is not None}
    if ret:
        lines = []
        for ln in Line:
            v = (ret.get("lines") or {}).get(ln.name)
            if not v:
                continue
            lines.append({"line": ln.name, "no": ln.value, "label": LINE_LABELS.get(ln.name, ln.name),
                          "amount": int(v.get("amount") or 0), "tax": int(v.get("tax") or 0), "count": int(v.get("count") or 0)})
        nd = ret.get("non_deductible_breakdown") or {}
        cr = ret.get("card_receipt_summary") or {}
        fa = (ret.get("lines") or {}).get("P_TI_FIXED") or {}
        od = ret.get("other_deductible_breakdown") or {}
        expected.update({
            "lines": lines,
            "final_tax": int(((ret.get("lines") or {}).get("FINAL") or {}).get("tax") or 0),
            "non_deductible_breakdown": nd,
            "card_receipt_summary": cr,
            "other_deductible_breakdown": od,
            "attachments": {
                "신용카드매출전표등수령명세서": bool(cr) or any("카드" in k or "현금" in k for k in od),
                "공제받지못할매입세액명세서": bool(nd),
                "건물등감가상각자산취득명세서": bool(fa.get("amount")) or any("고정" in k for k in od),
            },
            "notes": ret.get("notes") or [],
        })

    stop_before = "60_vat_return" if block_reasons else None
    plan_steps = PROCEDURES[: PROCEDURES.index(stop_before)] if stop_before else list(PROCEDURES)
    name = client.name if client else str(state.get("client_name") or "")
    biz = client.biz_no if client else ""
    order = {
        "schema": SCHEMA,
        "generated_at": (now or datetime.now().astimezone()).isoformat(timespec="seconds"),
        "client_id": client_id,
        "period": period,
        "company": {
            "client_id": client_id,
            "name": name,
            "biz_no": biz,
            "biz_no_dash": _biz_dash(biz),
            "taxpayer_type": client.taxpayer_type.value if client else None,
            "switch_hint": "위하고 회사(수임처) 목록에서 사업자번호로 찾고, 상호가 같은지 함께 확인",
        },
        "filing": {k: filing.get(k) for k in ("coverage_start", "coverage_end", "due_date", "filed_preliminary")},
        "prepaid": {
            "preliminary_notice_tax": int(filing.get("preliminary_notice_tax") or 0),   # (22) 예정고지세액
            "preliminary_unrefunded": int(filing.get("preliminary_unrefunded") or 0),   # (21) 예정신고 미환급세액
        },
        "blocked": bool(block_reasons),
        "block_reasons": block_reasons,
        "plan": {
            "procedures": plan_steps,
            "stop_before": stop_before,
            "efile": "prepare_only",   # 전자신고 파일 제작까지만. 홈택스 제출 절대 금지
            "submit": False,
            "large_correction_set": bool(max_corr and auto_count > max_corr),
        },
        "corrections": corrections,
        "corrections_summary": dict(Counter(x["action"] for x in corrections)),
        "corrections_auto": auto_count,
        "corrections_human": sum(1 for x in corrections if x["requires_human"]),
        "expected_return": expected,
        "reconcile": {
            "tolerance_won": 0,
            "how": "위하고 신고서 내보내기 파일을 ingest_file 로 넣고 get_filing 대사 결과 확인. 불일치면 원인 기록 후 중단",
        },
        "warnings": warnings,
        "engine_inputs": {n: _sha1(root / n) for n in ("transactions.json", "review.json", "return.json", "filing.json")},
    }
    return order


def work_order_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / "wehago" / "work_order.json"


def save_work_order(workspace_root: Path | str, order: dict | None = None, **kw: Any) -> Path:
    order = order if order is not None else build_work_order(workspace_root, **kw)
    p = work_order_path(workspace_root)
    _dump(p, order)
    return p


def load_work_order(workspace_root: Path | str) -> dict | None:
    p = work_order_path(workspace_root)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def is_stale(workspace_root: Path | str, order: dict | None = None) -> bool:
    """엔진 결과가 지시서 생성 이후 바뀌었으면 True(다시 만들어야 함)."""
    order = order or load_work_order(workspace_root)
    if not order:
        return True
    root = Path(workspace_root)
    return any(_sha1(root / n) != h for n, h in (order.get("engine_inputs") or {}).items())


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m taxauto.wehago.work_order", description="위하고 작업지시서 생성")
    ap.add_argument("--client", required=True)
    ap.add_argument("--period", required=True)
    ap.add_argument("--base-dir", default=".")
    a = ap.parse_args(argv)
    from .replay import workspace_root

    base = Path(a.base_dir).resolve()
    root = workspace_root(base, a.period, a.client)
    order = build_work_order(root, clients_dir=base / "clients")
    p = save_work_order(root, order)
    print(json.dumps({"path": str(p), "blocked": order["blocked"], "block_reasons": [b["title"] for b in order["block_reasons"]],
                      "corrections": order["corrections_summary"], "auto": order["corrections_auto"],
                      "human": order["corrections_human"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

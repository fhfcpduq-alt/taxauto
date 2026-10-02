"""taxauto MCP 서버 — 엔진 기능을 AI 에이전트 도구로 노출 (stdio).

실행:  python -m taxauto.agent.mcp_server
작업 폴더: 환경변수 TAXAUTO_HOME, 없으면 현재 폴더.

의도적으로 없는 도구: 전자신고 제출, 데이터·검토항목 삭제.
에이전트가 직접 판단한 결과는 decided_by=llm + needs_review 로 남고, 메모리 저장(remember)은
사람이 승인했을 때만(승인자 이름 필수) 가능하다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from .. import ops
from ..models import Direction, Line, PurchaseCategory, ReviewStatus, Severity, Transaction
from ..pipeline import Paths, STAGE_ORDER, build_summary, final_tax_of, sort_review
from ..redact import redact

mcp = FastMCP(
    "taxauto",
    instructions=(
        "세무사무실 부가가치세 신고 엔진 도구. 금액·세액은 도구 결과 값만 인용하고 직접 계산하지 마세요. "
        "전자신고 제출과 삭제는 할 수 없습니다. 확실하지 않은 세법 판단은 단정하지 말고 사람에게 넘기세요. "
        "remember=True(거래처 메모리 저장)는 사람이 승인한 경우에만, 승인자 이름과 함께 쓰세요."
    ),
)


def _paths() -> Paths:
    return Paths.from_base(None)


def _period(period: str | None) -> str:
    from ..models import TaxPeriod
    from ..period import current_period

    return TaxPeriod.parse(period).code if period else current_period(date.today()).code


def _line_list(ret: dict | None) -> list[dict]:
    from ..report.fmt import LINE_LABELS

    if not ret:
        return []
    lines = ret.get("lines") or {}
    out = []
    for ln in Line:
        v = lines.get(ln.name)
        if not v or not (v.get("amount") or v.get("tax")) and ln.name not in ("S_TOTAL", "P_NET", "PAYABLE", "FINAL"):
            continue
        out.append({"line": ln.name, "no": ln.value, "label": LINE_LABELS.get(ln.name, ln.name),
                    "amount": int(v.get("amount") or 0), "tax": int(v.get("tax") or 0), "count": int(v.get("count") or 0)})
    return out


def _item_out(cid: str, d: dict) -> dict:
    return {
        "id": d.get("id"), "client_id": cid, "severity": d.get("severity"), "status": d.get("status"),
        "code": d.get("code"), "title": redact(d.get("title")), "detail": redact(d.get("detail"))[:800],
        "tax_impact": int(d.get("tax_impact") or 0), "suggested_action": d.get("suggested_action", ""),
        "tx_ids": list(d.get("tx_ids") or [])[:50], "tx_count": len(d.get("tx_ids") or []),
        "resolution": redact(d.get("resolution")), "resolved_by": d.get("resolved_by", ""),
    }


def _tx_out(t: Transaction) -> dict:
    c = t.classification
    return {
        "id": t.id,
        "direction": t.direction.value,
        "date": t.tx_date.isoformat(),
        "source": t.source.value,
        "doc_type": t.doc_type.value,
        "counterparty_name": redact(t.counterparty_name),
        "counterparty_biz_no": t.counterparty_biz_no,
        "counterparty_tax_type": t.counterparty_tax_type,
        "counterparty_status": t.counterparty_status,
        "item": redact(t.item),
        "merchant_category": t.merchant_category,
        "supply_amount": int(t.supply_amount),
        "vat": int(t.vat),
        "total": int(t.total),
        "card_kind": t.card_kind.value if t.card_kind else None,
        "card_no_masked": t.card_no_masked,
        "memo": redact(t.memo)[:100],
        "classification": None if c is None else {
            "category": c.category.value,
            "non_deductible_reason": c.non_deductible_reason.value if c.non_deductible_reason else None,
            "exclusion_reason": c.exclusion_reason.value if c.exclusion_reason else None,
            "decided_by": c.decided_by.value, "rule_id": c.rule_id, "confidence": c.confidence,
            "needs_review": c.needs_review, "note": redact(c.note),
        },
    }


# ---------------------------------------------------------------------------
# 조회 도구
# ---------------------------------------------------------------------------


@mcp.tool()
def list_filings(period: str | None = None) -> dict:
    """회차 전체 거래처 신고 현황을 돌려줍니다(_summary.json 과 같은 구조, 최신 파일 기준으로 다시 집계).

    period: 신고회차 코드. 예 '2026-2P'(2기 예정), '2026-2F'(2기 확정). 생략하면 오늘 기준 회차.
    반환 clients[] 의 주요 필드:
      status(ok|failed|not_implemented|skipped|pending), final_tax(+납부/-환급, 원),
      blocker_open/warn_open(미해결 차단·경고 수), ready_to_file(차단 0이고 계산 완료),
      due_date, d_day, failed_stage, error, top_items(우선 볼 검토항목 3개), wehago_status.
    """
    return build_summary(_period(period), paths=_paths())


@mcp.tool()
def get_filing(client_id: str, period: str) -> dict:
    """거래처 1곳의 신고 상세: 요약 + 신고서 라인(엔진 독립 재계산) + 계산 노트 + 미해결 검토항목.

    client_id: 거래처 코드(예 'C001'). period: 회차 코드(예 '2026-2P').
    lines[].line 은 신고서 칸 이름(FINAL=차가감 납부할 세액, 27번). 금액은 모두 원 단위 정수.
    wehago_diff 는 위하고 신고서 내보내기(wehago_return.json)가 있을 때 칸별 차이(엔진-위하고)입니다.
    """
    paths = _paths()
    p = _period(period)
    client = ops.find_client(paths, client_id)
    s = build_summary(p, paths=paths)
    row = next((r for r in s["clients"] if r["client_id"] == client.id), None)
    ws = paths.workspace(p, client.id)
    ret = ws.load_json("return.json")
    items = sort_review(ws.load_json("review.json", []) or [])
    wret = ws.load_json("wehago_return.json")
    diff = None
    if ret and wret:
        diff = []
        el, wl = ret.get("lines") or {}, wret.get("lines") or {}
        for name in sorted(set(el) | set(wl), key=lambda n: list(Line.__members__).index(n) if n in Line.__members__ else 999):
            ea, et = int((el.get(name) or {}).get("amount") or 0), int((el.get(name) or {}).get("tax") or 0)
            wa, wt = int((wl.get(name) or {}).get("amount") or 0), int((wl.get(name) or {}).get("tax") or 0)
            if ea != wa or et != wt:
                diff.append({"line": name, "amount_diff": ea - wa, "tax_diff": et - wt, "engine_tax": et, "wehago_tax": wt})
    return {
        "summary": row,
        "filing": ops.load_filing(paths, client, p).to_dict(),
        "final_tax": final_tax_of(ret),
        "lines": _line_list(ret),
        "notes": [redact(n) for n in (ret or {}).get("notes") or []],
        "non_deductible_breakdown": (ret or {}).get("non_deductible_breakdown") or {},
        "open_review_items": [_item_out(client.id, i) for i in items if i.get("status") == ReviewStatus.OPEN.value],
        "wehago_diff": diff,
        "report_files": {k: str(ws.report_dir / f) for k, f in
                         (("review_html", "review.html"), ("kakao", "kakao.txt"), ("xlsx", "review_items.xlsx"))
                         if (ws.report_dir / f).exists()},
    }


@mcp.tool()
def list_review_items(period: str, client_id: str | None = None, open_only: bool = True) -> list[dict]:
    """검토항목 목록. 차단(신고 불가) → 경고(사람 확인) → 참고 순, 세액영향 큰 순으로 정렬됩니다.

    period: 회차 코드. client_id: 생략하면 회차 전체 거래처. open_only: True 면 미해결만.
    각 항목의 id 로 resolve_review_item 을 호출합니다. tx_ids 는 get_transactions 결과의 id 와 같습니다.
    """
    paths = _paths()
    p = _period(period)
    cids = [client_id] if client_id else ops.client_ids_in_period(paths, p)
    out: list[dict] = []
    for cid in cids:
        for d in sort_review(paths.workspace(p, cid).load_json("review.json", []) or []):
            if open_only and d.get("status") != ReviewStatus.OPEN.value:
                continue
            out.append(_item_out(cid, d))
    sev = {Severity.BLOCKER.value: 0, Severity.WARN.value: 1, Severity.INFO.value: 2}
    out.sort(key=lambda i: (i["status"] != ReviewStatus.OPEN.value, sev.get(i["severity"], 9), -abs(i["tax_impact"])))
    return out


@mcp.tool()
def get_transactions(client_id: str, period: str, filter: str = "needs_review", limit: int = 50) -> dict:
    """거래 목록(민감정보 마스킹). 매입 분류 판단 근거를 볼 때 씁니다.

    filter:
      needs_review   — 분류 확인이 필요한 매입(미분류 포함). 기본값.
      non_deductible — 불공제로 분류된 매입
      excluded       — 신고제외(면세·간이·중복 등)로 분류된 매입
      all            — 전체(매출 포함)
    limit: 최대 반환 건수(1~500). 금액이 큰 순으로 정렬됩니다.
    """
    paths = _paths()
    p = _period(period)
    client = ops.find_client(paths, client_id)
    txns = paths.workspace(p, client.id).load_transactions()
    f = (filter or "needs_review").strip()
    if f == "needs_review":
        sel = [t for t in txns if t.direction == Direction.PURCHASE and (t.classification is None or t.classification.needs_review)]
    elif f == "non_deductible":
        sel = [t for t in txns if t.classification and t.classification.category == PurchaseCategory.NON_DEDUCTIBLE]
    elif f == "excluded":
        sel = [t for t in txns if t.classification and t.classification.category == PurchaseCategory.NOT_APPLICABLE]
    elif f == "all":
        sel = list(txns)
    else:
        raise ValueError("filter 는 needs_review | non_deductible | excluded | all")
    sel.sort(key=lambda t: (-abs(t.supply_amount), t.tx_date))
    limit = max(1, min(int(limit or 50), 500))
    return {"client_id": client.id, "period": p, "filter": f, "total": len(sel),
            "returned": min(limit, len(sel)), "transactions": [_tx_out(t) for t in sel[:limit]]}


# ---------------------------------------------------------------------------
# 결정 반영 도구
# ---------------------------------------------------------------------------


@mcp.tool()
def resolve_review_item(client_id: str, period: str, item_id: str, status: str, note: str,
                        remember: bool = False, resolved_by: str = "agent") -> dict:
    """검토항목 처리 결과를 기록하고 리포트·요약을 갱신합니다(신고서 재계산은 안 함).

    status: '해결'(원인 해소·수정 완료) | '확인후유지'(확인했고 지금 값 그대로 신고) | '미해결'(되돌리기).
    note: 처리 근거(필수, 감사 기록). 거래처명·금액 외 민감정보(주민번호·카드번호 전체)는 쓰지 마세요.
    resolved_by: 처리 주체. 에이전트 스스로의 판단이면 'agent' 그대로, 사람이 지시했으면 그 사람 이름.
    remember: True 면 이 항목 거래들의 현재 분류를 거래처 메모리에 저장(다음부터 자동 적용).
              사람이 승인한 경우에만 쓰고, resolved_by 에 승인자 이름이 있어야 합니다.
    차단 항목을 '확인후유지'로 닫는 것은 사람 판단이 필요한 경우가 많습니다 — 근거가 약하면 닫지 말고 사람에게 넘기세요.
    """
    return ops.resolve_review_item(_paths(), _period(period), item_id, status, note,
                                   client_id=client_id, resolved_by=resolved_by, remember=remember)


@mcp.tool()
def reclassify_transaction(client_id: str, period: str, tx_id: str, category: str, reason: str = "",
                           note: str = "", remember: bool = False, decided_by: str = "agent",
                           confidence: float | None = None) -> dict:
    """매입 1건의 공제 분류를 바꾸고 compute → validate → report 를 다시 돌립니다.

    category: '일반매입' | '고정자산매입' | '불공제' | '신고제외' | '의제매입후보'
    reason:
      불공제   → '필요적기재사항누락','사업과직접관련없는지출','비영업용소형승용자동차','접대비및이와유사한비용',
                 '면세사업등관련','토지의자본적지출관련','사업자등록전매입세액','공통매입세액면세사업분','기타' 중 하나
      신고제외 → '부가세없음','간이과세자(영수증)가맹점','면세사업자가맹점','폐업자','세금계산서중복','개인사용',
                 '카드공제불가업종','기타' 중 하나
      그 외    → 자유 문장(판단 근거)
    decided_by: 'agent'(기본) 면 AI 판정으로 기록(decided_by=llm, needs_review=True, confidence 사용).
                사람이 결정했으면 그 사람 이름(→ decided_by=human, 다음 실행에도 이 거래 분류 유지).
    remember: True 면 같은 거래처(가맹점)의 이후 거래에도 적용되도록 메모리에 저장. 사람 결정일 때만 가능.
    반환: 변경 전/후 분류, 재실행 결과(rerun.summary 에 새 납부세액·검토항목 수).
    """
    return ops.reclassify_transaction(_paths(), _period(period), client_id, tx_id, category, reason, note,
                                      decided_by=decided_by, confidence=confidence, remember=remember)


@mcp.tool()
def run_pipeline(period: str, client_ids: list[str] | None = None, from_stage: str | None = None) -> dict:
    """파이프라인을 실행합니다(collect→normalize→enrich→classify→compute→validate→report).

    client_ids: 생략하면 회차 전체. from_stage: 이 단계부터 재실행(앞 단계 결과 재사용).
      가능한 단계: collect, normalize, enrich, classify, compute, validate, report
    다른 실행(야간 실행 등)이 진행 중이면 오류를 돌려줍니다 — 잠시 뒤 다시 시도하세요.
    반환: totals(완료·실패·차단 수 등)와 거래처별 상태.
    """
    from ..pipeline import run_period

    if from_stage and from_stage not in STAGE_ORDER:
        raise ValueError(f"from_stage 는 {', '.join(STAGE_ORDER)} 중 하나")
    s = run_period(_period(period), client_ids=client_ids, from_stage=from_stage, base_dir=_paths().base, command="mcp")
    return {"period": s["period"], "totals": s["totals"], "last_run": s.get("last_run"),
            "clients": [{k: r.get(k) for k in ("client_id", "name", "status", "failed_stage", "error", "final_tax",
                                                "blocker_open", "warn_open", "ready_to_file", "d_day")} for r in s["clients"]]}


@mcp.tool()
def draft_client_message(client_id: str, period: str) -> dict:
    """거래처 안내 카톡 초안(현재 결과 파일 기준으로 새로 생성).

    sendable=False 이면(차단 미해결·세액 미산출) 본문 맨 위에 '초안 아님 — 발송 금지' 가 붙습니다.
    발송은 사람이 합니다. 문구를 다듬을 때도 금액·기한 숫자는 바꾸지 마세요.
    """
    from ..report.stage import render_kakao
    from ..law import load_policy

    paths = _paths()
    p = _period(period)
    client = ops.find_client(paths, client_id)
    ws = paths.workspace(p, client.id)
    ret = ws.load_return()
    items = ws.load_review()
    office = str((load_policy(paths.config_dir).get("office") or {}).get("name") or "")
    text = render_kakao(client, ops.load_filing(paths, client, p), ret, items, office_name=office)
    blk = sum(1 for i in items if i.status == ReviewStatus.OPEN and i.severity == Severity.BLOCKER)
    warn = sum(1 for i in items if i.status == ReviewStatus.OPEN and i.severity == Severity.WARN)
    sendable = ret is not None and "FINAL" in ret.lines and blk == 0
    return {"client_id": client.id, "sendable": sendable, "blocker_open": blk, "warn_open": warn, "text": text}


# ---------------------------------------------------------------------------
# 위하고 조작 에이전트 연동
# ---------------------------------------------------------------------------


@mcp.tool()
def ingest_file(client_id: str, period: str, path: str) -> dict:
    """위하고·홈택스에서 내려받은 파일(전표 목록, 신고서 내보내기, 카드·현금영수증 내역 등)을
    inbox/{period}/{client_id}/ 로 복사하고 전체 단계를 다시 실행합니다.

    path: 내려받은 파일의 절대경로(.xlsx .xls .csv .json .zip .txt). 원본은 지우지 않습니다.
    같은 내용의 파일이 이미 있으면 복사하지 않고 재실행만 합니다.
    반환: inbox_path, rerun.status, rerun.summary(새 납부세액·검토항목 수).
    """
    return ops.ingest_file(_paths(), _period(period), client_id, path)


@mcp.tool()
def get_work_order(client_id: str, period: str) -> dict:
    """위하고에서 고쳐야 할 작업지시서(불공제 전환·고정자산 표시·중복 정리·예정고지세액 입력 등)를 돌려줍니다.

    엔진의 공제판정·검증 결과에서 만들어집니다. 작업지시 모듈이 아직 없으면 status='not_implemented'.
    작업을 수행한 뒤에는 record_agent_step 으로 단계별 결과를 남기고, 위하고 신고서를 내려받아 ingest_file 로 대사하세요.
    """
    return ops.get_work_order(_paths(), _period(period), client_id)


@mcp.tool()
def record_agent_step(client_id: str, period: str, step: str, status: str, note: str = "",
                      evidence_path: str | None = None) -> dict:
    """위하고 조작 단계 결과를 작업기록(data/{period}/{client_id}/wehago/agent_log.jsonl)에 추가합니다.

    step: 단계 이름(예 'login','switch_company','collect_hometax','auto_voucher','apply_work_order',
          'write_return','export_return'). status: 'started'|'ok'|'failed'|'skipped'|'needs_human' 권장.
    note: 무엇을 했는지/왜 멈췄는지 한두 줄(비밀번호·인증서 정보 금지). evidence_path: 스크린샷 등 증빙 파일 경로.
    마지막 기록은 대시보드·_summary.json 의 wehago_status 로 보입니다. 기록은 추가만 되고 지울 수 없습니다.
    """
    return ops.record_agent_step(_paths(), _period(period), client_id, step, status, note, evidence_path)


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()

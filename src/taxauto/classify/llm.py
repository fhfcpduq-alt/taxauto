"""선택적 AI 분류기 (Anthropic Claude).

- policy.llm.enabled 가 true 이고 ANTHROPIC_API_KEY 가 있을 때만 동작(클라이언트를 주입하면 키 검사 생략).
- 대상: 사람·메모리 결정이 아닌 매입 중 needs_review 이거나 기본값(default)으로 떨어진 '애매한' 거래.
- 전송 항목: 가맹점명·가맹점업종·품목·증빙종류·공급가액·세액·상대방 과세유형, 거래처(우리 고객) 업종.
  카드번호·승인번호·사업자번호·메모·원본행은 보내지 않는다. 사람 이름으로 보이는 가맹점명은 마스킹.
- 결과: confidence ≥ policy.review.llm_auto_accept_confidence 면 반영(decided_by=llm, needs_review 유지),
  미만이면 기존 판정 유지 + note 에 'AI 제안' 기록.
- 네트워크 오류·SDK 미설치·키 없음 → 조용히 건너뜀(엔진은 결정적 결과만으로도 완결).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from ..compute.lawutil import policy_get
from ..models import (
    Classification,
    Client,
    DecidedBy,
    Direction,
    ExclusionReason,
    NonDeductibleReason,
    PurchaseCategory,
    Source,
    Transaction,
)

log = logging.getLogger("taxauto.classify.llm")

BATCH_SIZE = 50

SYSTEM_PROMPT = """당신은 한국 세무사무실의 부가가치세 매입세액 공제 판정 보조자입니다.
일반과세자(개인·법인)의 매입 거래 목록을 받아 각 거래를 다음 중 하나로 분류합니다.
- 일반매입: 매입세액 공제 대상 일반 비용
- 고정자산매입: 공제 대상이며 감가상각자산(비품·기계·차량운반구·시설 등)
- 불공제: 공제받지 못할 매입세액(사유를 non_deductible_reason 에 기재)
- 신고제외: 카드·현금영수증 중 수령명세서에 넣지 않는 것(사유를 exclusion_reason 에 기재)
- 의제매입후보: 면세 농축수산물 원재료(의제매입 업종 거래처만)

판단 기준(부가가치세법 제39조, 제46조 및 시행령):
- 접대비 성격(골프·유흥·선물·상품권 등)은 불공제(접대비및이와유사한비용).
- 비영업용 소형승용자동차의 구입·유지·임차 비용은 불공제(비영업용소형승용자동차). 화물차·경차·9인승 이상 등은 공제.
- 사업과 직접 관련 없는 지출(가사용 등)은 불공제(사업과직접관련없는지출).
- 목욕·이발·미용·여객운송(전세버스 제외)·입장권·진료 등 영수증 발급 업종의 카드매입은 신고제외(카드공제불가업종).
- 확신이 없으면 confidence 를 낮게 주세요. 추측으로 높은 confidence 를 주지 마세요.
반드시 입력의 모든 index 에 대해 결과를 하나씩 돌려주세요."""


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "category": {"type": "string", "enum": [c.value for c in PurchaseCategory]},
                        "non_deductible_reason": {"type": "string", "enum": [""] + [r.value for r in NonDeductibleReason]},
                        "exclusion_reason": {"type": "string", "enum": [""] + [r.value for r in ExclusionReason]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["index", "category", "non_deductible_reason", "exclusion_reason", "confidence", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


_PERSON_NAME = re.compile(r"^[가-힣]{2,4}$")
_BUSINESS_HINT = re.compile(r"(상회|상사|마트|식당|약국|센터|카페|점|社|商)$")
_DIGITS = re.compile(r"\d{6,}")


def _mask_name(name: str) -> str:
    n = (name or "").strip()
    if _PERSON_NAME.match(n) and not _BUSINESS_HINT.search(n):
        return "(개인명 마스킹)"
    return _DIGITS.sub("***", n)  # 상호에 섞인 긴 숫자(전화·카드번호 등) 제거


def to_prompt_item(i: int, t: Transaction) -> dict:
    """LLM 에 보내는 최소 정보(민감정보 제외)."""
    return {
        "index": i,
        "가맹점명": _mask_name(t.counterparty_name),
        "가맹점업종": t.merchant_category,
        "품목": _DIGITS.sub("***", t.item or ""),
        "증빙": t.doc_type.value,
        "공급가액": t.supply_amount,
        "세액": t.vat,
        "상대방과세유형": t.counterparty_tax_type,
        "현재판정": t.classification.category.value if t.classification else "",
    }


def is_ambiguous(t: Transaction) -> bool:
    if t.direction != Direction.PURCHASE or t.source == Source.WEHAGO_LEDGER:
        return False
    c = t.classification
    if c is None:
        return True
    if c.decided_by in (DecidedBy.HUMAN, DecidedBy.MEMORY, DecidedBy.LLM):
        return False
    return c.needs_review or c.decided_by == DecidedBy.DEFAULT


@dataclass
class LLMRunResult:
    sent: int = 0
    applied: int = 0
    suggested: int = 0
    skipped_reason: str = ""


def _default_client_factory() -> Any:
    import anthropic  # 선택 의존성

    return anthropic.Anthropic()


def _call(client: Any, model: str, client_info: dict, items: list[dict]) -> list[dict]:
    user = (
        "거래처 정보: " + json.dumps(client_info, ensure_ascii=False) + "\n"
        "매입 거래 목록(JSON):\n" + json.dumps(items, ensure_ascii=False)
    )
    resp = client.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _schema()}},
    )
    if getattr(resp, "stop_reason", None) in ("refusal", "max_tokens"):
        log.warning("LLM 응답 중단(stop_reason=%s) - 이 묶음은 건너뜀", resp.stop_reason)
        return []
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    data = json.loads(text or "{}")
    return list(data.get("results") or [])


def _to_classification(r: dict, prev: Classification | None) -> Classification:
    cat = PurchaseCategory(r["category"])
    ndr = NonDeductibleReason(r["non_deductible_reason"]) if r.get("non_deductible_reason") else None
    exr = ExclusionReason(r["exclusion_reason"]) if r.get("exclusion_reason") else None
    if cat == PurchaseCategory.NON_DEDUCTIBLE and ndr is None:
        ndr = NonDeductibleReason.OTHER
    if cat == PurchaseCategory.NOT_APPLICABLE and exr is None:
        exr = ExclusionReason.OTHER
    prev_note = f" / 이전 판정: {prev.category.value}({prev.rule_id})" if prev else ""
    return Classification(
        category=cat,
        non_deductible_reason=ndr if cat == PurchaseCategory.NON_DEDUCTIBLE else None,
        exclusion_reason=exr if cat == PurchaseCategory.NOT_APPLICABLE else None,
        decided_by=DecidedBy.LLM,
        rule_id="llm",
        confidence=float(r.get("confidence", 0.0)),
        needs_review=True,
        note=f"AI 판정: {r.get('reason', '')}{prev_note}",
    )


def classify_with_llm(
    txns: list[Transaction],
    client_info: Client,
    policy: dict,
    client_factory: Callable[[], Any] | None = None,
) -> LLMRunResult:
    """애매한 매입만 AI 로 판정해 txns 의 classification 을 제자리 갱신."""
    res = LLMRunResult()
    if not policy_get(policy, "llm.enabled", False):
        res.skipped_reason = "policy.llm.enabled=false"
        return res
    model = policy_get(policy, "llm.model")
    if not model:
        res.skipped_reason = "policy.llm.model 없음"
        return res
    if client_factory is None and not os.environ.get("ANTHROPIC_API_KEY"):
        res.skipped_reason = "ANTHROPIC_API_KEY 없음"
        return res
    targets = [t for t in txns if is_ambiguous(t)][: int(policy_get(policy, "llm.max_items_per_run", 300))]
    if not targets:
        res.skipped_reason = "대상 없음"
        return res
    threshold = float(policy_get(policy, "review.llm_auto_accept_confidence", 1.0))
    try:
        client = (client_factory or _default_client_factory)()
    except Exception as e:  # SDK 미설치 등
        res.skipped_reason = f"클라이언트 생성 실패: {type(e).__name__}"
        log.info("LLM 분류 건너뜀: %s", res.skipped_reason)
        return res

    cinfo = {
        "업종": client_info.industry,
        "업종코드": client_info.industry_code,
        "사업자유형": client_info.taxpayer_type.value,
        "의제매입업종": client_info.deemed_input_type or "해당없음",
        "등록차량": [{"차종": v.model, "공제대상": v.deductible} for v in client_info.vehicles],
    }
    for start in range(0, len(targets), BATCH_SIZE):
        chunk = targets[start : start + BATCH_SIZE]
        items = [to_prompt_item(i, t) for i, t in enumerate(chunk)]
        try:
            results = _call(client, str(model), cinfo, items)
        except Exception as e:  # 네트워크·API·파싱 오류 → 조용히 건너뜀
            log.info("LLM 호출 실패(건너뜀): %s", type(e).__name__)
            res.skipped_reason = f"호출 실패: {type(e).__name__}"
            continue
        res.sent += len(chunk)
        for r in results:
            try:
                idx = int(r["index"])
                t = chunk[idx]
                c = _to_classification(r, t.classification)
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            if c.confidence >= threshold:
                t.classification = c
                res.applied += 1
            else:
                if t.classification is not None:
                    t.classification.needs_review = True
                    t.classification.note += (
                        f" | AI 제안: {c.category.value}"
                        f"{'(' + c.non_deductible_reason.value + ')' if c.non_deductible_reason else ''}"
                        f" 신뢰도 {c.confidence:.2f} - {r.get('reason', '')}"
                    )
                res.suggested += 1
    return res

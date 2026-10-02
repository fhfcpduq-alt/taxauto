"""매입 공제판정(룰·메모리·AI) 테스트."""

import json
from datetime import date
from types import SimpleNamespace


from calc_fixtures import POLICY, buy_card, buy_ti, client, make_law, sale_card, tx

from taxauto.classify import llm as llm_mod
from taxauto.classify.memory import Memory, add_decision, load_memory, normalize_name
from taxauto.classify.stage import classify_transactions
from taxauto.models import (
    Classification, DecidedBy, Direction, DocType, ExclusionReason, NonDeductibleReason, PurchaseCategory,
    Source, Vehicle,
)

D = date(2026, 8, 5)
P = PurchaseCategory


def run(txns, cl=None, memory=None, law=None):
    classify_transactions(txns, cl or client(), POLICY, law or make_law(), memory, on=date(2026, 12, 31))
    return txns


def one(t, **kw):
    run([t], **kw)
    return t.classification


def test_card_no_vat_excluded():
    c = one(buy_card(D, 10_000, 0, counterparty_name="동네슈퍼"))
    assert (c.category, c.exclusion_reason, c.decided_by) == (P.NOT_APPLICABLE, ExclusionReason.NO_VAT, DecidedBy.RULE)


def test_card_simple_taxpayer_and_tax_free_merchants():
    c1 = one(buy_card(D, 10_000, 1_000, counterparty_tax_type="부가가치세 간이과세자"))
    assert c1.exclusion_reason == ExclusionReason.SIMPLE_TAXPAYER_SELLER
    # 세금계산서 발급 간이과세자는 카드 공제 가능 → 일반매입
    c2 = one(buy_card(D, 10_000, 1_000, counterparty_tax_type="부가가치세 간이과세자(세금계산서 발급사업자)"))
    assert c2.category == P.GENERAL
    c3 = one(buy_card(D, 10_000, 1_000, counterparty_tax_type="부가가치세 면세사업자"))
    assert c3.exclusion_reason == ExclusionReason.TAX_FREE_SELLER


def test_closed_seller_tax_invoice_is_nondeductible_candidate():
    t = buy_ti(D, 1_000_000, 100_000, counterparty_closed_on=date(2026, 7, 31), counterparty_status="폐업")
    c = one(t)
    assert c.category == P.NON_DEDUCTIBLE and c.needs_review and c.rule_id == "ti_closed_seller"
    # 거래일 이후 폐업이면 해당 없음
    t2 = buy_ti(D, 500_000, 50_000, counterparty_closed_on=date(2026, 9, 1))
    assert one(t2).category == P.GENERAL


def test_card_non_deductible_industry():
    c = one(buy_card(D, 20_000, 2_000, counterparty_name="행복목욕탕", merchant_category="목욕탕"))
    assert c.exclusion_reason == ExclusionReason.NON_DEDUCTIBLE_CARD
    c2 = one(buy_card(D, 50_000, 5_000, counterparty_name="(주)코레일", merchant_category="철도"))
    assert c2.exclusion_reason == ExclusionReason.NON_DEDUCTIBLE_CARD


def test_entertainment_candidates():
    c = one(buy_card(D, 300_000, 30_000, counterparty_name="스카이72골프클럽"))
    assert c.category == P.NON_DEDUCTIBLE
    assert c.non_deductible_reason == NonDeductibleReason.ENTERTAINMENT and c.needs_review
    c2 = one(buy_ti(D, 2_000_000, 200_000, item="백화점상품권"))
    assert c2.non_deductible_reason == NonDeductibleReason.ENTERTAINMENT


def test_vehicle_rules_use_client_vehicles():
    fuel = lambda **kw: buy_card(D, 50_000, 5_000, counterparty_name="SK에너지 강남주유소", **kw)
    # 차량정보 없음 → 일반매입 + 검토
    c = one(fuel())
    assert c.category == P.GENERAL and c.needs_review and "차량정보 없음" in c.note
    # 모두 비영업용 소형승용차 → 불공제(비영업용소형승용자동차)
    cl = client(vehicles=[Vehicle(plate="12가3456", model="쏘나타", deductible=False)])
    c = one(fuel(), cl=cl)
    assert c.category == P.NON_DEDUCTIBLE and c.non_deductible_reason == NonDeductibleReason.PASSENGER_CAR
    assert not c.needs_review
    # 섞여 있으면 차량번호로 판정
    cl2 = client(vehicles=[Vehicle(plate="12가3456", deductible=False), Vehicle(plate="34나5678", model="포터", deductible=True)])
    assert one(fuel(memo="34나 5678 주유"), cl=cl2).category == P.GENERAL
    c_mixed = one(fuel(), cl=cl2)
    assert c_mixed.needs_review and "섞여" in c_mixed.note


def test_fixed_asset_threshold_from_policy():
    big = one(buy_ti(D, 1_500_000, 150_000, item="노트북"))
    assert big.category == P.FIXED_ASSET and big.needs_review
    small = one(buy_ti(D, 500_000, 50_000, item="노트북"))
    assert small.category == P.GENERAL and small.decided_by == DecidedBy.DEFAULT


def test_hometax_non_deductible_flag_respected():
    c = one(buy_card(D, 10_000, 1_000, counterparty_name="어딘가", deductible_flag_from_source=False))
    assert c.category == P.NOT_APPLICABLE and "홈택스" in c.note


def test_card_purchase_duplicate_with_tax_invoice():
    ti = buy_ti(D, 1_000_000, 100_000, counterparty_biz_no="2222222222")
    card = buy_card(date(2026, 8, 7), 1_000_000, 100_000, counterparty_biz_no="2222222222")
    other = buy_card(D, 1_000_000, 100_000, counterparty_biz_no="3333333333")
    run([ti, card, other])
    assert card.classification.exclusion_reason == ExclusionReason.DUPLICATE_TAX_INVOICE
    assert other.classification.exclusion_reason is None
    assert ti.classification.category == P.GENERAL


def test_deemed_input_only_for_deemed_clients():
    inv = lambda: tx(Source.EINV_PURCHASE, Direction.PURCHASE, DocType.INVOICE, D, 500_000, 0,
                     counterparty_name="OO축산", item="돼지고기")
    cl = client(industry="음식점", deemed_input_type="restaurant")
    c = one(inv(), cl=cl)
    assert c.category == P.DEEMED_INPUT and not c.needs_review
    c2 = one(inv())
    assert c2.category == P.NOT_APPLICABLE and c2.exclusion_reason == ExclusionReason.NO_VAT
    # 카드 면세 식자재(세액 0) → 의제매입 후보(검토)
    c3 = one(buy_card(D, 80_000, 0, counterparty_name="농협하나로마트"), cl=cl)
    assert c3.category == P.DEEMED_INPUT and c3.needs_review


def test_ti_zero_vat_and_default_and_sales_untouched():
    z = one(buy_ti(D, 3_000_000, 0, counterparty_name="수출대행"))
    assert z.category == P.GENERAL and z.needs_review and z.rule_id == "ti_zero_vat"
    s = sale_card(D, 10_000)
    run([s])
    assert s.classification is None


def test_late_receipt_over_one_year_is_nondeductible():
    """2024-08 공급분 → 2024년 2기 확정기한 2025-01-25(토) → 다음 영업일 2025-01-27, +1년 = 2026-01-27 이후 발급 → 불공제."""
    t = buy_ti(date(2024, 8, 1), 1_000_000, 100_000, issue_date=date(2026, 3, 1))
    c = one(t)
    assert c.category == P.NON_DEDUCTIBLE and c.rule_id == "ti_late_receipt_over_1y"
    t2 = buy_ti(date(2024, 8, 1), 1_000_000, 100_000, issue_date=date(2025, 6, 1))
    assert one(t2).category == P.GENERAL


def test_human_decision_preserved_and_style_fields_carried():
    t = buy_card(D, 300_000, 30_000, counterparty_name="스카이72골프클럽")
    t.classification = Classification(category=P.GENERAL, decided_by=DecidedBy.HUMAN, note="직원 복리후생")
    run([t])
    assert t.classification.decided_by == DecidedBy.HUMAN
    # 스타일 필드는 재판정해도 유지
    t2 = buy_card(D, 300_000, 30_000, counterparty_name="스카이72골프클럽")
    t2.classification = Classification(category=P.GENERAL, decided_by=DecidedBy.RULE, account_code="813", account_name="접대비")
    run([t2])
    assert t2.classification.category == P.NON_DEDUCTIBLE
    assert (t2.classification.account_code, t2.classification.account_name) == ("813", "접대비")


# ---------------------------------------------------------------------------- memory


def test_memory_roundtrip_and_priority(tmp_path):
    golf = buy_card(D, 300_000, 30_000, counterparty_name="스카이72골프클럽", counterparty_biz_no="5555555555")
    add_decision(tmp_path, golf, Classification(category=P.GENERAL, note="직원 체육행사"), scope="counterparty", by="human",
                 today=date(2026, 10, 2))
    entries = load_memory(tmp_path)
    assert entries[0]["key"] == "biz:5555555555" and entries[0]["doc_type"] == DocType.CARD.value
    mem = Memory.load(tmp_path)
    nxt = buy_card(date(2026, 9, 1), 200_000, 20_000, counterparty_name="스카이72 골프클럽", counterparty_biz_no="5555555555")
    c = one(nxt, memory=mem)
    assert c.category == P.GENERAL and c.decided_by == DecidedBy.MEMORY and not c.needs_review
    assert "직원 체육행사" in c.note


def test_memory_by_name_and_once_scope(tmp_path):
    t = buy_card(D, 50_000, 5_000, counterparty_name="(주)행복 상사")
    add_decision(tmp_path, t, Classification(category=P.NON_DEDUCTIBLE, non_deductible_reason=NonDeductibleReason.UNRELATED))
    assert load_memory(tmp_path)[0]["key"] == "name:" + normalize_name("행복상사")
    once = buy_card(D, 70_000, 7_000, counterparty_name="다른가게")
    add_decision(tmp_path, once, Classification(category=P.FIXED_ASSET), scope="once")
    mem = Memory.load(tmp_path)
    a = buy_card(date(2026, 9, 9), 10_000, 1_000, counterparty_name="행복상사")
    b = buy_card(date(2026, 9, 9), 10_000, 1_000, counterparty_name="다른가게")
    run([a, b, once], memory=mem)
    assert a.classification.non_deductible_reason == NonDeductibleReason.UNRELATED
    assert b.classification.decided_by != DecidedBy.MEMORY       # once 는 그 거래에만
    assert once.classification.category == P.FIXED_ASSET


def test_transaction_level_rules_beat_memory(tmp_path):
    """메모리에 '공제'로 저장된 가맹점이라도 세금계산서와 중복된 카드분은 제외."""
    card = buy_card(D, 1_000_000, 100_000, counterparty_biz_no="2222222222")
    add_decision(tmp_path, card, Classification(category=P.GENERAL))
    ti = buy_ti(D, 1_000_000, 100_000, counterparty_biz_no="2222222222")
    run([card, ti], memory=Memory.load(tmp_path))
    assert card.classification.exclusion_reason == ExclusionReason.DUPLICATE_TAX_INVOICE


# ---------------------------------------------------------------------------- LLM


class FakeMessages:
    def __init__(self, results, fail=False):
        self.results, self.fail, self.calls = results, fail, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise ConnectionError("network down")
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps({"results": self.results}))])


def fake_factory(results, fail=False):
    msgs = FakeMessages(results, fail)
    return msgs, (lambda: SimpleNamespace(messages=msgs))


LLM_POLICY = {**POLICY, "llm": {"enabled": True, "model": "test-model", "max_items_per_run": 10}}


def test_llm_applies_high_confidence_and_keeps_review():
    amb = buy_card(D, 40_000, 4_000, counterparty_name="애매한가게", card_no_masked="1234-****-****-5678", memo="김철수 개인메모")
    known = buy_card(D, 10_000, 0)
    run([amb, known])
    assert amb.classification.decided_by == DecidedBy.DEFAULT
    msgs, factory = fake_factory([
        {"index": 0, "category": "불공제", "non_deductible_reason": "사업과직접관련없는지출", "exclusion_reason": "",
         "confidence": 0.95, "reason": "개인 용도"},
    ])
    res = llm_mod.classify_with_llm([amb, known], client(), LLM_POLICY, client_factory=factory)
    assert res.sent == 1 and res.applied == 1
    c = amb.classification
    assert c.decided_by == DecidedBy.LLM and c.needs_review and c.confidence == 0.95
    assert c.non_deductible_reason == NonDeductibleReason.UNRELATED
    sent = msgs.calls[0]
    assert sent["model"] == "test-model"
    payload = sent["messages"][0]["content"]
    assert "애매한가게" in payload and "5678" not in payload and "김철수" not in payload
    assert sent["output_config"]["format"]["type"] == "json_schema"


def test_llm_low_confidence_only_suggests():
    amb = buy_card(D, 40_000, 4_000, counterparty_name="애매한가게")
    run([amb])
    _, factory = fake_factory([{"index": 0, "category": "고정자산매입", "non_deductible_reason": "",
                                "exclusion_reason": "", "confidence": 0.5, "reason": "추정"}])
    res = llm_mod.classify_with_llm([amb], client(), LLM_POLICY, client_factory=factory)
    assert res.suggested == 1 and res.applied == 0
    assert amb.classification.decided_by == DecidedBy.DEFAULT
    assert amb.classification.needs_review and "AI 제안" in amb.classification.note


def test_llm_skips_quietly(monkeypatch):
    amb = buy_card(D, 40_000, 4_000, counterparty_name="애매한가게")
    run([amb])
    before = amb.classification
    # 꺼져 있음
    assert llm_mod.classify_with_llm([amb], client(), POLICY).skipped_reason
    # 키 없음
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "ANTHROPIC_API_KEY" in llm_mod.classify_with_llm([amb], client(), LLM_POLICY).skipped_reason
    # 네트워크 오류
    _, factory = fake_factory([], fail=True)
    res = llm_mod.classify_with_llm([amb], client(), LLM_POLICY, client_factory=factory)
    assert res.applied == 0 and "호출 실패" in res.skipped_reason
    assert amb.classification is before


def test_llm_masks_personal_names():
    assert llm_mod._mask_name("홍길동") == "(개인명 마스킹)"
    assert llm_mod._mask_name("홍길동상회") == "홍길동상회"
    assert "***" in llm_mod._mask_name("가게 010123456789")

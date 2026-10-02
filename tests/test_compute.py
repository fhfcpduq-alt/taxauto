"""신고서 재계산 테스트. 각 테스트에 손계산 산식을 주석으로 적고 기대값을 검증한다."""

from datetime import date

from calc_fixtures import (
    POLICY, C, buy_card, buy_ti, client, filing, make_law, sale_card, sale_ti, tx,
)

from taxauto.models import (
    CardKind, DocType, Direction, ExclusionReason, Line, NonDeductibleReason, PurchaseCategory, Source,
    TaxpayerType,
)
from taxauto.registry import Adjustment, FilingSettings
from taxauto.compute.vat_return import compute_return, compute_return_detailed

G = PurchaseCategory.GENERAL
D = date(2026, 8, 5)


def L(ret, ln):
    x = ret.line(ln)
    return (x.amount, x.tax)


def base_txns():
    return [
        # 매출 세금계산서 1억 / 1천만
        sale_ti(D, 100_000_000, 10_000_000, approval_no="S1", issue_date=D),
        # 카드매출 공급대가 1,100만 (세액 미구분)
        sale_card(date(2026, 9, 1), 11_000_000, approval_no="K1"),
        # 매입 세금계산서: 일반 4,900만/490만 + 접대비(불공제) 100만/10만 → 10번에 5천만/500만, 16번 100만/10만
        buy_ti(D, 49_000_000, 4_900_000, C(G), approval_no="P1"),
        buy_ti(D, 1_000_000, 100_000, C(PurchaseCategory.NON_DEDUCTIBLE, non_deductible_reason=NonDeductibleReason.ENTERTAINMENT), approval_no="P2"),
        # 고정자산 세금계산서 300만/30만 → 11번
        buy_ti(D, 3_000_000, 300_000, C(PurchaseCategory.FIXED_ASSET), approval_no="P3", item="노트북"),
        # 사업용카드 200만/20만 공제, 현금영수증 50만/5만 공제 → 14번 250만/25만
        buy_card(D, 2_000_000, 200_000, C(G), approval_no="CP1"),
        tx(Source.CASH_RECEIPT_PURCHASE, Direction.PURCHASE, DocType.CASH_RECEIPT, D, 500_000, 50_000,
           approval_no="CR1", classification=C(G)),
        # 카드 신고제외(간이 가맹점) 30만/3만 → 신고서 밖
        buy_card(D, 300_000, 30_000, C(PurchaseCategory.NOT_APPLICABLE, exclusion_reason=ExclusionReason.SIMPLE_TAXPAYER_SELLER), approval_no="CP2"),
    ]


def test_full_return_individual_final():
    """개인 2기 확정(예정고지, 6개월 집계).

    매출: 1번 100,000,000 / 10,000,000
          3번 공급대가 11,000,000 → 세액 11,000,000×10/110 = 1,000,000, 공급가액 10,000,000
          9번 110,000,000 / 11,000,000
    매입: 10번 49,000,000+1,000,000 = 50,000,000 / 5,000,000
          11번 3,000,000 / 300,000
          14번 2,000,000+500,000 = 2,500,000 / 250,000
          15번 = 10+11+14 = 55,500,000 / 5,550,000
          16번 1,000,000 / 100,000 (접대비)
          17번 = 15-16 = 54,500,000 / 5,450,000
    ㉰ = 11,000,000 - 5,450,000 = 5,550,000
    19번 = 11,000,000 × 1.3% = 143,000 (연한도 1천만, 납부세액 5,550,000 이내)
    27번 = 5,550,000 - 143,000 - 예정고지 1,000,000 = 4,407,000
    """
    ret = compute_return(base_txns(), client(), filing(notice=1_000_000), make_law(), POLICY, FilingSettings())
    assert L(ret, Line.S_TAX_INVOICE) == (100_000_000, 10_000_000)
    assert L(ret, Line.S_CARD_CASH) == (10_000_000, 1_000_000)
    assert L(ret, Line.S_TOTAL) == (110_000_000, 11_000_000)
    assert L(ret, Line.P_TI_GENERAL) == (50_000_000, 5_000_000)
    assert L(ret, Line.P_TI_FIXED) == (3_000_000, 300_000)
    assert L(ret, Line.P_OTHER_DEDUCTIBLE) == (2_500_000, 250_000)
    assert L(ret, Line.P_TOTAL) == (55_500_000, 5_550_000)
    assert L(ret, Line.P_NON_DEDUCTIBLE) == (1_000_000, 100_000)
    assert L(ret, Line.P_NET) == (54_500_000, 5_450_000)
    assert ret.line(Line.PAYABLE).tax == 5_550_000
    assert ret.line(Line.C_CARD_ISSUE).tax == 143_000
    assert ret.line(Line.C_CARD_ISSUE).amount == 11_000_000
    assert ret.line(Line.C_TOTAL).tax == 143_000
    assert ret.line(Line.PRELIM_NOTICE).tax == 1_000_000
    assert ret.line(Line.PENALTY).tax == 0
    assert ret.line(Line.FINAL).tax == 4_407_000
    # 부속 집계
    assert ret.non_deductible_breakdown[NonDeductibleReason.ENTERTAINMENT.value].tax == 100_000
    assert ret.other_deductible_breakdown["신용카드등_일반"].tax == 250_000
    assert (ret.card_receipt_summary[CardKind.BUSINESS.value].count,
            ret.card_receipt_summary[CardKind.BUSINESS.value].tax) == (1, 200_000)
    assert ret.card_receipt_summary["현금영수증"].amount == 500_000
    assert all(isinstance(x.tax, int) and isinstance(x.amount, int) for x in ret.lines.values())
    assert any("가산세 별도 검토" in n for n in ret.notes)


def test_card_credit_annual_limit_and_payable_cap():
    """연 한도: 1천만 - 올해 기공제 9,950,000 = 잔여 50,000.
    카드매출 1,100,000 → 3번 1,000,000/100,000, 매입 10번 900,000/90,000 → ㉰ 10,000
    공제 = min(1,100,000×1.3%=14,300, 잔여 50,000, ㉰ 10,000) = 10,000 → 27번 0
    """
    txns = [sale_card(D, 1_100_000), buy_ti(D, 900_000, 90_000, C(G))]
    ret = compute_return(txns, client(), filing(), make_law(), POLICY, FilingSettings(card_credit_used_this_year=9_950_000))
    assert ret.line(Line.PAYABLE).tax == 10_000
    assert ret.line(Line.C_CARD_ISSUE).tax == 10_000
    assert ret.line(Line.FINAL).tax == 0

    # 잔여 한도가 더 작으면 잔여 한도까지: 9,995,000 사용 → 잔여 5,000
    ret2 = compute_return(txns, client(), filing(), make_law(), POLICY, FilingSettings(card_credit_used_this_year=9_995_000))
    assert ret2.line(Line.C_CARD_ISSUE).tax == 5_000
    assert ret2.line(Line.FINAL).tax == 5_000


def test_card_credit_not_for_corp_or_large_individual():
    txns = [sale_card(D, 11_000_000)]
    corp = compute_return(txns, client(taxpayer_type=TaxpayerType.CORPORATION), filing(), make_law(), POLICY)
    assert corp.line(Line.C_CARD_ISSUE).tax == 0
    big = compute_return(txns, client(prior_year_supply=1_000_000_001), filing(), make_law(), POLICY)
    assert big.line(Line.C_CARD_ISSUE).tax == 0
    # 기준 금액 이하(=10억)는 공제: 11,000,000 × 1.3% = 143,000
    edge = compute_return(txns, client(prior_year_supply=1_000_000_000), filing(), make_law(), POLICY)
    assert edge.line(Line.C_CARD_ISSUE).tax == 143_000


def test_missing_law_param_zero_and_review_item():
    """card_issue_credit.rate 가 없으면 19번 0원 + C001 검토항목(계산은 계속)."""
    txns = [sale_card(D, 11_000_000)]
    res = compute_return_detailed(txns, client(), filing(), make_law(card_issue_credit__rate=None), POLICY)
    assert res.ret.line(Line.C_CARD_ISSUE).tax == 0
    assert res.ret.line(Line.FINAL).tax == 1_000_000
    assert any(i.code == "C001_LAW_PARAM_MISSING" and "card_issue_credit.rate" in i.title for i in res.issues)


def test_card_credit_outside_law_period():
    """2027년 회차: 법 파라미터 적용기간(~2026-12-31) 밖 → 19번 0 + 경고."""
    txns = [sale_card(date(2027, 2, 1), 11_000_000)]
    res = compute_return_detailed(txns, client(), filing("2027-1F"), make_law(), POLICY)
    assert res.ret.line(Line.C_CARD_ISSUE).tax == 0
    assert any(i.code == "C001_LAW_PARAM_MISSING" for i in res.issues)


def test_prior_year_supply_unknown_warns():
    res = compute_return_detailed([sale_card(D, 1_100_000)], client(prior_year_supply=None), filing(), make_law(), POLICY)
    assert res.ret.line(Line.C_CARD_ISSUE).tax == 14_300  # 1,100,000 × 1.3%
    assert any(i.code == "C002_PRIOR_YEAR_SUPPLY_UNKNOWN" for i in res.issues)


def test_sales_line_mapping():
    """영세율 세금계산서 → 5번, 영세율 기타 → 6번, 기타매출 → 4번, 매입자발행 → 2번, 계산서 → 신고서 제외."""
    txns = [
        sale_ti(D, 5_000_000, 0, zero_rated=True),
        tx(Source.OTHER_SALES, Direction.SALES, DocType.NONE, D, 3_000_000, 0, zero_rated=True),
        tx(Source.OTHER_SALES, Direction.SALES, DocType.NONE, D, 2_000_000, 200_000),
        sale_ti(D, 1_000_000, 100_000, raw={"buyer_issued": True}),
        tx(Source.EINV_SALES, Direction.SALES, DocType.INVOICE, D, 7_000_000, 0),
        tx(Source.CASH_RECEIPT_SALES, Direction.SALES, DocType.CASH_RECEIPT, D, 100_000, 10_000),
    ]
    ret = compute_return(txns, client(), filing(), make_law(), POLICY)
    assert L(ret, Line.S_ZR_TAX_INVOICE) == (5_000_000, 0)
    assert L(ret, Line.S_ZR_OTHER) == (3_000_000, 0)
    assert L(ret, Line.S_OTHER) == (2_000_000, 200_000)
    assert L(ret, Line.S_BUYER_ISSUED) == (1_000_000, 100_000)
    assert L(ret, Line.S_CARD_CASH) == (100_000, 10_000)
    # 9번 = 5,000,000+3,000,000+2,000,000+1,000,000+100,000 = 11,100,000 / 310,000 (계산서 7백만 제외)
    assert L(ret, Line.S_TOTAL) == (11_100_000, 310_000)
    assert any("면세 계산서" in n for n in ret.notes)


def test_card_sale_duplicate_with_tax_invoice_excluded_from_line3():
    """같은 날 같은 금액(1,100,000) 세금계산서와 카드매출 → 카드분은 3번에서 제외(1번으로만 신고).
    발행세액공제 대상 금액에는 포함: 1,100,000 × 1.3% = 14,300.
    ㉰ = 100,000(1번) → 27번 = 100,000 - 14,300 = 85,700
    """
    txns = [sale_ti(D, 1_000_000, 100_000), sale_card(D, 1_100_000)]
    res = compute_return_detailed(txns, client(), filing(), make_law(), POLICY)
    ret = res.ret
    assert L(ret, Line.S_TAX_INVOICE) == (1_000_000, 100_000)
    assert L(ret, Line.S_CARD_CASH) == (0, 0)
    assert ret.line(Line.C_CARD_ISSUE).tax == 14_300
    assert ret.line(Line.FINAL).tax == 85_700
    assert len(res.meta["card_sales_dup_excluded_ids"]) == 1


def test_prelim_omitted_and_coverage_for_corp_final():
    """법인 2기 확정(예정신고함): 집계 10~12월.
    10월 매출 TI 10,000,000/1,000,000 → 1번
    8월 매출 TI 2,000,000/200,000 (예정신고 누락) → 7번
    8월 매출 TI 5,000,000/500,000 (예정신고 포함 id) → 제외
    5월 매출 → 집계기간 밖 제외
    9월 매입 TI 1,000,000/100,000 (누락) → 12번
    ㉰ = (1,000,000+200,000) - 100,000 = 1,100,000, 법인이라 19번 없음 → 27번 1,100,000
    """
    already = sale_ti(date(2026, 8, 20), 5_000_000, 500_000, approval_no="A")
    txns = [
        sale_ti(date(2026, 10, 5), 10_000_000, 1_000_000, approval_no="B"),
        sale_ti(date(2026, 8, 10), 2_000_000, 200_000, approval_no="C"),
        already,
        sale_ti(date(2026, 5, 10), 9_000_000, 900_000, approval_no="D"),
        buy_ti(date(2026, 9, 3), 1_000_000, 100_000, C(G), approval_no="E"),
    ]
    f = filing("2026-2F", filed_preliminary=True)
    assert (f.coverage_start, f.coverage_end) == (date(2026, 10, 1), date(2026, 12, 31))
    res = compute_return_detailed(txns, client(taxpayer_type=TaxpayerType.CORPORATION), f, make_law(), POLICY,
                                  prior_reported_ids={already.id})
    ret = res.ret
    assert L(ret, Line.S_TAX_INVOICE) == (10_000_000, 1_000_000)
    assert L(ret, Line.S_PRELIM_OMITTED) == (2_000_000, 200_000)
    assert L(ret, Line.S_TOTAL) == (12_000_000, 1_200_000)
    assert L(ret, Line.P_PRELIM_OMITTED) == (1_000_000, 100_000)
    assert L(ret, Line.P_TOTAL) == (1_000_000, 100_000)
    assert ret.line(Line.FINAL).tax == 1_100_000
    assert len(res.meta["out_of_range_ids"]) == 1
    assert res.meta["prelim_already_reported_ids"] == [already.id]


def test_adjustments_and_total_line_ignored():
    """수동조정: 18번 세액 10,000(전자신고세액공제 등), 8번 대손세액 -50,000, 9번(합계) 조정은 무시+경고.
    매출 TI 1,000,000/100,000 → 9번 세액 = 100,000 - 50,000 = 50,000
    ㉰ 50,000, 20번 = 18번 10,000 → 27번 40,000
    """
    st = FilingSettings(adjustments=[
        Adjustment(Line.C_OTHER, tax=10_000, note="전자신고세액공제"),
        Adjustment(Line.S_BAD_DEBT, tax=-50_000, note="대손"),
        Adjustment(Line.S_TOTAL, amount=1, tax=1, note="잘못된 조정"),
    ])
    res = compute_return_detailed([sale_ti(D, 1_000_000, 100_000)], client(), filing(), make_law(), POLICY, st)
    ret = res.ret
    assert ret.line(Line.S_TOTAL).tax == 50_000
    assert ret.line(Line.C_TOTAL).tax == 10_000
    assert ret.line(Line.FINAL).tax == 40_000
    assert any(i.code == "C004_ADJUSTMENT_IGNORED" for i in res.issues)


def test_card_nondeductible_and_unclassified_purchases():
    """카드 불공제(비영업용 승용차) 330,000 → 14번 제외. 미분류 세금계산서 → 일반매입(10번)으로 보고 경고."""
    txns = [
        buy_card(D, 300_000, 30_000, C(PurchaseCategory.NON_DEDUCTIBLE, non_deductible_reason=NonDeductibleReason.PASSENGER_CAR)),
        buy_ti(D, 1_000_000, 100_000, None),
    ]
    res = compute_return_detailed(txns, client(), filing(), make_law(), POLICY)
    assert L(res.ret, Line.P_OTHER_DEDUCTIBLE) == (0, 0)
    assert L(res.ret, Line.P_TI_GENERAL) == (1_000_000, 100_000)
    assert L(res.ret, Line.P_NON_DEDUCTIBLE) == (0, 0)  # 카드 불공제는 16번에 넣지 않음
    assert any(i.code == "C005_UNCLASSIFIED_PURCHASE" for i in res.issues)


def test_nondeductible_fixed_asset_ti_goes_to_line11_and_16():
    """비영업용 승용차 구입 세금계산서 30,000,000/3,000,000 (불공제, 고정자산 키워드 '차량') → 11번과 16번에 모두 기재."""
    t = buy_ti(D, 30_000_000, 3_000_000,
               C(PurchaseCategory.NON_DEDUCTIBLE, non_deductible_reason=NonDeductibleReason.PASSENGER_CAR), item="승용차량 구입")
    ret = compute_return([t], client(), filing(), make_law(), POLICY)
    assert L(ret, Line.P_TI_FIXED) == (30_000_000, 3_000_000)
    assert L(ret, Line.P_NON_DEDUCTIBLE) == (30_000_000, 3_000_000)
    assert ret.line(Line.P_NET).tax == 0


def test_deemed_input_restaurant_individual_with_limit():
    """개인 음식점, 2기 확정(6개월). 카드매출 110,000,000 → 과세표준 100,000,000 (2억 이하 → 9/109, 1억 이하 한도율 75%).
    (a) 계산서 농산물 10,900,000: 한도 100,000,000×75% = 75,000,000 이내 → 10,900,000 × 9/109 = 900,000
    (b) 계산서 80,000,000: 한도 75,000,000 → 75,000,000 × 9/109 = 6,192,660.55… → 6,192,660 (절사)
    """
    cl = client(industry="한식 음식점", deemed_input_type="restaurant")
    sales = sale_card(D, 110_000_000)
    deemed = lambda amt: tx(Source.EINV_PURCHASE, Direction.PURCHASE, DocType.INVOICE, D, amt, 0,
                            classification=C(PurchaseCategory.DEEMED_INPUT))
    r1 = compute_return([sales, deemed(10_900_000)], cl, filing(), make_law(), POLICY)
    assert r1.other_deductible_breakdown["의제매입세액"].tax == 900_000
    assert r1.line(Line.P_OTHER_DEDUCTIBLE).tax == 900_000
    r2 = compute_return([sales, deemed(80_000_000)], cl, filing(), make_law(), POLICY)
    assert r2.other_deductible_breakdown["의제매입세액"].amount == 75_000_000
    assert r2.other_deductible_breakdown["의제매입세액"].tax == 6_192_660


def test_deemed_input_after_rate_expiry_falls_back():
    """2027년: 9/109 키 적용기간 종료 → 8/108 폴백. 1기 확정 6개월, 과세표준 100,000,000.
    10,800,000 × 8/108 = 800,000"""
    cl = client(deemed_input_type="restaurant")
    txns = [sale_card(date(2027, 3, 1), 110_000_000),
            tx(Source.EINV_PURCHASE, Direction.PURCHASE, DocType.INVOICE, date(2027, 3, 1), 10_800_000, 0,
               classification=C(PurchaseCategory.DEEMED_INPUT))]
    ret = compute_return(txns, cl, filing("2027-1F"), make_law(), POLICY)
    assert ret.other_deductible_breakdown["의제매입세액"].tax == 800_000


def test_deemed_input_prelim_has_no_limit_and_missing_limit_warns():
    """예정신고(P)는 한도 없이 공제: 매출 0이어도 1,090,000 × 9/109 = 90,000."""
    cl = client(deemed_input_type="restaurant")
    t = tx(Source.EINV_PURCHASE, Direction.PURCHASE, DocType.INVOICE, date(2026, 7, 3), 1_090_000, 0,
           classification=C(PurchaseCategory.DEEMED_INPUT))
    ret = compute_return([t], cl, filing("2026-2P", filed_preliminary=True), make_law(), POLICY)
    assert ret.other_deductible_breakdown["의제매입세액"].tax == 90_000
    # 확정인데 한도 키가 없으면 전액 공제 + 경고
    law = make_law(**{"deemed_input__limit__individual_restaurant_le_100m": None})
    res = compute_return_detailed([t], cl, filing("2026-2F"), law, POLICY)
    assert res.ret.other_deductible_breakdown["의제매입세액"].tax == 90_000
    assert any(i.code == "C003_DEEMED_INPUT_LIMIT" for i in res.issues)


def test_refund_and_unverified_note():
    """매입이 더 많으면 27번 음수(환급). 미검증 파라미터 사용 사실은 notes 에 기록."""
    law = make_law(**{"vat.rate": [{"value": 0.10, "verified": False}]})
    txns = [sale_ti(D, 1_000_000, 100_000), buy_ti(D, 5_000_000, 500_000, C(G))]
    ret = compute_return(txns, client(), filing(), law, POLICY)
    assert ret.line(Line.FINAL).tax == -400_000
    assert any("미검증 법 파라미터" in n and "vat.rate" in n for n in ret.notes)

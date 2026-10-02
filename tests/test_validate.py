"""검증(V001~V020) 테스트."""

from datetime import date

from calc_fixtures import POLICY, C, buy_card, buy_ti, client, filing, make_law, sale_card, sale_ti, tx

from taxauto.compute.vat_return import compute_return_detailed
from taxauto.models import (
    DecidedBy, Direction, DocType, ExclusionReason, Line, LineValue, NonDeductibleReason, PurchaseCategory,
    ReviewStatus, Severity, Source, TaxpayerType, VatReturn,
)
from taxauto.registry import FilingSettings
from taxauto.validate import checks as V
from taxauto.validate.stage import find_previous_sales, previous_period_code

D = date(2026, 8, 5)
P = PurchaseCategory


def ci(txns, cl=None, f=None, law=None, **kw):
    cl = cl or client()
    f = f or filing()
    law = law or make_law()
    res = compute_return_detailed(txns, cl, f, law, POLICY, kw.pop("settings", FilingSettings()))
    kw.setdefault("ret", res.ret)
    return V.CheckInput(client=cl, filing=f, txns=txns, law=law, policy=POLICY, compute_meta=res.meta, **kw)


def codes(items):
    return [i.code for i in items]


def test_v001_ledger_and_return_reconciliation():
    t = sale_ti(D, 1_000_000, 100_000)
    led = tx(Source.WEHAGO_LEDGER, Direction.SALES, DocType.TAX_INVOICE, D, 900_000, 90_000)
    c = ci([t, led])
    items = V.v001_reconcile_wehago(c)
    assert codes(items) == ["V001_LEDGER_MISMATCH"] and items[0].tax_impact == 10_000
    # 위하고 신고서 라인 대사: 1번 세액이 다르면 차단
    wr = VatReturn(client_id="C001", period="2026-2F", lines={
        Line.S_TAX_INVOICE.name: LineValue(1_000_000, 90_000), Line.S_CARD_CASH.name: LineValue(0, 0)})
    c.wehago_return = wr
    items = V.v001_reconcile_wehago(c)
    blk = [i for i in items if i.code == "V001_RETURN_MISMATCH"]
    assert len(blk) == 1 and blk[0].severity == Severity.BLOCKER and "1번" in blk[0].title
    # 위하고 전표가 원천과 같으면 지적 없음
    led2 = tx(Source.WEHAGO_LEDGER, Direction.SALES, DocType.TAX_INVOICE, D, 1_000_000, 100_000)
    assert V.v001_reconcile_wehago(ci([t, led2])) == []


def test_v002_vat_mismatch():
    good = sale_ti(D, 1_000_005, 100_000)       # 100,000.5 → 허용오차 1원
    bad = buy_ti(D, 1_000_000, 90_000, C(P.GENERAL))
    zr = sale_ti(D, 1_000_000, 100_000, zero_rated=True)
    items = V.v002_vat_amount(ci([good, bad, zr]))
    assert len(items) == 1 and set(items[0].tx_ids) == {bad.id, zr.id}


def test_v003_duplicate_approval():
    a = sale_ti(D, 1_000_000, 100_000, approval_no="2026080541000012")
    b = sale_ti(D, 1_000_000, 100_000, approval_no="2026080541000012", row_no=99, source_file="other.xlsx")
    b.id = "different-id"
    items = V.v003_duplicate_approval(ci([a, b]))
    assert codes(items) == ["V003_DUP_APPROVAL"] and items[0].tax_impact == -100_000


def test_v004_closed_seller():
    t = buy_ti(D, 1_000_000, 100_000, C(P.GENERAL), counterparty_closed_on=date(2026, 7, 1))
    items = V.v004_closed_seller(ci([t]))
    assert items and items[0].tax_impact == 100_000


def test_v005_card_excluded_summary():
    a = buy_card(D, 10_000, 1_000, C(P.NOT_APPLICABLE, exclusion_reason=ExclusionReason.SIMPLE_TAXPAYER_SELLER))
    b = buy_card(D, 20_000, 2_000, C(P.NOT_APPLICABLE, exclusion_reason=ExclusionReason.NON_DEDUCTIBLE_CARD))
    items = V.v005_card_excluded_summary(ci([a, b]))
    assert items[0].severity == Severity.INFO and "2건" in items[0].title and "3,000" in items[0].title


def test_v006_card_ti_duplicate_auto_and_suspect():
    # 강한 매칭(유일 후보) → 자동 제외 알림
    ti = sale_ti(D, 1_000_000, 100_000)
    card = sale_card(date(2026, 8, 7), 1_100_000)
    items = V.v006_card_ti_duplicate(ci([ti, card]))
    assert codes(items) == ["V006_CARD_TI_DUP_EXCLUDED"]
    # 같은 금액 세금계산서 2장, 카드 1건 → 복수 후보 → 의심만(자동 제외 안 함)
    ti2 = sale_ti(date(2026, 8, 6), 1_000_000, 100_000)
    items = V.v006_card_ti_duplicate(ci([ti, ti2, card]))
    assert codes(items) == ["V006_CARD_TI_DUP_SUSPECT"]


def test_v007_sales_ti_late_issue_and_transmit():
    """8/5 공급 → 발급기한 9/10(목).
    지연발급: 9/20 발급(확정기한 이내) → 2,000,000 × 1% = 20,000 (전송 가산세 중복 없음)
    미발급: 2027-02-01 발급(2기 확정기한 2027-01-25 후) → 1,000,000 × 2% = 20,000
    지연전송: 8/5 발급, 8/20 전송 → 3,000,000 × 0.3% = 9,000
    """
    late = sale_ti(D, 2_000_000, 200_000, issue_date=date(2026, 9, 20), transmit_date=date(2026, 9, 25))
    never = sale_ti(D, 1_000_000, 100_000, issue_date=date(2027, 2, 1))
    trans = sale_ti(D, 3_000_000, 300_000, issue_date=D, transmit_date=date(2026, 8, 20))
    ok = sale_ti(D, 4_000_000, 400_000, issue_date=date(2026, 9, 10), transmit_date=date(2026, 9, 11))
    items = {i.code: i for i in V.v007_sales_ti_timing(ci([late, never, trans, ok]))}
    assert items["V007_SALES_TI_LATE_ISSUE"].tax_impact == 20_000
    assert items["V007_SALES_TI_LATE_ISSUE"].tx_ids == [late.id]
    assert items["V007_SALES_TI_NOT_ISSUED"].tax_impact == 20_000
    assert items["V007_SALES_TI_LATE_TRANSMIT"].tax_impact == 9_000
    assert len(items) == 3


def test_v008_late_receipt_buckets():
    """8/5 공급, 발급기한 9/10, 2기 확정기한 2027-01-25.
    10/1 수취 → 지연수취 1,000,000×0.5% = 5,000 / 2027-03-01 수취 → 경정청구 대상"""
    a = buy_ti(D, 1_000_000, 100_000, C(P.GENERAL), issue_date=date(2026, 10, 1))
    b = buy_ti(D, 2_000_000, 200_000, C(P.GENERAL), issue_date=date(2027, 3, 1))
    items = {i.code: i for i in V.v008_purchase_ti_late_receipt(ci([a, b]))}
    assert items["V008_LATE_RECEIPT"].tax_impact == 5_000
    assert "V008_RECEIPT_AFTER_DEADLINE" in items


def test_v009_sales_change_monthly_normalized():
    """이번 6개월 과세표준 12,000,000(월 2,000,000) vs 전기 3개월 9,000,000(월 3,000,000) → -33.3% 경고."""
    t = sale_ti(D, 12_000_000, 1_200_000)
    items = V.v009_sales_change(ci([t], previous_sales=9_000_000, previous_months=3))
    assert items and "-33.3%" in items[0].title
    assert V.v009_sales_change(ci([t], previous_sales=11_000_000, previous_months=6)) == []


def test_v010_v011_fixed_asset_and_needs_review():
    fa = buy_ti(D, 2_000_000, 200_000, C(P.FIXED_ASSET, needs_review=True))
    nd = buy_card(D, 300_000, 30_000, C(P.NON_DEDUCTIBLE, non_deductible_reason=NonDeductibleReason.ENTERTAINMENT,
                                       needs_review=True, rule_id="entertainment", note="골프 - 접대비"))
    ai = buy_card(D, 100_000, 10_000, C(P.GENERAL, decided_by=DecidedBy.LLM, needs_review=True, rule_id="llm"))
    c = ci([fa, nd, ai])
    assert codes(V.v010_fixed_asset(c)) == ["V010_FIXED_ASSET"]
    items = V.v011_needs_review(c)
    titles = sorted(i.title for i in items)
    assert len(items) == 2 and any(t.startswith("AI 판정") for t in titles)
    ent = next(i for i in items if "골프" in i.title)
    assert ent.tax_impact == -30_000


def test_v012_refund_v013_prelim_notice():
    c = ci([buy_ti(D, 5_000_000, 500_000, C(P.GENERAL))])
    assert codes(V.v012_refund(c)) == ["V012_REFUND"]
    assert codes(V.v013_prelim_notice(c)) == ["V013_PRELIM_NOTICE_ZERO"]
    c2 = ci([sale_ti(D, 1, 0)], f=filing(notice=300_000))
    assert V.v013_prelim_notice(c2) == []
    corp_prelim = ci([sale_ti(D, 1, 0)], f=filing(filed_preliminary=True))
    assert V.v013_prelim_notice(corp_prelim) == []


def test_v014_unverified_and_v015_parse_issues():
    law = make_law(**{"vat.rate": [{"value": 0.10, "verified": False}]})
    c = ci([sale_card(D, 11_000)], law=law)
    assert "vat.rate" in V.v014_unverified_law(c)[0].detail
    c.parse_issues = [{"source_file": "a.xlsx", "row_no": 3, "message": "날짜 형식 오류", "severity": "차단"}]
    assert V.v015_parse_issues(c)[0].severity == Severity.BLOCKER


def test_v016_missing_data():
    empty = ci([])
    assert V.v016_missing_data(empty)[0].severity == Severity.BLOCKER
    rest = ci([sale_ti(D, 1_000_000, 100_000)], cl=client(industry="한식 음식점"))
    assert codes(V.v016_missing_data(rest)) == ["V016_NO_CARD_SALES"]


def test_v017_v018_v019():
    assert V.v017_simple_taxpayer(ci([], cl=client(taxpayer_type=TaxpayerType.SIMPLE)))[0].severity == Severity.BLOCKER
    mixed = ci([tx(Source.EINV_SALES, Direction.SALES, DocType.INVOICE, D, 1_000_000, 0)])
    assert codes(V.v018_mixed_business(mixed)) == ["V018_COMMON_INPUT_TAX"]
    zr = ci([sale_ti(D, 5_000_000, 0, zero_rated=True)])
    assert codes(V.v019_zero_rated(zr)) == ["V019_ZERO_RATED_DOCS"]


def test_v020_out_of_coverage_and_prelim_omitted():
    f = filing("2026-2F", filed_preliminary=True)
    om = sale_ti(date(2026, 8, 10), 2_000_000, 200_000)
    old = sale_ti(date(2026, 3, 10), 1_000_000, 100_000)
    cur = sale_ti(date(2026, 11, 1), 1_000_000, 100_000)
    items = {i.code: i for i in V.v020_out_of_coverage(ci([om, old, cur], cl=client(taxpayer_type=TaxpayerType.CORPORATION), f=f))}
    assert items["V020_PRELIM_OMITTED"].tax_impact == 200_000
    assert items["V020_OUT_OF_COVERAGE"].tx_ids == [old.id]


def test_run_checks_isolates_errors_and_ids_stable():
    def boom(_):
        raise RuntimeError("x")

    c = ci([sale_ti(D, 1_000_000, 100_000)])
    items = V.run_checks(c, checks=[boom, V.v013_prelim_notice])
    assert codes(items) == ["V999_CHECK_ERROR", "V013_PRELIM_NOTICE_ZERO"]
    again = V.run_checks(c, checks=[V.v013_prelim_notice])
    assert items[1].id == again[0].id  # 같은 원인 → 같은 id (사람 처리결과 병합 보존)


def test_previous_period_lookup(tmp_path):
    assert previous_period_code("2026-2F") == "2026-2P"
    assert previous_period_code("2026-2P") == "2026-1F"
    assert previous_period_code("2026-1P") == "2025-2F"
    import json

    root = tmp_path / "2026-1F" / "C001"
    root.mkdir(parents=True)
    r = VatReturn(client_id="C001", period="2026-1F", lines={Line.S_TOTAL.name: LineValue(30_000_000, 3_000_000)})
    (root / "return.json").write_text(json.dumps(r.to_dict()), encoding="utf-8")
    (root / "compute_meta.json").write_text(json.dumps({"months": 6}), encoding="utf-8")
    assert find_previous_sales(tmp_path, "C001", "2026-2F") == (30_000_000, 6)


def test_validate_stage_end_to_end(tmp_path):
    """compute → validate 단계를 작업공간 파일로 실행. 사람이 처리한 항목 상태는 재실행 후에도 유지."""
    import logging

    from taxauto.compute import stage as cstage
    from taxauto.context import RunContext
    from taxauto.validate import stage as vstage
    from taxauto.workspace import Workspace

    ws = Workspace(tmp_path / "data" / "2026-2F" / "C001")
    ws.save_transactions([sale_ti(D, 1_000_000, 100_000, issue_date=date(2026, 9, 20)),
                          buy_ti(D, 5_000_000, 500_000, C(P.GENERAL))])
    ctx = RunContext(client=client(), filing=filing(), workspace=ws, law=make_law(card_issue_credit__rate=None), policy=POLICY,
                     inbox_dir=tmp_path / "inbox", config_dir=tmp_path / "cfg", clients_dir=tmp_path / "clients",
                     today=date(2026, 10, 2), log=logging.getLogger("t"))
    assert cstage.run(ctx).ok
    assert ws.load_return().line(Line.FINAL).tax == -400_000
    assert vstage.run(ctx).ok
    items = ws.load_review()
    cs = codes(items)
    assert "V012_REFUND" in cs and "V007_SALES_TI_LATE_ISSUE" in cs
    first = items[0]
    first.status, first.resolution = ReviewStatus.ACCEPTED, "확인함"
    ws.save_review(items, merge=False)
    vstage.run(ctx)
    again = {i.id: i for i in ws.load_review()}
    assert again[first.id].status == ReviewStatus.ACCEPTED

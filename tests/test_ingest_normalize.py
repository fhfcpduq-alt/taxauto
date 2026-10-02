"""normalize.py: 종류 판별·방향 이중확인·금액 분리·표시값·중복제거·기간 밖 거래·위하고 신고서."""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import openpyxl
import pytest

from fixtures.make_fixtures import CP, FACTS, build_inbox, example_clients, make_ctx
from taxauto.ingest import normalize
from taxauto.ingest.normalize import load_columns, parse_file
from taxauto.models import CardKind, Direction, DocType, Filing, Severity, Source, TaxPeriod

CL = {c.id: c for c in example_clients()}
F2P = {
    "C001": Filing("C001", TaxPeriod.parse("2026-2P"), date(2026, 7, 1), date(2026, 9, 30), date(2026, 10, 26)),
    "C002": Filing("C002", TaxPeriod.parse("2026-2P"), date(2026, 7, 1), date(2026, 9, 30), date(2026, 10, 26), filed_preliminary=True),
}


@pytest.fixture(scope="module")
def inbox(tmp_path_factory):
    root = tmp_path_factory.mktemp("inbox")
    build_inbox(root, "2026-2P")
    return root / "2026-2P"


def _parse(inbox, cid, name):
    return parse_file(inbox / cid / name, CL[cid], F2P[cid], load_columns())


def _write(path: Path, rows):
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    wb.save(path)
    return path


# --- 종류별 해석 ---------------------------------------------------------

def test_etax_sales_flags(inbox):
    r = _parse(inbox, "C002", "전자세금계산서_매출.xlsx")
    assert r.kinds == ["etax_sales"] and not r.issues
    by = {t.approval_no: t for t in r.transactions}
    assert len(by) == 6
    assert all(t.source == Source.ETAX_SALES and t.direction == Direction.SALES for t in by.values())
    zr = by[FACTS["C002"]["zero_rated_approval"]]
    assert zr.zero_rated and zr.vat == 0 and zr.supply_amount == 20_000_000
    am = by[FACTS["C002"]["amended_approval"]]
    assert am.is_amended and am.supply_amount == -1_000_000 and am.vat == -100_000 and am.total == -1_100_000
    late = by[FACTS["C002"]["late_issue_approval"]]
    assert late.tx_date == date(2026, 7, 15) and late.issue_date == date(2026, 8, 14) and late.transmit_date == date(2026, 8, 15)
    t = by[FACTS["C002"]["wehago_diff"]["approval"]]
    assert t.counterparty_biz_no == CP["가나전자"][0] and t.counterparty_name == "(주)가나전자"
    assert t.item == "PCB 모듈" and t.row_no == 8  # 제목 3행 + 헤더(4행) + 7월 3건 다음
    assert not any(x.zero_rated for x in by.values() if x is not zr)


def test_etax_purchase_two_row_header(inbox):
    r = _parse(inbox, "C001", "전자세금계산서_매입.xlsx")
    assert r.kinds == ["etax_purchase"]
    t = r.transactions[0]
    assert (t.direction, t.doc_type, t.supply_amount, t.vat) == (Direction.PURCHASE, DocType.TAX_INVOICE, 1_500_000, 150_000)
    assert t.counterparty_biz_no == CP["대한식자재"][0] and t.counterparty_name == "(주)대한식자재"


def test_einv_purchase_is_tax_free(inbox):
    r = _parse(inbox, "C001", "전자계산서_매입.xlsx")
    assert r.kinds == ["einv_purchase"]
    assert all(t.source == Source.EINV_PURCHASE and t.doc_type == DocType.INVOICE and t.vat == 0 for t in r.transactions)


def test_card_sales_monthly_total_split(inbox):
    r = _parse(inbox, "C001", "신용카드매출자료.xlsx")
    assert r.kinds == ["card_sales"]
    assert len(r.transactions) == 3  # 합계행 제외
    t = r.transactions[0]
    assert t.tx_date == date(2026, 7, 31) and "월합계" in t.memo
    assert t.vat == 33_000_000 * 10 // 110 and t.supply_amount == 33_000_000 - t.vat and t.total == 33_000_000


def test_vat_inclusive_split_truncates():
    assert normalize._split_vat_inclusive(10_000) == (9_091, 909)
    assert normalize._split_vat_inclusive(-10_000) == (-9_091, -909)
    assert normalize._split_vat_inclusive(11_000) == (10_000, 1_000)


def test_cash_receipt_sales_cancel(inbox):
    r = _parse(inbox, "C001", "현금영수증_매출내역.xlsx")
    assert r.kinds == ["cash_receipt_sales"]
    cancel = [t for t in r.transactions if "취소" in t.memo]
    assert len(cancel) == 1 and cancel[0].total == -22_000 and cancel[0].vat == -2_000
    assert cancel[0].tx_date == date(2026, 8, 17)


def test_pg_sales(inbox):
    r = _parse(inbox, "C001", "판매대행_매출자료.xlsx")
    assert r.kinds == ["pg_sales"]
    assert {t.counterparty_name for t in r.transactions} == {"배달앱A(주)", "배달앱B(유)"}
    t = next(t for t in r.transactions if t.counterparty_name == "배달앱B(유)")
    assert (t.supply_amount, t.vat) == (2_000_000, 200_000) and t.source == Source.PG_SALES


def test_card_purchase_fields_and_masking(inbox):
    r = _parse(inbox, "C001", "사업용신용카드_매입내역.xlsx")
    assert r.kinds == ["card_purchase"]
    simple = next(t for t in r.transactions if t.counterparty_name == "동네분식")
    assert simple.card_kind == CardKind.BUSINESS
    assert simple.counterparty_tax_type == "간이과세자" and simple.deductible_flag_from_source is False
    assert simple.card_no_masked == "****-****-****-1111"
    assert simple.merchant_category == "분식"
    assert all("1234-56" not in json.dumps(t.raw, ensure_ascii=False) for t in r.transactions)
    office = next(t for t in r.transactions if t.counterparty_name == "(주)오피스몰")
    assert office.deductible_flag_from_source is True and (office.supply_amount, office.vat) == (100_000, 10_000)


def test_card_purchase_golf_and_fuel(inbox):
    r = _parse(inbox, "C002", "사업용신용카드_매입내역.xlsx")
    golf = next(t for t in r.transactions if "컨트리" in t.counterparty_name)
    assert golf.merchant_category == "골프장 운영업" and golf.vat == 80_000
    fuel = next(t for t in r.transactions if "주유소" in t.counterparty_name)
    assert fuel.merchant_category == "주유소"


def test_cash_receipt_purchase_and_paper(inbox):
    r = _parse(inbox, "C001", "현금영수증_매입내역.xlsx")
    assert r.kinds == ["cash_receipt_purchase"] and r.transactions[0].direction == Direction.PURCHASE
    r = _parse(inbox, "C001", "종이세금계산서_입력.xlsx")
    assert r.kinds == ["paper_invoice"]
    t = r.transactions[0]
    assert (t.source, t.direction, t.doc_type, t.supply_amount, t.vat) == (
        Source.PAPER_TAX_INVOICE, Direction.PURCHASE, DocType.TAX_INVOICE, 300_000, 30_000)


def test_wehago_ledger(inbox):
    r = _parse(inbox, "C002", "위하고_매입매출전표.xlsx")
    assert r.kinds == ["wehago_ledger"]
    assert all(t.source == Source.WEHAGO_LEDGER for t in r.transactions)
    zr = next(t for t in r.transactions if t.supply_amount == 20_000_000)
    assert zr.zero_rated and zr.direction == Direction.SALES
    card = next(t for t in r.transactions if t.doc_type == DocType.CARD)
    assert card.direction == Direction.PURCHASE and card.tx_date == date(2026, 8, 5)
    diff = [t for t in r.transactions if t.supply_amount == 5_500_000]
    assert len(diff) == 1  # 의도적 대사 차이
    bul = next(t for t in r.transactions if "54" in t.raw.get("유형", ""))
    assert bul.direction == Direction.PURCHASE and bul.doc_type == DocType.TAX_INVOICE
    assert len(r.transactions) == 11


def test_welfare_card(tmp_path):
    p = _write(tmp_path / "화물운전자복지카드_사용내역.xlsx", [
        ["화물운전자 복지카드 사용내역"],
        ["승인일자", "카드번호", "가맹점사업자번호", "가맹점명", "공급가액", "부가세", "합계"],
        ["2026-08-01", "1111-2222-3333-4444", "214-81-00146", "OO주유소", 100_000, 10_000, 110_000],
    ])
    r = parse_file(p, CL["C002"], F2P["C002"])
    assert r.kinds == ["welfare_card"]
    t = r.transactions[0]
    assert t.card_kind == CardKind.WELFARE and t.source == Source.CARD_PURCHASE and t.card_no_masked.endswith("4444")


# --- 방향 이중확인 -------------------------------------------------------

def _etax_rows(sup, buy, title="전자세금계산서 목록조회"):
    return [[title], ["작성일자", "승인번호", "공급자사업자등록번호", "상호", "공급받는자사업자등록번호", "상호", "공급가액", "세액"],
            ["2026-07-01", "A-1", sup, "갑", buy, "을", 1000, 100]]


def test_direction_from_biz_no_when_file_ambiguous(tmp_path):
    me = CL["C002"].biz_no
    p = _write(tmp_path / "목록.xlsx", _etax_rows("1248100038", me))   # 제목·파일명에 매출/매입 없음
    r = parse_file(p, CL["C002"], F2P["C002"])
    t = r.transactions[0]
    assert t.direction == Direction.PURCHASE and t.source == Source.ETAX_PURCHASE
    assert t.counterparty_biz_no == "1248100038"
    assert not [i for i in r.issues if i.severity != Severity.INFO]


def test_direction_conflict_warns_and_uses_biz_no(tmp_path):
    me = CL["C002"].biz_no
    p = _write(tmp_path / "전자세금계산서_매입.xlsx", _etax_rows(me, "1248100038"))  # 파일은 매입인데 공급자=본인
    r = parse_file(p, CL["C002"], F2P["C002"])
    t = r.transactions[0]
    assert t.direction == Direction.SALES and t.source == Source.ETAX_SALES
    assert any("방향" in i.message and i.severity == Severity.WARN for i in r.issues)


def test_foreign_rows_excluded(tmp_path):
    p = _write(tmp_path / "전자세금계산서_매출.xlsx", _etax_rows("1248100038", "1358100057"))
    r = parse_file(p, CL["C002"], F2P["C002"])
    assert r.transactions == []
    assert any("무관한 행" in i.message for i in r.issues)


def test_rrn_counterparty_masked(tmp_path):
    me = CL["C002"].biz_no
    p = _write(tmp_path / "전자세금계산서_매출.xlsx", _etax_rows(me, "900101-1234567"))
    r = parse_file(p, CL["C002"], F2P["C002"])
    t = r.transactions[0]
    assert t.counterparty_biz_no == "" and "주민등록번호" in t.memo
    dumped = json.dumps(t.to_dict(), ensure_ascii=False)
    assert "1234567" not in dumped


# --- 기타 견고성 ---------------------------------------------------------

def test_unknown_format_is_issue_not_error(tmp_path):
    p = _write(tmp_path / "메모.xlsx", [["아무", "표"], ["1", "2"]])
    r = parse_file(p, CL["C001"], F2P["C001"])
    assert r.transactions == [] and "알 수 없는 형식" in r.issues[0].message


def test_bad_row_is_issue(tmp_path):
    p = _write(tmp_path / "현금영수증_매출.xlsx", [
        ["현금영수증 매출내역"], ["매출일시", "총금액", "승인번호"],
        ["2026-07-01", "11,000", "1"], ["언젠가", "22,000", "2"], ["2026-07-02", "abc", "3"],
    ])
    r = parse_file(p, CL["C001"], F2P["C001"])
    assert len(r.transactions) == 1 and (r.transactions[0].supply_amount, r.transactions[0].vat) == (10_000, 1_000)
    assert sorted(i.row_no for i in r.issues) == [4, 5]


def test_tax_free_flag_total_only(tmp_path):
    p = _write(tmp_path / "신용카드_매출.xlsx", [
        ["신용카드 매출 내역(카드사)"], ["승인일자", "승인금액", "과세구분"],
        ["2026-07-01", 11_000, "과세"], ["2026-07-02", 5_000, "면세"],
    ])
    r = parse_file(p, CL["C001"], F2P["C001"])
    a, b = r.transactions
    assert (a.supply_amount, a.vat) == (10_000, 1_000)
    assert (b.supply_amount, b.vat) == (5_000, 0)


def test_csv_cp949_card_purchase(tmp_path):
    p = tmp_path / "사업용카드.csv"
    p.write_bytes("사업용 신용카드 사용내역\n승인일자,가맹점명,가맹점사업자번호,합계\n20260705,문구점,101-25-00096,\"22,000\"\n".encode("cp949"))
    r = parse_file(p, CL["C001"], F2P["C001"])
    t = r.transactions[0]
    assert r.kinds == ["card_purchase"] and (t.supply_amount, t.vat) == (20_000, 2_000)
    assert "추정" in t.memo or "10/110" in t.memo


def test_dedupe_and_out_of_coverage(tmp_path, inbox):
    a = inbox / "C002" / "전자세금계산서_매출.xlsx"
    b = tmp_path / "전자세금계산서_매출_사본.xlsx"
    shutil.copy(a, b)
    f = Filing("C002", TaxPeriod.parse("2026-2P"), date(2026, 8, 1), date(2026, 9, 30), date(2026, 10, 26))
    txns, issues, _ = normalize.normalize_files([a, b], CL["C002"], f, load_columns())
    assert len(txns) == 6  # 사본 6건 제거
    assert any("중복 거래 6건" in i.message for i in issues)
    out = [i for i in issues if "집계기간" in i.message]
    assert out and out[0].severity == Severity.INFO and "3건" in out[0].message
    assert any(t.tx_date < f.coverage_start for t in txns)  # 버리지 않고 유지


def test_wehago_return_parser(tmp_path):
    p = _write(tmp_path / "위하고_부가가치세신고서.xlsx", [
        ["일반과세자 부가가치세 신고서"],
        ["구분", "", "번호", "금액", "세율", "세액"],
        ["과세표준및매출세액", "과세 세금계산서 발급분", "(1)", "40,000,000", "10/100", "4,000,000"],
        ["", "과세 신용카드·현금영수증 발행분", "(3)", 1_000_000, "10/100", 100_000],
        ["", "영세율 세금계산서 발급분", "(5)", 20_000_000, "0/100", 0],
        ["매입세액", "세금계산서 수취분 일반매입", "(10)", 10_000_000, "", 1_000_000],
        ["", "차가감하여 납부할 세액", "(27)", "", "", 3_100_000],
    ])
    r = parse_file(p, CL["C002"], F2P["C002"])
    wr = r.wehago_return
    assert wr is not None and wr.computed_by == "wehago" and r.transactions == []
    assert (wr.lines["S_TAX_INVOICE"].amount, wr.lines["S_TAX_INVOICE"].tax) == (40_000_000, 4_000_000)
    assert wr.lines["S_ZR_TAX_INVOICE"].amount == 20_000_000
    assert wr.lines["P_TI_GENERAL"].tax == 1_000_000
    assert wr.lines["FINAL"].tax == 3_100_000


def test_wehago_return_by_labels(tmp_path):
    p = _write(tmp_path / "신고서.xlsx", [
        ["부가가치세신고서"],
        ["과세", "세금계산서발급분", 1_000, 100],
        ["영세율", "세금계산서발급분", 500, 0],
        ["", "공제받지못할매입세액", 300, 30],
    ])
    wr = parse_file(p, CL["C002"], F2P["C002"]).wehago_return
    assert wr.lines["S_TAX_INVOICE"].amount == 1_000
    assert wr.lines["S_ZR_TAX_INVOICE"].amount == 500
    assert wr.lines["P_NON_DEDUCTIBLE"].tax == 30


# --- 단계 실행 -----------------------------------------------------------

def test_run_stage_writes_workspace(tmp_path):
    from taxauto.ingest import collect

    build_inbox(tmp_path / "inbox", "2026-2P", clients=["C002"])
    ctx = make_ctx(tmp_path, "C002", "2026-2P")
    assert collect.run(ctx).ok
    res = normalize.run(ctx)
    assert res.ok and res.counts["transactions"] > 0
    txns = ctx.workspace.load_transactions()
    assert len(txns) == res.counts["transactions"]
    assert {t.source for t in txns} >= {Source.ETAX_SALES, Source.ETAX_PURCHASE, Source.CARD_PURCHASE, Source.WEHAGO_LEDGER}
    assert isinstance(ctx.workspace.load_parse_issues(), list)
    assert all(isinstance(t.supply_amount, int) and isinstance(t.vat, int) for t in txns)


def test_2f_individual_six_months_and_late_purchase(tmp_path):
    build_inbox(tmp_path / "inbox", "2026-2F")
    from taxauto.ingest import collect

    c1 = make_ctx(tmp_path, "C001", "2026-2F")
    assert c1.filing.coverage_start == date(2026, 7, 1)
    collect.run(c1)
    normalize.run(c1)
    months = {t.tx_date.month for t in c1.workspace.load_transactions()}
    assert months == {7, 8, 9, 10, 11, 12}

    c2 = make_ctx(tmp_path, "C002", "2026-2F")
    collect.run(c2)
    normalize.run(c2)
    txns = c2.workspace.load_transactions()
    late = [t for t in txns if t.approval_no == FACTS["C002"]["prelim_omitted_purchase_approval"]]
    assert late and late[0].tx_date < c2.filing.coverage_start   # 기간 밖이지만 유지
    assert any("집계기간" in i["message"] for i in c2.workspace.load_parse_issues())


# --- 계산 섹터용 raw 표시 -------------------------------------------------

def test_buyer_issued_flag(tmp_path):
    me = CL["C002"].biz_no
    rows = _etax_rows("1248100038", me, title="전자세금계산서 목록조회 (매입)")
    rows[1].append("전자세금계산서분류")
    rows[2].append("매입자발행세금계산서")
    rows.append(["2026-07-02", "A-2", "1248100038", "갑", me, "을", 2000, 200, "세금계산서"])
    p = _write(tmp_path / "전자세금계산서_매입.xlsx", rows)
    a, b = parse_file(p, CL["C002"], F2P["C002"]).transactions
    assert a.raw.get("buyer_issued") is True and "매입자발행" in a.memo
    assert "buyer_issued" not in b.raw


def test_tax_invoice_duplicate_flag(tmp_path):
    p = _write(tmp_path / "현금영수증_매출.xlsx", [
        ["현금영수증 매출내역"], ["매출일시", "총금액", "세금계산서발급여부", "비고"],
        ["2026-07-01", 11_000, "Y", ""], ["2026-07-02", 22_000, "N", ""], ["2026-07-03", 33_000, "", "세금계산서 발급분"],
    ])
    a, b, c = parse_file(p, CL["C001"], F2P["C001"]).transactions
    assert a.raw.get("tax_invoice_duplicate") is True
    assert "tax_invoice_duplicate" not in b.raw
    assert c.raw.get("tax_invoice_duplicate") is True
    # 세금계산서 자체에는 표시하지 않음
    me = CL["C002"].biz_no
    p2 = _write(tmp_path / "전자세금계산서_매출.xlsx", _etax_rows(me, "1248100038") )
    t = parse_file(p2, CL["C002"], F2P["C002"]).transactions[0]
    assert "tax_invoice_duplicate" not in t.raw and "buyer_issued" not in t.raw

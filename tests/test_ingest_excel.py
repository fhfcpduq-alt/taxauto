"""excel.py: 파일 읽기·헤더 탐지·날짜/금액 파싱·마스킹."""

from __future__ import annotations

import builtins
from datetime import date, datetime

import openpyxl
import pytest

from taxauto.ingest import excel
from taxauto.models import Severity


# --- 날짜 ---------------------------------------------------------------

@pytest.mark.parametrize("v", [
    "2026-07-03", "20260703", "2026.07.03", "2026/07/03", "2026. 7. 3.", "2026년 07월 03일",
    "2026-07-03 12:34:56", 20260703, 46206, 46206.0, "46206", datetime(2026, 7, 3, 9, 0), date(2026, 7, 3),
])
def test_parse_date_formats(v):
    assert excel.parse_date(v) == date(2026, 7, 3)


def test_parse_date_empty_and_bad():
    assert excel.parse_date(None) is None
    assert excel.parse_date("  ") is None
    assert excel.parse_date("-") is None
    with pytest.raises(ValueError):
        excel.parse_date("어제")
    with pytest.raises(ValueError):
        excel.parse_date(202607)   # 년월 숫자는 날짜 아님
    assert excel.parse_date("07-03", default_year=2026) == date(2026, 7, 3)


def test_parse_month():
    for v in ("2026-07", "2026.07", "202607", 202607, "2026년 7월", datetime(2026, 7, 1)):
        assert excel.parse_month(v) == (2026, 7)
    assert excel.parse_month(7, default_year=2026) == (2026, 7)
    assert excel.month_end(2026, 9) == date(2026, 9, 30)
    assert excel.month_end(2026, 12) == date(2026, 12, 31)


# --- 금액 ---------------------------------------------------------------

@pytest.mark.parametrize("v,exp", [
    ("1,234,000", 1234000), ("-", 0), ("", 0), (None, 0), ("  ", 0), (-1000, -1000), ("-1,000", -1000),
    ("(1,000)", -1000), ("△1,000", -1000), ("1,000-", -1000), ("1,000원", 1000), (1234.0, 1234),
    (1233.9999999, 1234), ("₩ 5,500", 5500), (0, 0),
])
def test_parse_amount(v, exp):
    assert excel.parse_amount(v) == exp
    assert isinstance(excel.parse_amount(v), int)


def test_parse_amount_bad():
    with pytest.raises(ValueError):
        excel.parse_amount("일천원")
    with pytest.raises(ValueError):
        excel.parse_amount(True)


# --- 헤더 탐지 ----------------------------------------------------------

ALIASES = {
    "tx_date": ["작성일자"],
    "supplier_biz_no": ["공급자사업자등록번호", "사업자등록번호@1"],
    "supplier_name": ["공급자상호", "상호@1"],
    "buyer_name": ["공급받는자상호", "상호@2"],
    "supply_amount": ["공급가액"],
    "vat": ["세액"],
}


def test_find_header_skips_title_rows():
    rows = [["전자세금계산서 목록조회"], ["조회기간 : 2026-07-01 ~ 2026-09-30"], [],
            ["작성일자", "공급자사업자등록번호", "상호", "상호", "공급 가액(원)", "세 액"],
            ["2026-07-01", "123-45-67890", "갑", "을", "1,000", "100"]]
    hm = excel.find_header(rows, ALIASES)
    assert hm.row_index == 3
    assert hm.columns == {"tx_date": 0, "supplier_biz_no": 1, "supplier_name": 2, "buyer_name": 3, "supply_amount": 4, "vat": 5}
    assert "목록조회" in hm.title_text


def test_find_header_two_row_group():
    rows = [["제목"],
            ["작성일자", "공급자", None, "공급받는자", None, "공급가액", "세액"],
            [None, "사업자등록번호", "상호", "사업자등록번호", "상호", None, None],
            ["2026-07-01", "1234567890", "갑", "2208123456", "을", 1000, 100]]
    hm = excel.find_header(rows, ALIASES)
    assert hm.row_index == 2 and hm.data_start == 3
    assert hm.columns["supplier_name"] == 2 and hm.columns["buyer_name"] == 4
    assert hm.columns["tx_date"] == 0 and hm.columns["supply_amount"] == 5


def test_find_header_beyond_scan_limit_is_none():
    rows = [["x"]] * 20 + [["작성일자", "공급가액", "세액"]]
    assert excel.find_header(rows, ALIASES) is None


def test_norm_header():
    assert excel.norm_header(" 공급 가액\n(원) ") == "공급가액"
    assert excel.norm_header("*승인번호") == "승인번호"


# --- 파일 읽기 ----------------------------------------------------------

def test_read_xlsx_and_csv_encodings(tmp_path):
    p = tmp_path / "a.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["작성일자", "공급가액"])
    wb.active.append([datetime(2026, 7, 1), 1000])
    wb.save(p)
    sheets, issues = excel.read_workbook(p)
    assert not issues and sheets[0].rows[1] == [datetime(2026, 7, 1), 1000]

    text = "제목,,\n작성일자,공급가액,세액\n2026-07-01,\"1,000\",100\n"
    for enc in ("utf-8-sig", "cp949"):
        c = tmp_path / f"b_{enc}.csv"
        c.write_bytes(text.encode(enc))
        sheets, issues = excel.read_workbook(c)
        assert not issues
        assert sheets[0].rows[1] == ["작성일자", "공급가액", "세액"]
        assert sheets[0].rows[2][1] == "1,000"


def test_xls_without_xlrd_gives_issue(tmp_path, monkeypatch):
    p = tmp_path / "old.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "xlrd":
            raise ImportError("no xlrd")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    sheets, issues = excel.read_workbook(p)
    assert sheets == []
    assert issues and "xls 변환 필요" in issues[0].message and issues[0].severity == Severity.WARN


def test_html_disguised_xls(tmp_path):
    p = tmp_path / "hometax.xls"
    p.write_text("<html><body><table><tr><th>작성일자</th><th colspan=2>공급가액</th></tr>"
                 "<tr><td>2026-07-01</td><td>1,000</td><td>x</td></tr></table></body></html>", encoding="cp949")
    sheets, issues = excel.read_workbook(p)
    assert not issues
    assert sheets[0].rows[0] == ["작성일자", "공급가액"]
    assert sheets[0].rows[1] == ["2026-07-01", "1,000", "x"]


def test_unsupported_and_broken(tmp_path):
    p = tmp_path / "scan.pdf"
    p.write_bytes(b"%PDF-1.4")
    _, issues = excel.read_workbook(p)
    assert "지원하지 않는" in issues[0].message
    b = tmp_path / "broken.xlsx"
    b.write_bytes(b"not a zip")
    _, issues = excel.read_workbook(b)
    assert issues and issues[0].severity == Severity.WARN


# --- 마스킹 -------------------------------------------------------------

def test_masking():
    assert excel.mask_card_no("1234-5678-9012-3456") == "****-****-****-3456"
    assert excel.mask_card_no("1234-56**-****-7890") == "****-****-****-7890"
    assert excel.mask_card_no("1234-56**-****-****") == "****"
    assert excel.mask_rrn("주민 900101-1234567 끝") == "주민 900101-1****** 끝"
    assert excel.mask_rrn("20260715-41000012-12345678") == "20260715-41000012-12345678"  # 승인번호는 그대로
    assert excel.is_rrn("900101-1234567") and not excel.is_rrn("123-45-67890")

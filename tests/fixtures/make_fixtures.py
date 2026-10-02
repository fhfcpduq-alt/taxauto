"""합성(가상) 수집파일 생성기 — 실제 홈택스·위하고 양식과 비슷하게 제목행·조회조건행 포함.

저장소에 바이너리 xlsx 를 커밋하지 않고, 테스트 실행 시 tmp_path 에 만든다.

공개 함수(다른 섹터 e2e 용)
  example_clients()                 examples/clients.example.yaml 과 동일한 Client 목록(C001 개인 음식점, C002 법인 제조)
  build_inbox(root, period)         root/{period}/{client_id}/*.xlsx 생성 → {client_id: [경로]}
                                    root 는 inbox 루트(= policy run.inbox). period: "2026-2P" | "2026-2F"
  build_bulk(root, period)          root/{period}/_bulk/ 에 수임처 일괄(여러 거래처 혼합) 파일 생성
  FACTS                             의도적으로 넣은 특이사항(승인번호·금액) — 검증 섹터 기대값
  make_ctx(base, client_id, period) 테스트용 RunContext (base/inbox, base/data, base/clients)

명령행: python tests/fixtures/make_fixtures.py OUT_DIR [period]
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import openpyxl
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from taxauto.models import Client, TaxpayerType, Vehicle  # noqa: E402


# ---------------------------------------------------------------------------
# 거래처
# ---------------------------------------------------------------------------


def example_clients() -> list[Client]:
    """examples/clients.example.yaml 그대로(파일이 없으면 같은 내용의 내장값)."""
    p = ROOT / "examples" / "clients.example.yaml"
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return [Client.from_dict(c) for c in data.get("clients") or []]
    return [
        Client(id="C001", name="OO식당", biz_no="1234567890", taxpayer_type=TaxpayerType.INDIVIDUAL,
               industry="음식점업/한식", industry_code="552101", prior_year_supply=300_000_000,
               deemed_input_type="restaurant", vehicles=[Vehicle("12가3456", "승용 2000cc", False)],
               notes="배달앱 매출(배민·쿠팡이츠) 있음"),
        Client(id="C002", name="(주)민테크", biz_no="2208123456", taxpayer_type=TaxpayerType.CORPORATION,
               industry="제조업/전자부품", prior_year_supply=2_500_000_000, contact_name="김경리"),
    ]


def _client(cid: str) -> Client:
    return next(c for c in example_clients() if c.id == cid)


def _fmt_biz(b: str) -> str:
    return f"{b[:3]}-{b[3:5]}-{b[5:]}" if len(b) == 10 else b


def valid_biz_no(prefix9: str) -> str:
    """검증번호가 맞는 가상 사업자번호(앞 9자리 → 10자리)."""
    w = [1, 3, 7, 1, 3, 7, 1, 3, 5]
    d = [int(c) for c in prefix9]
    s = sum(a * b for a, b in zip(d, w)) + (d[8] * 5) // 10
    return prefix9 + str((10 - s % 10) % 10)


# 가상 거래상대방
CP = {
    "대한식자재": (valid_biz_no("214860001"), "(주)대한식자재"),
    "푸른농산": (valid_biz_no("305120002"), "푸른농산"),
    "가나전자": (valid_biz_no("124810003"), "(주)가나전자"),
    "글로벌무역": (valid_biz_no("124810004"), "(주)글로벌무역"),
    "다라상사": (valid_biz_no("135810005"), "(주)다라상사"),
    "부품상사": (valid_biz_no("140810006"), "(주)부품상사"),
    "기계산업": (valid_biz_no("410810007"), "(주)기계산업"),
    "한빛렌터카": (valid_biz_no("220810008"), "한빛렌터카(주)"),
    "동네분식": (valid_biz_no("101250009"), "동네분식"),          # 간이과세자
    "하나로마트": (valid_biz_no("107820010"), "OO농협하나로마트"),  # 면세 농산물
    "오피스몰": (valid_biz_no("211860011"), "(주)오피스몰"),
    "주방설비": (valid_biz_no("312250012"), "한빛주방설비"),
    "레이크CC": (valid_biz_no("128810013"), "레이크컨트리클럽(주)"),  # 골프장(접대비성)
    "OO주유소": (valid_biz_no("214810014"), "SK OO주유소"),
    "철물점": (valid_biz_no("305250015"), "대성철물"),
    "설비수리": (valid_biz_no("312250016"), "한빛설비"),
    "배달앱A": (valid_biz_no("120870017"), "배달앱A(주)"),
    "배달앱B": (valid_biz_no("120880018"), "배달앱B(유)"),
    "타거래처": (valid_biz_no("999810019"), "(주)무관상사"),
}


def _appr(d: date, seq: int) -> str:
    return f"{d:%Y%m%d}-4100{seq:04d}-{(seq * 7919) % 10**8:08d}"


# 의도적 특이사항 (검증 섹터 기대값)
FACTS: dict[str, Any] = {
    "C002": {
        "zero_rated_approval": _appr(date(2026, 7, 31), 102),     # 영세율(내국신용장) 20,000,000 / 0
        "amended_approval": _appr(date(2026, 8, 20), 104),        # 수정세금계산서 -1,000,000 / -100,000
        "late_issue_approval": _appr(date(2026, 7, 15), 105),     # 작성 07-15, 발급 08-14 (지연발급)
        "late_issue": {"tx_date": "2026-07-15", "issue_date": "2026-08-14"},
        "prelim_omitted_purchase_approval": _appr(date(2026, 9, 28), 299),  # 2F 파일에만 있는 9월 매입(예정신고 누락분)
        "wehago_diff": {"approval": _appr(date(2026, 8, 20), 103), "engine": [5_000_000, 500_000], "wehago": [5_500_000, 550_000]},
        "fixed_asset_approval": _appr(date(2026, 8, 12), 202),    # 기계장치 15,000,000
        "passenger_car_rent_approval": _appr(date(2026, 9, 15), 204),  # 승용차 렌트(불공제 후보)
        "card_golf": {"merchant": CP["레이크CC"][1], "supply": 800_000, "vat": 80_000},
        "card_fuel": {"merchant": CP["OO주유소"][1], "supply": 90_000, "vat": 9_000},
    },
    "C001": {
        "card_simple_taxpayer": {"merchant": CP["동네분식"][1], "total": 11_000},
        "card_tax_free": {"merchant": CP["하나로마트"][1], "total": 55_000},
        "card_sales_monthly_totals": {7: 33_000_000, 8: 35_200_000, 9: 30_800_000, 10: 31_900_000, 11: 29_700_000, 12: 38_500_000},
        "cash_receipt_cancel": {"tx_date": "2026-08-17", "total": -22_000},
        "paper_invoice": {"merchant": CP["설비수리"][1], "supply": 300_000, "vat": 30_000},
    },
}


# ---------------------------------------------------------------------------
# 엑셀 쓰기
# ---------------------------------------------------------------------------


def _write(path: Path, title_rows: list[list[Any]], header_rows: list[list[Any]], rows: list[list[Any]], sheet: str = "Sheet1") -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for r in title_rows + header_rows + rows:
        ws.append(r)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _period_text(months: list[int], year: int = 2026) -> str:
    s = date(year, months[0], 1)
    e_m = months[-1]
    e = date(year + e_m // 12, e_m % 12 + 1, 1) if e_m < 12 else date(year + 1, 1, 1)
    from datetime import timedelta

    return f"조회기간 : {s.isoformat()} ~ {(e - timedelta(days=1)).isoformat()}"


ETAX_FLAT_HEADER = ["작성일자", "승인번호", "발급일자", "전송일자",
                    "공급자사업자등록번호", "종사업장번호", "상호", "대표자명",
                    "공급받는자사업자등록번호", "종사업장번호", "상호", "대표자명",
                    "합계금액", "공급가액", "세액", "전자세금계산서분류", "전자세금계산서종류", "발급유형", "비고", "품목명"]


def _etax_row(r: dict, flat: bool = True, with_vat: bool = True) -> list[Any]:
    sup_b, sup_n = r["sup"]
    buy_b, buy_n = r["buy"]
    tx = r["tx"]
    iss = r.get("issue", tx)
    trn = r.get("transmit", iss)
    vat = r["vat"] if with_vat else 0
    row = [tx.isoformat(), r["appr"], iss.isoformat(), trn.isoformat(),
           _fmt_biz(sup_b), "", sup_n, "대표", _fmt_biz(buy_b), "", buy_n, "대표",
           r["supply"] + vat, r["supply"]]
    if with_vat:
        row.append(vat)
    row += [r.get("cls", "세금계산서" if with_vat else "계산서"), r.get("type", "일반"), r.get("issue_type", "정발행"), r.get("memo", ""), r.get("item", "")]
    return row


def write_etax(path: Path, label: str, rows: list[dict], months: list[int], two_row_header: bool = False,
               einv: bool = False) -> Path:
    doc = "전자계산서" if einv else "전자세금계산서"
    title = [[f"{doc} 목록조회 ({label})"], [_period_text(months)], []]
    if not two_row_header:
        hdr = list(ETAX_FLAT_HEADER)
        if einv:
            hdr.remove("세액")
            hdr = [h.replace("전자세금계산서", "전자계산서") for h in hdr]
        data = [_etax_row(r, with_vat=not einv) for r in rows]
        return _write(path, title, [hdr], data)
    # 2단 헤더(병합된 공급자/공급받는자 그룹)
    top = ["작성일자", "승인번호", "발급일자", "전송일자", "공급자", None, None, "공급받는자", None, None,
           "합계금액", "공급가액", "세액", f"{doc}분류", f"{doc}종류", "품목명"]
    sub = [None, None, None, None, "사업자등록번호", "상호", "대표자명", "사업자등록번호", "상호", "대표자명",
           None, None, None, None, None, None]
    data = []
    for r in rows:
        full = _etax_row(r)
        # flat → 2단 열 순서로 재배치(종사업장번호 없음)
        data.append([full[0], full[1], full[2], full[3], full[4], full[6], full[7], full[8], full[10], full[11],
                     full[12], full[13], full[14], full[15], full[16], full[19]])
    return _write(path, title, [top, sub], data)


def _months_of(period: str, client: Client) -> list[int]:
    if period.endswith("2P"):
        return [7, 8, 9]
    if period.endswith("2F"):
        return [7, 8, 9, 10, 11, 12] if not client.is_corporation else [10, 11, 12]
    raise ValueError(f"픽스처 미지원 회차: {period} (2026-2P / 2026-2F)")


def _me(m: int) -> date:
    from datetime import timedelta

    return (date(2026 + m // 12, m % 12 + 1, 1) if m < 12 else date(2027, 1, 1)) - timedelta(days=1)


# ---------------------------------------------------------------------------
# C001 개인 음식점
# ---------------------------------------------------------------------------


def _c001_files(d: Path, months: list[int]) -> list[Path]:
    c = _client("C001")
    me = (c.biz_no, c.name)
    out: list[Path] = []
    purch = {7: 1_500_000, 8: 1_700_000, 9: 1_600_000, 10: 1_550_000, 11: 1_650_000, 12: 1_800_000}
    rows = [dict(tx=_me(m), appr=_appr(_me(m), 10 + m), sup=CP["대한식자재"], buy=me, supply=purch[m], vat=purch[m] // 10,
                 item="식자재") for m in months]
    out.append(write_etax(d / "전자세금계산서_매입.xlsx", "매입", rows, months, two_row_header=True))

    veg = {7: 800_000, 8: 850_000, 9: 900_000, 10: 780_000, 11: 820_000, 12: 950_000}
    rows = [dict(tx=date(2026, m, 15), appr=_appr(date(2026, m, 15), 30 + m), sup=CP["푸른농산"], buy=me,
                 supply=veg[m], vat=0, item="채소류") for m in months]
    out.append(write_etax(d / "전자계산서_매입.xlsx", "매입", rows, months, einv=True))

    # 신용카드 매출(홈택스 월별 합계)
    tot = FACTS["C001"]["card_sales_monthly_totals"]
    out.append(_write(d / "신용카드매출자료.xlsx",
                      [["신용카드 매출자료 조회"], [_period_text(months)], ["(단위:원)"]],
                      [["승인년월", "건수", "매출액계", "신용카드결제", "구매전용카드결제", "봉사료"]],
                      [[f"2026-{m:02d}", 1000 + m, f"{tot[m]:,}", f"{tot[m]:,}", "0", "0"] for m in months]
                      + [["합계", "", f"{sum(tot[m] for m in months):,}", "", "", ""]]))

    # 현금영수증 매출(건별, 공급가액·부가세 분리) + 취소 1건
    cr = []
    for m in months:
        cr.append([f"2026-{m:02d}-05 12:30:00", "500,000", "50,000", "0", "550,000", f"T{m:02d}0501", "카드", "승인", "소득공제"])
        cr.append([f"2026-{m:02d}-20 19:10:00", "200,000", "20,000", "0", "220,000", f"T{m:02d}2001", "휴대전화", "승인", "지출증빙"])
    if 8 in months:
        cr.append(["2026-08-17 13:00:00", "20,000", "2,000", "0", "22,000", "T081701", "휴대전화", "취소", "소득공제"])
    out.append(_write(d / "현금영수증_매출내역.xlsx",
                      [["현금영수증 매출내역 조회"], [_period_text(months)]],
                      [["매출일시", "공급가액", "부가세", "봉사료", "총금액", "승인번호", "발급수단", "거래구분", "용도구분"]], cr))

    # 판매대행(배달앱) 매출 - 월별, 공급대가
    pg = []
    for m in months:
        pg.append([f"{2026}{m:02d}", _fmt_biz(CP["배달앱A"][0]), CP["배달앱A"][1], 300 + m, 5_500_000 + m * 11_000])
        pg.append([f"{2026}{m:02d}", _fmt_biz(CP["배달앱B"][0]), CP["배달앱B"][1], 100 + m, 2_200_000])
    out.append(_write(d / "판매대행_매출자료.xlsx",
                      [["판매(결제)대행 매출자료 조회"], [_period_text(months)]],
                      [["매출년월", "판매대행사 사업자등록번호", "판매대행사명", "건수", "판매(결제)금액"]], pg))

    # 사업용카드 매입: 간이과세자, 면세, 일반, 고정자산 후보
    cards = []
    if 7 in months:
        cards.append([datetime(2026, 7, 3), "1234-56**-****-1111", "30070301", _fmt_biz(CP["동네분식"][0]), CP["동네분식"][1],
                      11_000, 0, 0, 11_000, "간이과세자", "분식", "불공제"])
        cards.append([datetime(2026, 7, 8), "1234-56**-****-1111", "30070801", _fmt_biz(CP["하나로마트"][0]), CP["하나로마트"][1],
                      55_000, 0, 0, 55_000, "일반과세자", "농축수산물", "불공제"])
    if 8 in months:
        cards.append([datetime(2026, 8, 2), "1234-56**-****-1111", "30080201", _fmt_biz(CP["오피스몰"][0]), CP["오피스몰"][1],
                      100_000, 10_000, 0, 110_000, "일반과세자", "문구용품", "공제"])
    if 9 in months:
        cards.append([datetime(2026, 9, 10), "1234-56**-****-1111", "30091001", _fmt_biz(CP["주방설비"][0]), CP["주방설비"][1],
                      2_000_000, 200_000, 0, 2_200_000, "일반과세자", "주방기기", "공제"])
    for m in (x for x in months if x >= 10):
        cards.append([datetime(2026, m, 6), "1234-56**-****-1111", f"300{m}0601", _fmt_biz(CP["오피스몰"][0]), CP["오피스몰"][1],
                      50_000, 5_000, 0, 55_000, "일반과세자", "문구용품", "공제"])
    out.append(_write(d / "사업용신용카드_매입내역.xlsx",
                      [["사업용 신용카드 사용내역 (매입세액 공제 확인/변경)"], [_period_text(months)]],
                      [["승인일자", "카드번호", "승인번호", "가맹점사업자번호", "가맹점명", "공급가액", "부가세", "봉사료", "합계",
                        "가맹점유형", "업종", "공제여부결정"]], cards))

    if 7 in months:
        out.append(_write(d / "현금영수증_매입내역.xlsx",
                          [["현금영수증 매입내역(지출증빙) 조회"], [_period_text(months)]],
                          [["매입일시", "가맹점사업자번호", "가맹점명", "공급가액", "부가세", "봉사료", "총금액", "승인번호", "거래구분"]],
                          [["2026-07-22 10:00:00", _fmt_biz(CP["철물점"][0]), CP["철물점"][1], 30_000, 3_000, 0, 33_000, "R072201", "승인"]]))

    if 8 in months:
        sys.path.insert(0, str(ROOT / "examples"))
        from make_paper_invoice_template import build_template

        p = FACTS["C001"]["paper_invoice"]
        out.append(build_template(d / "종이세금계산서_입력.xlsx", rows=[
            ["매입", "세금계산서", date(2026, 8, 25), _fmt_biz(CP["설비수리"][0]), p["merchant"], "배관수리",
             p["supply"], p["vat"], p["supply"] + p["vat"], "N", "N", ""],
        ]))
    return out


# ---------------------------------------------------------------------------
# C002 법인 제조/도소매
# ---------------------------------------------------------------------------


def _c002_sales_rows(months: list[int]) -> list[dict]:
    c = _client("C002")
    me = (c.biz_no, c.name)
    rows: list[dict] = []
    if 7 in months:
        rows += [
            dict(tx=date(2026, 7, 10), appr=_appr(date(2026, 7, 10), 101), sup=me, buy=CP["가나전자"], supply=10_000_000, vat=1_000_000, item="PCB 모듈"),
            dict(tx=date(2026, 7, 31), appr=FACTS["C002"]["zero_rated_approval"], sup=me, buy=CP["글로벌무역"], supply=20_000_000, vat=0,
                 type="영세율", item="센서(내국신용장)"),
            dict(tx=date(2026, 7, 15), appr=FACTS["C002"]["late_issue_approval"], issue=date(2026, 8, 14), transmit=date(2026, 8, 15),
                 sup=me, buy=CP["다라상사"], supply=3_000_000, vat=300_000, item="케이블"),
        ]
    if 8 in months:
        rows += [
            dict(tx=date(2026, 8, 20), appr=FACTS["C002"]["wehago_diff"]["approval"], sup=me, buy=CP["가나전자"], supply=5_000_000, vat=500_000, item="PCB 모듈"),
            dict(tx=date(2026, 8, 20), appr=FACTS["C002"]["amended_approval"], issue=date(2026, 9, 5), sup=me, buy=CP["가나전자"],
                 supply=-1_000_000, vat=-100_000, cls="수정세금계산서", memo="환입", item="PCB 모듈(반품)"),
        ]
    if 9 in months:
        rows.append(dict(tx=date(2026, 9, 25), appr=_appr(date(2026, 9, 25), 106), sup=me, buy=CP["다라상사"], supply=8_000_000, vat=800_000, item="케이블"))
    for m in (x for x in months if x >= 10):
        rows.append(dict(tx=date(2026, m, 12), appr=_appr(date(2026, m, 12), 110 + m), sup=me, buy=CP["가나전자"], supply=9_000_000, vat=900_000, item="PCB 모듈"))
    return rows


def _c002_purchase_rows(months: list[int], late_sep: bool = False) -> list[dict]:
    c = _client("C002")
    me = (c.biz_no, c.name)
    rows: list[dict] = []
    if 7 in months:
        rows.append(dict(tx=date(2026, 7, 5), appr=_appr(date(2026, 7, 5), 201), sup=CP["부품상사"], buy=me, supply=6_000_000, vat=600_000, item="원재료"))
    if 8 in months:
        rows.append(dict(tx=date(2026, 8, 12), appr=FACTS["C002"]["fixed_asset_approval"], sup=CP["기계산업"], buy=me, supply=15_000_000, vat=1_500_000, item="CNC 가공기"))
    if 9 in months:
        rows.append(dict(tx=date(2026, 9, 3), appr=_appr(date(2026, 9, 3), 203), sup=CP["부품상사"], buy=me, supply=4_000_000, vat=400_000, item="원재료"))
        rows.append(dict(tx=date(2026, 9, 15), appr=FACTS["C002"]["passenger_car_rent_approval"], sup=CP["한빛렌터카"], buy=me,
                         supply=700_000, vat=70_000, item="승용차 렌트(쏘나타)"))
    if late_sep:
        rows.append(dict(tx=date(2026, 9, 28), appr=FACTS["C002"]["prelim_omitted_purchase_approval"], issue=date(2026, 10, 6),
                         sup=CP["부품상사"], buy=me, supply=1_000_000, vat=100_000, item="원재료"))
    for m in (x for x in months if x >= 10):
        rows.append(dict(tx=date(2026, m, 8), appr=_appr(date(2026, m, 8), 210 + m), sup=CP["부품상사"], buy=me, supply=5_000_000, vat=500_000, item="원재료"))
    return rows


def _c002_files(d: Path, months: list[int], period: str) -> list[Path]:
    out: list[Path] = []
    out.append(write_etax(d / "전자세금계산서_매출.xlsx", "매출", _c002_sales_rows(months), months))
    out.append(write_etax(d / "전자세금계산서_매입.xlsx", "매입", _c002_purchase_rows(months, late_sep=period.endswith("2F")), months))

    cards = []
    if 7 in months:
        g = FACTS["C002"]["card_golf"]
        cards.append(["2026-07-20", "9876-54**-****-2222", "50072001", _fmt_biz(CP["레이크CC"][0]), g["merchant"],
                      g["supply"], g["vat"], 0, g["supply"] + g["vat"], "일반과세자", "골프장 운영업", "공제"])
    if 8 in months:
        f = FACTS["C002"]["card_fuel"]
        cards.append(["2026-08-05", "9876-54**-****-2222", "50080501", _fmt_biz(CP["OO주유소"][0]), f["merchant"],
                      f["supply"], f["vat"], 0, f["supply"] + f["vat"], "일반과세자", "주유소", "공제"])
        cards.append(["2026-08-18", "9876-54**-****-2222", "50081801", _fmt_biz(CP["동네분식"][0]), CP["동네분식"][1],
                      33_000, 0, 0, 33_000, "간이과세자", "분식", "불공제"])
    if 9 in months:
        cards.append(["2026-09-09", "9876-54**-****-2222", "50090901", _fmt_biz(CP["하나로마트"][0]), CP["하나로마트"][1],
                      66_000, 0, 0, 66_000, "일반과세자", "농축수산물", "불공제"])
    for m in (x for x in months if x >= 10):
        cards.append([f"2026-{m:02d}-11", "9876-54**-****-2222", f"500{m}1101", _fmt_biz(CP["오피스몰"][0]), CP["오피스몰"][1],
                      200_000, 20_000, 0, 220_000, "일반과세자", "사무용품", "공제"])
    out.append(_write(d / "사업용신용카드_매입내역.xlsx",
                      [["사업용 신용카드 사용내역"], [_period_text(months)]],
                      [["승인일자", "카드번호", "승인번호", "가맹점사업자번호", "가맹점명", "공급가액", "부가세", "봉사료", "합계",
                        "가맹점유형", "업종", "공제여부결정"]], cards))

    if period.endswith("2P"):
        # 위하고 매입매출전표 내보내기. 홈택스 자료를 모두 입력했으나 8/20 가나전자 건만 5,500,000 으로 잘못 입력(대사 차이 1건)
        # 카드는 공제분(주유)만 입력, 골프장(접대비)·간이·면세 카드는 입력 안 함(실무 관행)
        diff = FACTS["C002"]["wehago_diff"]
        gn, dr, gl, bp, gm, rc = (CP[k] for k in ("가나전자", "다라상사", "글로벌무역", "부품상사", "기계산업", "한빛렌터카"))
        led = [
            [7, 10, "11.과세", "PCB 모듈", 10_000_000, 1_000_000, 11_000_000, gn[1], _fmt_biz(gn[0]), "여"],
            [7, 15, "11.과세", "케이블", 3_000_000, 300_000, 3_300_000, dr[1], _fmt_biz(dr[0]), "여"],
            [7, 31, "12.영세", "센서", 20_000_000, 0, 20_000_000, gl[1], _fmt_biz(gl[0]), "여"],
            [8, 20, "11.과세", "PCB 모듈", diff["wehago"][0], diff["wehago"][1], sum(diff["wehago"]), gn[1], _fmt_biz(gn[0]), "여"],
            [8, 20, "11.과세", "PCB 모듈(반품)", -1_000_000, -100_000, -1_100_000, gn[1], _fmt_biz(gn[0]), "여"],
            [9, 25, "11.과세", "케이블", 8_000_000, 800_000, 8_800_000, dr[1], _fmt_biz(dr[0]), "여"],
            [7, 5, "51.과세", "원재료", 6_000_000, 600_000, 6_600_000, bp[1], _fmt_biz(bp[0]), "여"],
            [8, 12, "51.과세", "CNC 가공기", 15_000_000, 1_500_000, 16_500_000, gm[1], _fmt_biz(gm[0]), "여"],
            [9, 3, "51.과세", "원재료", 4_000_000, 400_000, 4_400_000, bp[1], _fmt_biz(bp[0]), "여"],
            [9, 15, "54.불공", "승용차 렌트", 700_000, 70_000, 770_000, rc[1], _fmt_biz(rc[0]), "여"],
            [8, 5, "57.카과", "주유", 90_000, 9_000, 99_000, CP["OO주유소"][1], _fmt_biz(CP["OO주유소"][0]), ""],
        ]
        out.append(_write(d / "위하고_매입매출전표.xlsx",
                          [["매입매출전표 목록"], ["회사: (주)민테크   기간: 2026-07-01 ~ 2026-09-30"]],
                          [["월", "일", "유형", "품목", "공급가액", "부가세", "합계", "거래처", "사업자번호", "전자"]], led))
    return out


# ---------------------------------------------------------------------------
# 공개 함수
# ---------------------------------------------------------------------------


def build_inbox(root: Path, period: str = "2026-2P", clients: list[str] | None = None) -> dict[str, list[Path]]:
    """inbox 루트 아래 {period}/{client_id}/ 에 합성 파일 생성."""
    root = Path(root)
    out: dict[str, list[Path]] = {}
    for c in example_clients():
        if clients and c.id not in clients:
            continue
        d = root / period / c.id
        months = _months_of(period, c)
        if c.id == "C001":
            out[c.id] = _c001_files(d, months)
        elif c.id == "C002":
            out[c.id] = _c002_files(d, months, period)
    return out


def build_bulk(root: Path, period: str = "2026-2P") -> Path:
    """수임처 일괄 다운로드(전자세금계산서 매입, 여러 거래처 혼합) → {period}/_bulk/."""
    c1, c2 = _client("C001"), _client("C002")
    m1, m2 = _months_of(period, c1), _months_of(period, c2)
    purch = {7: 1_500_000, 8: 1_700_000, 9: 1_600_000, 10: 1_550_000, 11: 1_650_000, 12: 1_800_000}
    rows = [dict(tx=_me(m), appr=_appr(_me(m), 10 + m), sup=CP["대한식자재"], buy=(c1.biz_no, c1.name), supply=purch[m],
                 vat=purch[m] // 10, item="식자재") for m in m1]
    rows += _c002_purchase_rows(m2)
    other = dict(tx=date(2026, m2[0], 9), appr=_appr(date(2026, m2[0], 9), 901), sup=CP["부품상사"], buy=CP["타거래처"],
                 supply=777_000, vat=77_700, item="무관")
    rows.append(other)
    hdr = ["수임처사업자등록번호", "수임처명"] + ETAX_FLAT_HEADER
    own = {c1.biz_no: c1.name, c2.biz_no: c2.name, CP["타거래처"][0]: CP["타거래처"][1]}
    data = []
    for r in rows:
        b = r["buy"][0]
        data.append([_fmt_biz(b), own.get(b, "")] + _etax_row(r))
    return _write(Path(root) / period / "_bulk" / "수임처_전자세금계산서_매입.xlsx",
                  [["세무대리인 수임처 전자세금계산서 목록 (매입)"], [_period_text(m1)]], [hdr], data)


def make_ctx(base: Path, client_id: str, period: str = "2026-2P", policy: dict | None = None,
             today: date = date(2026, 10, 2), dry_run: bool = False):
    """테스트용 RunContext: base/inbox, base/data, base/clients 구조. (inbox 파일은 build_inbox 로 따로 생성)"""
    import logging

    from taxauto.context import RunContext
    from taxauto.law import CONFIG_DIR, Law, load_policy
    from taxauto.models import TaxPeriod
    from taxauto.period import build_filing, load_holidays
    from taxauto.workspace import Workspace

    base = Path(base)
    client = _client(client_id)
    law = Law.load()
    filing = build_filing(client, TaxPeriod.parse(period), law, load_holidays())
    return RunContext(
        client=client,
        filing=filing,
        workspace=Workspace(base / "data" / period / client_id),
        law=law,
        policy=policy if policy is not None else load_policy(),
        inbox_dir=base / "inbox" / period / client_id,
        config_dir=CONFIG_DIR,
        clients_dir=base / "clients",
        today=today,
        log=logging.getLogger("taxauto.test"),
        dry_run=dry_run,
    )


def example_clients_yaml() -> str:
    """example_clients() 를 clients.yaml 형식 문자열로."""
    return yaml.safe_dump({"clients": [c.to_dict() for c in example_clients()]}, allow_unicode=True, sort_keys=False)


if __name__ == "__main__":
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("inbox")
    for per in sys.argv[2:] or ["2026-2P", "2026-2F"]:
        for cid, files in build_inbox(out_dir, per).items():
            print(per, cid, len(files), "files")

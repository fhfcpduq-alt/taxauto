"""리포트 공용 포맷·라벨·스타일."""

from __future__ import annotations

import html
from datetime import date

from ..models import Line

# 신고서 칸 라벨 (서식 개정 시 대조)
LINE_LABELS: dict[str, str] = {
    "S_TAX_INVOICE": "과세 세금계산서 발급분",
    "S_BUYER_ISSUED": "과세 매입자발행 세금계산서",
    "S_CARD_CASH": "과세 신용카드·현금영수증 발행분",
    "S_OTHER": "과세 기타(정규영수증 외)",
    "S_ZR_TAX_INVOICE": "영세율 세금계산서 발급분",
    "S_ZR_OTHER": "영세율 기타",
    "S_PRELIM_OMITTED": "예정신고 누락분(매출)",
    "S_BAD_DEBT": "대손세액 가감",
    "S_TOTAL": "매출세액 합계 ㉮",
    "P_TI_GENERAL": "세금계산서 수취 일반매입",
    "P_TI_EXPORT_DEFER": "수출기업 수입분 납부유예",
    "P_TI_FIXED": "세금계산서 수취 고정자산",
    "P_PRELIM_OMITTED": "예정신고 누락분(매입)",
    "P_BUYER_ISSUED": "매입자발행 세금계산서",
    "P_OTHER_DEDUCTIBLE": "그 밖의 공제매입세액",
    "P_TOTAL": "매입세액 합계",
    "P_NON_DEDUCTIBLE": "공제받지 못할 매입세액",
    "P_NET": "차감계 ㉯",
    "PAYABLE": "납부(환급)세액 ㉮-㉯",
    "C_OTHER": "그 밖의 경감·공제세액",
    "C_CARD_ISSUE": "신용카드매출전표등 발행공제",
    "C_TOTAL": "경감·공제 합계",
    "C_SMALL_BIZ": "소규모 개인사업자 감면",
    "PRELIM_UNREFUNDED": "예정신고 미환급세액",
    "PRELIM_NOTICE": "예정고지세액",
    "PROXY_TRANSFEREE": "사업양수자 대리납부",
    "PROXY_BUYER": "매입자 납부특례",
    "PROXY_CARD": "신용카드업자 대리납부",
    "PENALTY": "가산세액계",
    "FINAL": "차가감 납부할(환급받을) 세액",
}

# 값이 0이어도 항상 보여줄 칸
ALWAYS_LINES = {"S_TOTAL", "P_NET", "PAYABLE", "FINAL"}
TOTAL_LINES = {"S_TOTAL", "P_TOTAL", "P_NET", "PAYABLE", "C_TOTAL", "FINAL"}


def line_no(name: str) -> str:
    try:
        return Line[name].value
    except KeyError:
        return ""


def won(n: int | None, sign: bool = False) -> str:
    if n is None:
        return "-"
    n = int(n)
    if sign and n > 0:
        return f"+{n:,}원"
    return f"{n:,}원"


def num(n: int | None) -> str:
    return "-" if n is None else f"{int(n):,}"


WEEKDAYS = "월화수목금토일"


def kdate(d: date | str | None, with_year: bool = False) -> str:
    if not d:
        return "-"
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    s = f"{d.year}년 {d.month}월 {d.day}일" if with_year else f"{d.month}월 {d.day}일"
    return f"{s}({WEEKDAYS[d.weekday()]})"


def dday(n: int | None) -> str:
    if n is None:
        return "-"
    if n == 0:
        return "D-day"
    return f"D-{n}" if n > 0 else f"D+{-n}"


def esc(s: object) -> str:
    return html.escape("" if s is None else str(s))


def tax_verdict(final_tax: int | None) -> tuple[str, int | None]:
    """(라벨, 절대값)"""
    if final_tax is None:
        return "세액 미산출", None
    if final_tax > 0:
        return "납부할 세액", final_tax
    if final_tax < 0:
        return "환급받을 세액", -final_tax
    return "납부할 세액", 0


# 인쇄 가능한 공용 스타일 (외부 CDN 없음)
BASE_CSS = """
:root{--ink:#1b1f24;--muted:#5b6470;--line:#d9dde3;--soft:#f4f6f8;--accent:#1f4e79;
--red:#b42318;--red-bg:#fdecea;--amber:#9a6700;--amber-bg:#fff6dc;--green:#1a7f37;--green-bg:#e8f5ec;--blue-bg:#eef3f9}
*{box-sizing:border-box}
html,body{margin:0;background:#fff;color:var(--ink)}
body{font-family:Pretendard,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",system-ui,sans-serif;
font-size:13px;line-height:1.5;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.page{max-width:900px;margin:0 auto;padding:24px 20px 40px}
h1{font-size:20px;margin:0}
h2{font-size:13px;letter-spacing:.02em;color:var(--accent);margin:22px 0 8px;padding-bottom:4px;border-bottom:2px solid var(--accent)}
.muted{color:var(--muted)}
.mono{font-family:ui-monospace,Consolas,"D2Coding",monospace;font-size:11px}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table{width:100%;border-collapse:collapse}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--soft);font-weight:600;text-align:left;font-size:12px;color:var(--muted)}
tr.total td{font-weight:700;background:var(--blue-bg)}
.chip{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap}
.chip.red{background:var(--red-bg);color:var(--red)}
.chip.amber{background:var(--amber-bg);color:var(--amber)}
.chip.green{background:var(--green-bg);color:var(--green)}
.chip.gray{background:var(--soft);color:var(--muted)}
@media (max-width:640px){.page{padding:16px}}
@page{size:A4;margin:12mm}
@media print{.page{max-width:none;padding:0}.noprint{display:none}h2{break-after:avoid}tr{break-inside:avoid}}
"""

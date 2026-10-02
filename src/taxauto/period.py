"""신고회차·집계기간·신고기한 계산."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import yaml

from .law import CONFIG_DIR, Law
from .models import Client, Filing, TaxPeriod


def load_holidays(config_dir: Path | None = None) -> set[date]:
    p = (config_dir or CONFIG_DIR) / "holidays.yaml"
    if not p.exists():
        return set()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: set[date] = set()
    for _year, items in (data.get("holidays") or {}).items():
        for it in items or []:
            out.add(date.fromisoformat(str(it["date"] if isinstance(it, dict) else it)))
    return out


def next_business_day(d: date, holidays: set[date]) -> date:
    """국세기본법 제5조: 기한이 토·일·공휴일(대체공휴일 포함)이면 그 다음 날."""
    while d.weekday() >= 5 or d in holidays:
        d += timedelta(days=1)
    return d


def months(period: TaxPeriod) -> tuple[int, int]:
    """회차가 '본래' 담당하는 3개월 (시작월, 끝월)."""
    base = 1 if period.half == 1 else 7
    return (base, base + 2) if period.kind == "P" else (base + 3, base + 5)


def coverage(period: TaxPeriod, filed_preliminary: bool) -> tuple[date, date]:
    """집계 대상 기간.

    예정(P): 3개월.
    확정(F): 예정신고를 한 사업자(법인 대부분)는 3개월,
             예정고지를 받은(=예정신고 안 한) 사업자는 6개월 전체.
    """
    s_m, e_m = months(period)
    if period.kind == "F" and not filed_preliminary:
        s_m -= 3
    start = date(period.year, s_m, 1)
    end = (date(period.year + (e_m // 12), (e_m % 12) + 1, 1) - timedelta(days=1))
    return start, end


def due_date(period: TaxPeriod, law: Law, holidays: set[date]) -> date:
    key = f"deadline.{period.half}{period.kind}"
    # 기한 파라미터 자체의 적용 시점은 과세기간 종료일 기준
    _, e_m = months(period)
    mmdd = law.get(key, on=date(period.year, e_m, 1))
    mm, dd = (int(x) for x in str(mmdd).split("-"))
    year = period.year + 1 if (period.half == 2 and period.kind == "F") else period.year
    return next_business_day(date(year, mm, dd), holidays)


def default_filed_preliminary(client: Client) -> bool:
    """기본 가정: 법인은 예정신고, 개인은 예정고지.

    예외(설정에서 거래처·회차별로 덮어쓴다):
      - 직전 과세기간 공급가액 1.5억 미만 소규모 법인 → 예정고지(예정신고 안 함)
      - 개인이라도 조기환급·사업부진 등으로 예정신고를 선택한 경우 → 예정신고
    """
    return client.is_corporation


def build_filing(
    client: Client,
    period: TaxPeriod,
    law: Law,
    holidays: set[date],
    filed_preliminary: bool | None = None,
    preliminary_notice_tax: int = 0,
    preliminary_unrefunded: int = 0,
) -> Filing:
    fp = default_filed_preliminary(client) if filed_preliminary is None else filed_preliminary
    s, e = coverage(period, fp)
    return Filing(
        client_id=client.id,
        period=period,
        coverage_start=s,
        coverage_end=e,
        due_date=due_date(period, law, holidays),
        filed_preliminary=fp,
        preliminary_notice_tax=preliminary_notice_tax,
        preliminary_unrefunded=preliminary_unrefunded,
    )


def filing_required(client: Client, period: TaxPeriod, filed_preliminary: bool | None = None) -> bool:
    """이 회차에 신고서를 내야 하는가. 예정 회차는 예정신고 대상자만."""
    if not client.active:
        return False
    if period.kind == "F":
        return True
    fp = default_filed_preliminary(client) if filed_preliminary is None else filed_preliminary
    return fp


def current_period(today: date) -> TaxPeriod:
    """오늘 기준 '지금 준비해야 할' 회차 (기한이 다가오는 회차)."""
    m = today.month
    if m in (1,):
        return TaxPeriod(today.year - 1, 2, "F")
    if m in (2, 3, 4):
        return TaxPeriod(today.year, 1, "P")
    if m in (5, 6, 7):
        return TaxPeriod(today.year, 1, "F")
    if m in (8, 9, 10):
        return TaxPeriod(today.year, 2, "P")
    return TaxPeriod(today.year, 2, "F")  # 11, 12월 → 다음 1월 확정 준비

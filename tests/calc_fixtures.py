"""판정·계산·검증 테스트 공용 헬퍼(법 파라미터는 config 파일이 아닌 Law(table) 로 직접 주입)."""

from __future__ import annotations

from datetime import date

from taxauto.law import Law
from taxauto.models import (
    CardKind,
    Classification,
    Client,
    DecidedBy,
    Direction,
    DocType,
    Filing,
    PurchaseCategory,
    Source,
    TaxPeriod,
    TaxpayerType,
    Transaction,
)

POLICY = {
    "review": {
        "sales_change_warn_ratio": 0.30,
        "fixed_asset_review_amount": 1_000_000,
        "vat_rounding_tolerance": 1,
        "card_vs_ti_match_days": 7,
        "llm_auto_accept_confidence": 0.90,
    },
    "llm": {"enabled": False, "model": "test-model", "max_items_per_run": 300},
}


def v(value, **kw):
    return [{"value": value, "verified": True, **kw}]


def make_law(**overrides) -> Law:
    table = {
        "vat.rate": v(0.10),
        "deadline.1P": v("04-25"),
        "deadline.1F": v("07-25"),
        "deadline.2P": v("10-25"),
        "deadline.2F": v("01-25"),
        "card_issue_credit.rate": v(0.013, **{"from": "2024-01-01", "to": "2026-12-31"}),
        "card_issue_credit.annual_limit": v(10_000_000, **{"from": "2024-01-01", "to": "2026-12-31"}),
        "card_issue_credit.prior_year_supply_cap": v(1_000_000_000),
        "card_issue_credit.cap_to_payable": v(True),
        "tax_invoice.issue_deadline_day": v(10),
        "penalty.ti_late_issue": v(0.01),
        "penalty.ti_not_issued": v(0.02),
        "penalty.eti_late_transmit": v(0.003),
        "penalty.eti_not_transmit": v(0.005),
        "penalty.ti_late_receipt": v(0.005),
        "deemed_input.restaurant_individual_le_200m": v("9/109", to="2026-12-31"),
        "deemed_input.restaurant_individual": v("8/108"),
        "deemed_input.restaurant_corp": v("6/106"),
        "deemed_input.other": v("2/102"),
        "deemed_input.limit.individual_restaurant_le_100m": v(0.75, to="2027-12-31"),
        "deemed_input.limit.individual_restaurant_100m_200m": v(0.70, to="2027-12-31"),
        "deemed_input.limit.individual_restaurant_gt_200m": v(0.60, to="2027-12-31"),
        "deemed_input.limit.corp": v(0.50, to="2027-12-31"),
    }
    for k, val in overrides.items():
        key = k.replace("__", ".")
        if val is None:
            table.pop(key, None)
        else:
            table[key] = val
    return Law(table)


def client(**kw) -> Client:
    d = dict(id="C001", name="테스트상회", biz_no="1234567890", taxpayer_type=TaxpayerType.INDIVIDUAL,
             industry="소매", prior_year_supply=500_000_000)
    d.update(kw)
    return Client(**d)


def filing(code: str = "2026-2F", filed_preliminary: bool = False, start=None, end=None, notice: int = 0, unrefunded: int = 0) -> Filing:
    from taxauto.period import coverage

    p = TaxPeriod.parse(code)
    s, e = coverage(p, filed_preliminary)
    return Filing(
        client_id="C001", period=p, coverage_start=start or s, coverage_end=end or e,
        due_date=date(2027, 1, 25), filed_preliminary=filed_preliminary,
        preliminary_notice_tax=notice, preliminary_unrefunded=unrefunded,
    )


_row = [0]


def tx(source: Source, direction: Direction, doc: DocType, d: date, supply: int, vat: int, total: int = 0, **kw) -> Transaction:
    _row[0] += 1
    kw.setdefault("row_no", _row[0])
    kw.setdefault("source_file", "test.xlsx")
    return Transaction(client_id="C001", source=source, direction=direction, doc_type=doc, tx_date=d,
                       supply_amount=supply, vat=vat, total=total, **kw)


def sale_ti(d, supply, vat, **kw):
    return tx(Source.ETAX_SALES, Direction.SALES, DocType.TAX_INVOICE, d, supply, vat, **kw)


def sale_card(d, total, **kw):
    # 카드매출 원천자료는 합계(공급대가)만 있는 경우가 많다 → 공급가액=합계, 세액 0 으로 넣고 계산에서 분리
    return tx(Source.CARD_SALES, Direction.SALES, DocType.CARD, d, total, 0, total=total, **kw)


def buy_ti(d, supply, vat, cls: Classification | None = None, **kw):
    t = tx(Source.ETAX_PURCHASE, Direction.PURCHASE, DocType.TAX_INVOICE, d, supply, vat, **kw)
    t.classification = cls
    return t


def buy_card(d, supply, vat, cls: Classification | None = None, kind=CardKind.BUSINESS, **kw):
    t = tx(Source.CARD_PURCHASE, Direction.PURCHASE, DocType.CARD, d, supply, vat, card_kind=kind, **kw)
    t.classification = cls
    return t


def C(cat: PurchaseCategory, **kw) -> Classification:
    kw.setdefault("decided_by", DecidedBy.RULE)
    return Classification(category=cat, **kw)

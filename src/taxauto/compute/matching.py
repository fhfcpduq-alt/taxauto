"""거래 매칭 유틸 (classify·compute·validate 공용).

- 카드·현금영수증·판매대행 매출 ↔ 세금계산서 매출 중복(같은 거래에 두 증빙 발급)
- 카드·현금영수증 매입 ↔ 세금계산서 매입 중복(같은 거래상대·같은 금액)

'강한 매칭'(strong)만 자동 제외 대상으로 쓰고, 약한 매칭은 검증(V006)에서 사람에게 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..models import Direction, DocType, Source, Transaction

# 카드·현금영수증 계열 매출 소스
CARD_LIKE_SALES_SOURCES = {Source.CARD_SALES, Source.CASH_RECEIPT_SALES, Source.PG_SALES}
CARD_LIKE_PURCHASE_SOURCES = {Source.CARD_PURCHASE, Source.CASH_RECEIPT_PURCHASE}

# normalize 단계가 원천자료에서 '세금계산서 발급분' 표시를 찾았을 때 남기는 raw 키
RAW_TI_DUPLICATE_KEY = "tax_invoice_duplicate"


@dataclass
class MatchPair:
    receipt: Transaction       # 카드·현금영수증·판매대행 쪽
    tax_invoice: Transaction   # 세금계산서 쪽
    day_diff: int
    strong: bool               # True 면 자동 제외해도 되는 수준(1:1, 금액 동일, 상대방 일치 또는 유일 후보)
    reason: str = ""


def is_card_like_sale(t: Transaction) -> bool:
    if t.direction != Direction.SALES:
        return False
    return t.source in CARD_LIKE_SALES_SOURCES or (
        t.source in (Source.MANUAL, Source.OTHER_SALES) and t.doc_type in (DocType.CARD, DocType.CASH_RECEIPT)
    )


def is_card_like_purchase(t: Transaction) -> bool:
    if t.direction != Direction.PURCHASE or t.source == Source.WEHAGO_LEDGER:
        return False
    return t.source in CARD_LIKE_PURCHASE_SOURCES or (
        t.source == Source.MANUAL and t.doc_type in (DocType.CARD, DocType.CASH_RECEIPT)
    )


def is_tax_invoice(t: Transaction, direction: Direction) -> bool:
    return t.direction == direction and t.doc_type == DocType.TAX_INVOICE and t.source != Source.WEHAGO_LEDGER


def has_explicit_ti_duplicate_flag(t: Transaction) -> bool:
    return bool((t.raw or {}).get(RAW_TI_DUPLICATE_KEY))


def _match(
    receipts: list[Transaction],
    invoices: list[Transaction],
    days: int,
    require_same_counterparty: bool,
) -> list[MatchPair]:
    # 금액(합계) 기준 후보 찾기
    cands: dict[str, list[tuple[int, Transaction]]] = {}
    inv_cands: dict[str, int] = {}
    for r in receipts:
        lst = []
        for ti in invoices:
            if ti.total != r.total or r.total == 0:
                continue
            dd = abs((ti.tx_date - r.tx_date).days)
            if dd > days:
                continue
            if r.counterparty_biz_no and ti.counterparty_biz_no and r.counterparty_biz_no != ti.counterparty_biz_no:
                continue
            if require_same_counterparty and not (r.counterparty_biz_no and r.counterparty_biz_no == ti.counterparty_biz_no):
                continue
            lst.append((dd, ti))
            inv_cands[ti.id] = inv_cands.get(ti.id, 0) + 1
        if lst:
            lst.sort(key=lambda x: (x[0], x[1].tx_date, x[1].id))
            cands[r.id] = lst

    used: set[str] = set()
    pairs: list[MatchPair] = []
    # 결정적 순서: 날짜·id
    for r in sorted(receipts, key=lambda x: (x.tx_date, x.id)):
        lst = cands.get(r.id)
        if not lst:
            continue
        for dd, ti in lst:
            if ti.id in used:
                continue
            used.add(ti.id)
            same_cp = bool(r.counterparty_biz_no and r.counterparty_biz_no == ti.counterparty_biz_no)
            unique = len(lst) == 1 and inv_cands.get(ti.id, 0) == 1
            strong = same_cp or unique
            reason = "거래상대 사업자번호·금액 일치" if same_cp else ("금액 일치·유일 후보" if unique else "금액 일치·복수 후보")
            pairs.append(MatchPair(r, ti, dd, strong, reason))
            break
    return pairs


def match_card_sales_to_tax_invoices(txns: Iterable[Transaction], days: int) -> list[MatchPair]:
    """카드·현금영수증·판매대행 매출 ↔ 매출 세금계산서. 같은 합계금액, 일자 차이 ≤ days."""
    txns = list(txns)
    receipts = [t for t in txns if is_card_like_sale(t)]
    invoices = [t for t in txns if is_tax_invoice(t, Direction.SALES)]
    return _match(receipts, invoices, days, require_same_counterparty=False)


def match_card_purchases_to_tax_invoices(txns: Iterable[Transaction], days: int) -> list[MatchPair]:
    """카드·현금영수증 매입 ↔ 매입 세금계산서. 같은 거래상대(사업자번호)·같은 합계금액, 일자 차이 ≤ days."""
    txns = list(txns)
    receipts = [t for t in txns if is_card_like_purchase(t)]
    invoices = [t for t in txns if is_tax_invoice(t, Direction.PURCHASE)]
    return _match(receipts, invoices, days, require_same_counterparty=True)


def card_sales_duplicate_ids(txns: Iterable[Transaction], days: int) -> set[str]:
    """신고서 3번에서 자동 제외할 카드매출 id (원천 표시 + 강한 매칭)."""
    txns = list(txns)
    ids = {t.id for t in txns if is_card_like_sale(t) and has_explicit_ti_duplicate_flag(t)}
    ids |= {p.receipt.id for p in match_card_sales_to_tax_invoices(txns, days) if p.strong}
    return ids

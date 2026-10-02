"""홈택스 원천 자료(Transaction) ↔ 위하고 전표(LedgerEntry) 짝짓기 → 학습 사례.

짝짓기 순서(먼저 맞는 것 확정, 한 원천은 한 전표에만):
  1) 승인번호 일치
  2) 일자 + 금액 + 사업자번호 일치
  3) 일자 ±3일 + 금액 일치 (동점이면 사업자번호·상호 유사도·날짜차 순)
원천 자료가 없으면 전표만으로 사례를 만든다(match='ledger_only').
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Direction, Source, Transaction
from .features import Features, make_features, name_similarity, style_config, summary_template
from .ledger_import import LedgerEntry

NEAR_DAYS = 3


@dataclass
class LearnPair:
    ledger: LedgerEntry
    source: Transaction | None = None
    match: str = "ledger_only"     # approval | date_amount_biz | near_date_amount | ledger_only


@dataclass
class PairingResult:
    pairs: list[LearnPair] = field(default_factory=list)
    unpaired_sources: list[Transaction] = field(default_factory=list)

    @property
    def counts(self) -> dict:
        c: dict[str, int] = {}
        for p in self.pairs:
            c[p.match] = c.get(p.match, 0) + 1
        c["source_unpaired"] = len(self.unpaired_sources)
        return c


def _appr(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", s or "")


def _amount_eq(e: LedgerEntry, t: Transaction) -> bool:
    if e.total and t.total and e.total == t.total:
        return True
    return e.supply_amount == t.supply_amount and e.vat == t.vat


def pair_entries(entries: list[LedgerEntry], sources: list[Transaction], near_days: int = NEAR_DAYS) -> PairingResult:
    srcs = [t for t in sources if t.source != Source.WEHAGO_LEDGER]
    used: set[str] = set()
    by_appr: dict[str, list[Transaction]] = {}
    by_total: dict[int, list[Transaction]] = {}
    for t in srcs:
        if _appr(t.approval_no):
            by_appr.setdefault(_appr(t.approval_no), []).append(t)
        by_total.setdefault(t.total, []).append(t)
        if t.supply_amount + t.vat != t.total:
            by_total.setdefault(t.supply_amount + t.vat, []).append(t)
    matched: dict[int, tuple[Transaction, str]] = {}

    def free(t: Transaction, e: LedgerEntry) -> bool:
        return t.id not in used and t.direction.value == e.direction

    # 1) 승인번호
    for i, e in enumerate(entries):
        a = _appr(e.approval_no)
        if not a:
            continue
        for t in by_appr.get(a, []):
            if free(t, e):
                matched[i] = (t, "approval")
                used.add(t.id)
                break
    # 2) 일자+금액+사업자번호
    for i, e in enumerate(entries):
        if i in matched or not e.counterparty_biz_no:
            continue
        for t in by_total.get(e.total, []) + by_total.get(e.supply_amount + e.vat, []):
            if free(t, e) and t.tx_date == e.tx_date and t.counterparty_biz_no == e.counterparty_biz_no and _amount_eq(e, t):
                matched[i] = (t, "date_amount_biz")
                used.add(t.id)
                break
    # 3) 일자±N + 금액
    for i, e in enumerate(entries):
        if i in matched:
            continue
        cands = []
        for t in by_total.get(e.total, []) + by_total.get(e.supply_amount + e.vat, []):
            if free(t, e) and abs((t.tx_date - e.tx_date).days) <= near_days and _amount_eq(e, t):
                biz = 1 if e.counterparty_biz_no and e.counterparty_biz_no == t.counterparty_biz_no else 0
                sim = name_similarity(e.counterparty_name, t.counterparty_name)
                cands.append((-biz, -sim, abs((t.tx_date - e.tx_date).days), t.id, t))
        if cands:
            cands.sort(key=lambda x: x[:4])
            t = cands[0][4]
            matched[i] = (t, "near_date_amount")
            used.add(t.id)
    res = PairingResult()
    for i, e in enumerate(entries):
        if i in matched:
            res.pairs.append(LearnPair(e, matched[i][0], matched[i][1]))
        else:
            res.pairs.append(LearnPair(e))
    res.unpaired_sources = [t for t in srcs if t.id not in used]
    return res


# ---------------------------------------------------------------------------
# 학습 사례
# ---------------------------------------------------------------------------


def pair_features(p: LearnPair, group: str, cfg: dict | None = None) -> Features:
    """적용 시점(홈택스 원천 기준)과 같은 특징이 되도록 원천이 있으면 원천 값을 우선."""
    e, t = p.ledger, p.source
    if t is not None:
        return make_features(
            e.client_id, group, t.direction.value, t.doc_type.value, t.counterparty_biz_no or e.counterparty_biz_no,
            t.counterparty_name or e.counterparty_name, t.merchant_category, t.supply_amount, t.total,
            t.item or e.item, t.tx_date.month, cfg,
        )
    return make_features(
        e.client_id, group, e.direction, e.doc_type, e.counterparty_biz_no, e.counterparty_name, "",
        e.supply_amount, e.total, e.item, e.tx_date.month, cfg,
    )


def labels_of(e: LedgerEntry, f: Features) -> dict:
    purchase = e.direction == Direction.PURCHASE.value
    return {
        "account": e.account,
        "entry_type": e.entry_type,
        "nd_reason": (e.nd_reason or "없음") if purchase else "",
        "settlement": e.settlement,
        "summary": summary_template(e.summary, f),
        "summary_raw": e.summary,
        "fixed_asset": ("Y" if e.fixed_asset else "N") if purchase else "",
        "deemed": bool(e.deemed) if e.deemed is not None else None,
        "electronic": e.electronic,
    }


def make_example(p: LearnPair, group: str, cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else style_config()
    f = pair_features(p, group, cfg)
    return {
        **f.to_dict(),
        "tx_date": (p.source.tx_date if p.source else p.ledger.tx_date).isoformat(),
        "match": p.match,
        "source_file": p.ledger.source_file,
        "row_no": p.ledger.row_no,
        "labels": labels_of(p.ledger, f),
    }


def build_examples(pairs: list[LearnPair], group: str, cfg: dict | None = None) -> list[dict]:
    return [make_example(p, group, cfg) for p in pairs]

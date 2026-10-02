"""매입 공제판정 룰엔진 (config/rules/purchase.yaml).

룰 = 조건(when) → 결과(then). 위에서부터 첫 매칭. 결정적(같은 입력 → 같은 결과).
세법 숫자는 룰 파일에도 쓰지 않는다: 금액 기준은 policy 경로, 업종 목록은 law 키로 참조.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..compute.lawutil import law_try, policy_get
from ..compute.matching import match_card_purchases_to_tax_invoices
from ..law import CONFIG_DIR, Law
from ..models import (
    Classification,
    Client,
    DecidedBy,
    Direction,
    DocType,
    ExclusionReason,
    NonDeductibleReason,
    PurchaseCategory,
    Source,
    Transaction,
)

DEFAULT_RULES_PATH = CONFIG_DIR / "rules" / "purchase.yaml"


@dataclass
class Rule:
    id: str
    when: dict
    then: dict
    handler: str = ""
    before_memory: bool = False


@dataclass
class RuleContext:
    client: Client
    policy: dict
    law: Law | None
    on: date
    ti_duplicate_ids: set[str] = field(default_factory=set)   # 세금계산서와 중복인 카드·현금영수증 매입 id
    missing_law_keys: set[str] = field(default_factory=set)   # 조회 실패한 law 키(경고용)


# ---------------------------------------------------------------------------
# 로딩
# ---------------------------------------------------------------------------


def load_rules(path: Path | None = None) -> list[Rule]:
    return list(_load_rules_cached(str(path or DEFAULT_RULES_PATH)))


@lru_cache(maxsize=8)
def _load_rules_cached(path: str) -> tuple[Rule, ...]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out = []
    for r in data.get("rules") or []:
        out.append(
            Rule(
                id=str(r["id"]),
                when=dict(r.get("when") or {}),
                then=dict(r.get("then") or {}),
                handler=str(r.get("handler") or ""),
                before_memory=bool(r.get("before_memory", False)),
            )
        )
    return tuple(out)


def build_context(
    txns: Iterable[Transaction], client: Client, policy: dict, law: Law | None = None, on: date | None = None
) -> RuleContext:
    txns = list(txns)
    days = int(policy_get(policy, "review.card_vs_ti_match_days", 7))
    dup = {p.receipt.id for p in match_card_purchases_to_tax_invoices(txns, days)}
    on = on or max((t.tx_date for t in txns), default=date.today())
    return RuleContext(client=client, policy=policy, law=law, on=on, ti_duplicate_ids=dup)


# ---------------------------------------------------------------------------
# 조건 평가
# ---------------------------------------------------------------------------


def _enum(cls: type[Enum], v: Any) -> Any:
    if v in (None, ""):
        return None
    try:
        return cls(v)
    except ValueError:
        return cls[str(v)]


def _vals(cls: type[Enum], items: list) -> set:
    return {_enum(cls, x) for x in items or []}


def _text(t: Transaction) -> str:
    return " ".join(x for x in (t.counterparty_name, t.merchant_category, t.item, t.memo) if x)


def _merchant(t: Transaction) -> str:
    return " ".join(x for x in (t.counterparty_name, t.merchant_category) if x)


def _num(v: Any, ctx: RuleContext) -> int | None:
    if isinstance(v, str) and v.startswith("policy:"):
        x = policy_get(ctx.policy, v[len("policy:"):])
        return None if x is None else int(x)
    return None if v is None else int(v)


def _re(pattern: str, s: str) -> bool:
    return re.search(pattern, s or "", flags=re.IGNORECASE) is not None


def _law_list(ctx: RuleContext, key: str) -> list[str]:
    if ctx.law is None:
        ctx.missing_law_keys.add(key)
        return []
    v, err = law_try(ctx.law, key, ctx.on)
    if err or not isinstance(v, (list, tuple)):
        ctx.missing_law_keys.add(key)
        return []
    return [str(x) for x in v if str(x).strip()]


def match_when(t: Transaction, when: dict, ctx: RuleContext) -> bool:
    for k, v in when.items():
        if k == "sources":
            if t.source not in _vals(Source, v):
                return False
        elif k == "doc_types":
            if t.doc_type not in _vals(DocType, v):
                return False
        elif k == "text_regex":
            if not _re(v, _text(t)):
                return False
        elif k == "merchant_regex":
            if not _re(v, _merchant(t)):
                return False
        elif k == "item_regex":
            if not _re(v, t.item):
                return False
        elif k == "client_industry_regex":
            if not _re(v, ctx.client.industry):
                return False
        elif k == "supply_min":
            n = _num(v, ctx)
            if n is None or t.supply_amount < n:
                return False
        elif k == "supply_max":
            n = _num(v, ctx)
            if n is None or t.supply_amount > n:
                return False
        elif k == "vat_min":
            if t.vat < _num(v, ctx):
                return False
        elif k == "vat_max":
            if t.vat > _num(v, ctx):
                return False
        elif k == "tax_type_contains":
            if not any(s in (t.counterparty_tax_type or "") for s in v):
                return False
        elif k == "tax_type_not_contains":
            if any(s in (t.counterparty_tax_type or "") for s in v):
                return False
        elif k == "deductible_flag":
            if t.deductible_flag_from_source is None or bool(t.deductible_flag_from_source) != bool(v):
                return False
        elif k == "closed_before_tx":
            closed = t.counterparty_closed_on is not None and t.counterparty_closed_on <= t.tx_date
            if closed != bool(v):
                return False
        elif k == "has_matching_tax_invoice":
            if (t.id in ctx.ti_duplicate_ids) != bool(v):
                return False
        elif k == "client_deemed_input":
            if bool(ctx.client.deemed_input_type) != bool(v):
                return False
        elif k == "zero_rated":
            if bool(t.zero_rated) != bool(v):
                return False
        elif k == "law_keywords":
            kws = _law_list(ctx, str(v))
            m = _merchant(t)
            if not kws or not any(kw in m for kw in kws):
                return False
        elif k == "any_of":
            if not any(match_when(t, sub or {}, ctx) for sub in v):
                return False
        else:
            raise ValueError(f"알 수 없는 룰 조건: {k}")
    return True


# ---------------------------------------------------------------------------
# 결과 생성
# ---------------------------------------------------------------------------


def make_classification(rule: Rule, then: dict | None = None) -> Classification:
    th = then if then is not None else rule.then
    decided = DecidedBy.DEFAULT if str(th.get("decided_by", "")).lower() == "default" else DecidedBy.RULE
    return Classification(
        category=_enum(PurchaseCategory, th["category"]),
        non_deductible_reason=_enum(NonDeductibleReason, th.get("non_deductible_reason")),
        exclusion_reason=_enum(ExclusionReason, th.get("exclusion_reason")),
        decided_by=decided,
        rule_id=rule.id,
        confidence=float(th.get("confidence", 1.0)),
        needs_review=bool(th.get("needs_review", False)),
        note=str(th.get("note", "")),
    )


def _plain(s: str) -> str:
    return re.sub(r"[\s\-]", "", s or "")


def _vehicle_handler(t: Transaction, rule: Rule, ctx: RuleContext) -> Classification:
    """거래처 차량정보로 판정. 판정 불가면 룰의 기본 결과(needs_review)."""
    vehicles = ctx.client.vehicles or []
    hay = _plain(" ".join([_text(t)] + [str(v) for v in (t.raw or {}).values()]))
    hit = [v for v in vehicles if v.plate and _plain(v.plate) in hay]
    if len(hit) == 1:
        v = hit[0]
        target, why = [v], f"차량번호 {v.plate} 일치"
    elif vehicles and all(v.deductible for v in vehicles):
        target, why = vehicles, "등록 차량이 모두 공제대상 차량"
    elif vehicles and not any(v.deductible for v in vehicles):
        target, why = vehicles, "등록 차량이 모두 비영업용 소형승용차"
    else:
        c = make_classification(rule)
        if not vehicles:
            c.note += " (거래처 차량정보 없음)"
        else:
            c.note += " (공제/불공제 차량이 섞여 있어 자동판정 불가)"
        return c
    if target[0].deductible:
        c = make_classification(rule, {"category": "일반매입", "confidence": 0.85, "note": f"차량 관련 지출 - {why} → 공제"})
    else:
        c = make_classification(
            rule,
            {
                "category": "불공제",
                "non_deductible_reason": "비영업용소형승용자동차",
                "confidence": 0.85,
                "note": f"차량 관련 지출 - {why} → 불공제",
            },
        )
    return c


HANDLERS = {"vehicle": _vehicle_handler}


def apply_rule(t: Transaction, rule: Rule, ctx: RuleContext) -> Classification | None:
    if not match_when(t, rule.when, ctx):
        return None
    if rule.handler:
        h = HANDLERS.get(rule.handler)
        if h is None:
            raise ValueError(f"알 수 없는 룰 handler: {rule.handler}")
        return h(t, rule, ctx)
    return make_classification(rule)


def classify_by_rules(
    t: Transaction, rules: list[Rule], ctx: RuleContext, phase: str = "all"
) -> Classification | None:
    """phase: 'pre'(before_memory 룰만) | 'post'(나머지) | 'all'."""
    if t.direction != Direction.PURCHASE:
        return None
    for r in rules:
        if phase == "pre" and not r.before_memory:
            continue
        if phase == "post" and r.before_memory:
            continue
        c = apply_rule(t, r, ctx)
        if c is not None:
            return c
    return None


def fixed_asset_hint(t: Transaction, policy: dict, rules: list[Rule] | None = None) -> bool:
    """'fixed_asset' 룰 조건(금액·키워드)에 맞는가 — 불공제 세금계산서를 10/11 중 어디에 둘지 판단용."""
    rules = rules if rules is not None else load_rules()
    rule = next((r for r in rules if r.id == "fixed_asset"), None)
    if rule is None:
        return False
    ctx = RuleContext(client=Client(id="", name="", biz_no=""), policy=policy, law=None, on=t.tx_date)
    return match_when(t, rule.when, ctx)

"""일반과세자 부가가치세 신고서 독립 재계산.

compute_return(txns, client, filing, law, policy, settings) -> VatReturn

원칙
- coverage(집계기간) 안의 거래만 집계. 예정신고를 한 확정 회차에서 직전 예정기간 날짜 거래는
  '예정신고누락분'(7번/12번)으로 본다(이미 예정신고에 포함된 거래 id 는 제외).
- 세금계산서 수취분은 공제 여부와 무관하게 전부 10/11(13)에 넣고, 불공제분은 16에 다시 적어 차감(서식 원칙).
- 카드·현금영수증 매입은 공제분만 14(수령명세서)에 넣는다. 카드 불공제·신고제외분은 신고서 밖.
- 세법 숫자는 law 키로만 조회. 키가 없으면 해당 항목 0 + 검토항목(경고).
- 가산세(26)는 자동 추정값을 넣지 않는다(0 + 수동조정만). 추정액은 validate 가 검토항목으로 보여 준다.
- 모든 금액 int, 원 미만 절사.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction
from typing import Any, Iterable

from ..law import Law
from ..models import (
    CardKind,
    Classification,
    Client,
    DecidedBy,
    Direction,
    DocType,
    Filing,
    Line,
    LineValue,
    NonDeductibleReason,
    PurchaseCategory,
    ReviewItem,
    Severity,
    Source,
    TaxPeriod,
    TaxpayerType,
    Transaction,
    VatReturn,
)
from ..period import months as period_months
from ..registry import FilingSettings
from .lawutil import law_try, mul_floor, policy_get, to_fraction
from .matching import card_sales_duplicate_ids, is_card_like_purchase, is_card_like_sale

# 14번 내역 키
OD_CARD_GENERAL = "신용카드등_일반"
OD_CARD_FIXED = "신용카드등_고정"
OD_DEEMED = "의제매입세액"
OD_MANUAL = "수동조정"
CASH_RECEIPT_KEY = "현금영수증"

# 수동조정으로 직접 바꾸면 안 되는 합계 라인
TOTAL_LINES = {Line.S_TOTAL, Line.P_TOTAL, Line.P_NET, Line.PAYABLE, Line.C_TOTAL, Line.FINAL}

RAW_BUYER_ISSUED_KEY = "buyer_issued"   # normalize 가 매입자발행 세금계산서에 남기는 표시


@dataclass
class ComputeResult:
    ret: VatReturn
    issues: list[ReviewItem] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


class _LawReader:
    """law 조회 + 사용 키 기록 + 누락 시 검토항목."""

    def __init__(self, law: Law, on: date, issue_fn):
        self.law, self.on, self.issue = law, on, issue_fn
        self.used: set[str] = set()

    def get(self, key: str, purpose: str, warn: bool = True) -> Any:
        v, err = law_try(self.law, key, self.on)
        if err is None:
            self.used.add(key)
            return v
        if warn:
            self.issue(
                "C001_LAW_PARAM_MISSING",
                Severity.WARN,
                f"법 파라미터 없음: {key} → {purpose} 0원 처리",
                detail=f"{err}. config/law/vat.yaml 에 적용기간 값이 있는지 세법검증 담당 확인 필요.",
                action=f"{purpose} 금액을 위하고에서 직접 계산해 입력하고, 법 파라미터 추가 후 재실행",
            )
        return None


def _is_buyer_issued(t: Transaction) -> bool:
    return bool((t.raw or {}).get(RAW_BUYER_ISSUED_KEY)) or "매입자발행" in (t.memo or "")


def _in(d: date, s: date, e: date) -> bool:
    return s <= d <= e


def _prelim_range(filing: Filing) -> tuple[date, date] | None:
    """예정신고를 한 확정 회차 → 직전 예정기간(3개월)."""
    p = filing.period
    if p.kind != "F" or not filing.filed_preliminary:
        return None
    s_m, e_m = period_months(TaxPeriod(p.year, p.half, "P"))
    start = date(p.year, s_m, 1)
    end = date(p.year, e_m + 1, 1) - timedelta(days=1)
    return start, end


def _months_between(s: date, e: date) -> int:
    return (e.year - s.year) * 12 + e.month - s.month + 1


def compute_return(
    txns: Iterable[Transaction],
    client: Client,
    filing: Filing,
    law: Law,
    policy: dict,
    settings: FilingSettings | None = None,
    prior_reported_ids: set[str] | None = None,
) -> VatReturn:
    return compute_return_detailed(txns, client, filing, law, policy, settings, prior_reported_ids).ret


def compute_return_detailed(
    txns: Iterable[Transaction],
    client: Client,
    filing: Filing,
    law: Law,
    policy: dict,
    settings: FilingSettings | None = None,
    prior_reported_ids: set[str] | None = None,
) -> ComputeResult:
    settings = settings or FilingSettings()
    prior_reported_ids = prior_reported_ids or set()
    period_code = filing.period.code
    ret = VatReturn(client_id=client.id, period=period_code)
    issues: list[ReviewItem] = []
    notes = ret.notes

    def issue(code: str, sev: Severity, title: str, detail: str = "", action: str = "", tx_ids=None, impact: int = 0):
        issues.append(
            ReviewItem(
                client_id=client.id,
                period=period_code,
                code=code,
                severity=sev,
                title=title,
                detail=detail,
                tx_ids=list(tx_ids or []),
                tax_impact=impact,
                suggested_action=action,
            )
        )

    on = filing.coverage_end
    lr = _LawReader(law, on, issue)
    for ln in Line:
        ret.line(ln)  # 모든 칸을 0으로 미리 만들어 둔다(리포트·대사 편의)

    if client.taxpayer_type == TaxpayerType.SIMPLE:
        notes.append("간이과세자는 v1 계산 범위 밖입니다. 신고서 값은 참고용이 아닙니다.")

    # ------------------------------------------------------------------ 기간 분류
    prelim = _prelim_range(filing)
    current: list[Transaction] = []
    omitted: list[Transaction] = []
    out_of_range: list[Transaction] = []
    already: list[Transaction] = []
    for t in txns:
        if t.source == Source.WEHAGO_LEDGER:
            continue
        if _in(t.tx_date, filing.coverage_start, filing.coverage_end):
            current.append(t)
        elif prelim and _in(t.tx_date, *prelim):
            (already if t.id in prior_reported_ids else omitted).append(t)
        else:
            out_of_range.append(t)

    notes.append(
        f"집계기간 {filing.coverage_start.isoformat()} ~ {filing.coverage_end.isoformat()} "
        f"({_months_between(filing.coverage_start, filing.coverage_end)}개월) 거래 {len(current)}건을 집계했습니다."
    )
    if omitted:
        notes.append(
            f"직전 예정기간 날짜 거래 {len(omitted)}건을 예정신고누락분(매출 7번/매입 12번)으로 반영했습니다. "
            "예정신고에 이미 포함된 거래라면 제외해야 합니다."
        )
    if already:
        notes.append(f"예정신고에 이미 포함된 직전 예정기간 거래 {len(already)}건은 제외했습니다.")
    if out_of_range:
        notes.append(f"집계기간 밖 거래 {len(out_of_range)}건은 신고서에서 제외했습니다.")

    rate = lr.get("vat.rate", "매출세액 계산(카드매출 공급가액 분리)")
    vat_ratio = (to_fraction(rate) / (1 + to_fraction(rate))) if rate is not None else None

    # ------------------------------------------------------------------ 매출
    days = int(policy_get(policy, "review.card_vs_ti_match_days", 7))
    dup_ids = card_sales_duplicate_ids(current + omitted, days)
    card_credit_base = 0          # 신용카드매출전표등 발행금액(공급대가) - 과세분
    card_credit_base_zr = 0
    tax_free_income = 0
    dup_total = dup_vat = 0
    split_ids: list[str] = []

    def sales_amounts(t: Transaction) -> tuple[int, int]:
        if is_card_like_sale(t) and not t.zero_rated and t.vat == 0 and t.total > 0 and t.supply_amount in (0, t.total):
            if vat_ratio is None:
                return t.total, 0
            v = int(Fraction(t.total) * vat_ratio)
            split_ids.append(t.id)
            return t.total - v, v
        return t.supply_amount, t.vat

    def sales_line(t: Transaction) -> Line | None:
        if t.doc_type == DocType.INVOICE or t.source == Source.EINV_SALES:
            return None
        if t.doc_type == DocType.TAX_INVOICE:
            if _is_buyer_issued(t):
                return Line.S_BUYER_ISSUED
            return Line.S_ZR_TAX_INVOICE if t.zero_rated else Line.S_TAX_INVOICE
        if is_card_like_sale(t):
            return Line.S_ZR_OTHER if t.zero_rated else Line.S_CARD_CASH
        return Line.S_ZR_OTHER if t.zero_rated else Line.S_OTHER

    for group, is_omitted in ((current, False), (omitted, True)):
        for t in group:
            if t.direction != Direction.SALES:
                continue
            ln = sales_line(t)
            if ln is None:
                tax_free_income += t.supply_amount or t.total
                continue
            card_like = is_card_like_sale(t)
            if card_like:
                if t.zero_rated:
                    card_credit_base_zr += t.total
                else:
                    card_credit_base += t.total
            if t.id in dup_ids:
                dup_total += t.total
                dup_vat += t.vat or (int(Fraction(t.total) * vat_ratio) if vat_ratio else 0)
                continue
            amt, tax = sales_amounts(t)
            ret.line(Line.S_PRELIM_OMITTED if is_omitted else ln).add(amt, tax)

    if dup_ids:
        notes.append(
            f"세금계산서와 중복으로 판단한 카드·현금영수증 매출 {len(dup_ids)}건(공급대가 {dup_total:,}원)을 3번에서 제외했습니다"
            "(세금계산서 1번으로 신고). 발행세액공제 계산에는 포함했습니다."
        )
    if split_ids:
        notes.append(f"공급가액·세액 구분이 없는 카드·현금영수증 매출 {len(split_ids)}건은 공급대가 × 세율/(1+세율)로 세액을 분리했습니다(원 미만 절사).")
        if client.has_tax_free_business:
            issue(
                "C006_CARD_SALES_SPLIT_TAXFREE",
                Severity.WARN,
                "겸영사업자 카드매출을 전부 과세로 계산함 - 면세분 구분 필요",
                detail=f"공급대가에서 세액을 분리한 카드·현금영수증 매출 {len(split_ids)}건",
                action="위하고 신용카드매출 집계에서 면세분을 분리해 3번 금액을 조정(수동조정 S_CARD_CASH)",
                tx_ids=split_ids,
            )
    if tax_free_income:
        notes.append(f"면세 계산서 발급분 {tax_free_income:,}원은 신고서 밖(면세수입금액·계산서합계표)입니다.")

    # ------------------------------------------------------------------ 매입
    from ..classify.rules import fixed_asset_hint, load_rules  # 순환 import 회피

    rules = load_rules()
    deemed_base = 0
    deemed_ids: list[str] = []
    card_nd_cnt = card_nd_vat = 0
    unclassified: list[str] = []

    def cls_of(t: Transaction) -> Classification:
        if t.classification is None:
            unclassified.append(t.id)
            return Classification(category=PurchaseCategory.GENERAL, decided_by=DecidedBy.DEFAULT)
        return t.classification

    def card_key(t: Transaction) -> str:
        if t.doc_type == DocType.CASH_RECEIPT or t.source == Source.CASH_RECEIPT_PURCHASE:
            return CASH_RECEIPT_KEY
        return (t.card_kind or CardKind.BUSINESS).value

    for group, is_omitted in ((current, False), (omitted, True)):
        for t in group:
            if t.direction != Direction.PURCHASE:
                continue
            c = cls_of(t)
            cat = c.category
            if cat == PurchaseCategory.NOT_APPLICABLE:
                continue
            if t.doc_type == DocType.TAX_INVOICE:
                if cat == PurchaseCategory.DEEMED_INPUT:
                    cat = PurchaseCategory.GENERAL  # 세금계산서는 의제매입 대상 아님
                if is_omitted:
                    ln = Line.P_PRELIM_OMITTED
                elif _is_buyer_issued(t):
                    ln = Line.P_BUYER_ISSUED
                elif cat == PurchaseCategory.FIXED_ASSET or (
                    cat == PurchaseCategory.NON_DEDUCTIBLE and fixed_asset_hint(t, policy, rules)
                ):
                    ln = Line.P_TI_FIXED
                else:
                    ln = Line.P_TI_GENERAL
                ret.line(ln).add(t.supply_amount, t.vat)
                if cat == PurchaseCategory.NON_DEDUCTIBLE:
                    reason = (c.non_deductible_reason or NonDeductibleReason.OTHER).value
                    ret.line(Line.P_NON_DEDUCTIBLE).add(t.supply_amount, t.vat)
                    ret.non_deductible_breakdown.setdefault(reason, LineValue()).add(t.supply_amount, t.vat)
                continue
            if cat == PurchaseCategory.DEEMED_INPUT:
                if t.doc_type == DocType.TAX_INVOICE:
                    continue
                deemed_base += t.total
                deemed_ids.append(t.id)
                continue
            if is_card_like_purchase(t):
                if cat == PurchaseCategory.NON_DEDUCTIBLE:
                    card_nd_cnt += 1
                    card_nd_vat += t.vat
                    continue
                if is_omitted:
                    ret.line(Line.P_PRELIM_OMITTED).add(t.supply_amount, t.vat)
                else:
                    key = OD_CARD_FIXED if cat == PurchaseCategory.FIXED_ASSET else OD_CARD_GENERAL
                    ret.other_deductible_breakdown.setdefault(key, LineValue()).add(t.supply_amount, t.vat)
                ret.card_receipt_summary.setdefault(card_key(t), LineValue()).add(t.supply_amount, t.vat)
                continue
            # 계산서·증빙없음 등 의제매입이 아닌 것은 신고서 밖

    if unclassified:
        issue(
            "C005_UNCLASSIFIED_PURCHASE",
            Severity.WARN,
            f"공제판정 없는 매입 {len(unclassified)}건을 일반매입으로 계산함",
            detail="classify 단계가 실행되지 않았거나 새로 추가된 거래입니다.",
            action="classify 단계를 다시 실행한 뒤 compute 재실행",
            tx_ids=unclassified,
        )
    if card_nd_cnt:
        notes.append(f"카드·현금영수증 매입 중 불공제 판정 {card_nd_cnt}건(세액 {card_nd_vat:,}원)은 수령명세서에서 제외했습니다.")
    nd = ret.line(Line.P_NON_DEDUCTIBLE)
    if nd.tax:
        notes.append(f"세금계산서 수취분 중 불공제 {nd.count}건(세액 {nd.tax:,}원)은 10/11번에 포함하고 16번에서 차감했습니다.")

    # ------------------------------------------------------------------ 수동조정(합계 라인 제외)
    total_line_adj = []
    for a in settings.adjustments:
        if a.line in TOTAL_LINES:
            total_line_adj.append(a)
            continue
        if a.line == Line.P_OTHER_DEDUCTIBLE:
            ret.other_deductible_breakdown.setdefault(OD_MANUAL, LineValue()).add(a.amount, a.tax)
        else:
            ret.line(a.line).add(a.amount, a.tax)
            if a.line == Line.P_NON_DEDUCTIBLE:
                ret.non_deductible_breakdown.setdefault(OD_MANUAL, LineValue()).add(a.amount, a.tax)
        notes.append(f"수동조정 반영: {a.line.value}번 금액 {a.amount:,}원 세액 {a.tax:,}원 ({a.note or '사유 미기재'})")
    if total_line_adj:
        issue(
            "C004_ADJUSTMENT_IGNORED",
            Severity.WARN,
            f"합계 라인 수동조정 {len(total_line_adj)}건은 반영하지 않음",
            detail="; ".join(f"{a.line.value}번 {a.amount:,}/{a.tax:,} ({a.note})" for a in total_line_adj),
            action="합계 라인 대신 구성 라인(예: 3번, 14번, 18번)에 조정값을 넣고 재실행",
        )

    # ------------------------------------------------------------------ 매출 합계(9) - 의제매입 한도 계산에 필요
    sales_lines = [Line.S_TAX_INVOICE, Line.S_BUYER_ISSUED, Line.S_CARD_CASH, Line.S_OTHER,
                   Line.S_ZR_TAX_INVOICE, Line.S_ZR_OTHER, Line.S_PRELIM_OMITTED]
    s9 = ret.line(Line.S_TOTAL)
    s9.amount = sum(ret.line(x).amount for x in sales_lines)
    s9.tax = sum(ret.line(x).tax for x in sales_lines) + ret.line(Line.S_BAD_DEBT).tax
    s9.count = sum(ret.line(x).count for x in sales_lines)

    # ------------------------------------------------------------------ 의제매입
    if deemed_base:
        credit, used_base = _deemed_input(client, deemed_base, s9.amount, filing, lr, issue, notes, deemed_ids)
        ret.other_deductible_breakdown.setdefault(OD_DEEMED, LineValue()).add(used_base, credit, len(deemed_ids))

    # 14 = 내역 합계
    l14 = ret.line(Line.P_OTHER_DEDUCTIBLE)
    l14.amount = sum(v.amount for v in ret.other_deductible_breakdown.values())
    l14.tax = sum(v.tax for v in ret.other_deductible_breakdown.values())
    l14.count = sum(v.count for v in ret.other_deductible_breakdown.values())

    # 15 = 10 - 10-1 + 11 + 12 + 13 + 14
    p15 = ret.line(Line.P_TOTAL)
    plus = [Line.P_TI_GENERAL, Line.P_TI_FIXED, Line.P_PRELIM_OMITTED, Line.P_BUYER_ISSUED, Line.P_OTHER_DEDUCTIBLE]
    p15.amount = sum(ret.line(x).amount for x in plus) - ret.line(Line.P_TI_EXPORT_DEFER).amount
    p15.tax = sum(ret.line(x).tax for x in plus) - ret.line(Line.P_TI_EXPORT_DEFER).tax
    # 17 = 15 - 16
    p17 = ret.line(Line.P_NET)
    p17.amount = p15.amount - ret.line(Line.P_NON_DEDUCTIBLE).amount
    p17.tax = p15.tax - ret.line(Line.P_NON_DEDUCTIBLE).tax
    # ㉰ = ㉮ - ㉯
    payable = ret.line(Line.PAYABLE)
    payable.tax = s9.tax - p17.tax

    # ------------------------------------------------------------------ 19 신용카드매출전표등 발행공제
    adj19 = ret.line(Line.C_CARD_ISSUE).tax  # 수동조정분
    credit = _card_issue_credit(
        client, settings, card_credit_base, payable.tax - ret.line(Line.C_OTHER).tax - ret.line(Line.C_SMALL_BIZ).tax,
        lr, issue, notes,
    )
    l19 = ret.line(Line.C_CARD_ISSUE)
    l19.amount += card_credit_base
    l19.tax = adj19 + credit
    if card_credit_base_zr:
        notes.append(f"영세율 카드·현금영수증 매출 {card_credit_base_zr:,}원은 발행세액공제 대상 금액에서 제외했습니다.")

    # 20 = 18 + 19
    c20 = ret.line(Line.C_TOTAL)
    c20.tax = ret.line(Line.C_OTHER).tax + l19.tax

    # 21, 22 (Filing 값, 없으면 회차설정 값)
    pu = filing.preliminary_unrefunded or settings.preliminary_unrefunded
    pn = filing.preliminary_notice_tax or settings.preliminary_notice_tax
    ret.line(Line.PRELIM_UNREFUNDED).tax += pu
    ret.line(Line.PRELIM_NOTICE).tax += pn
    if pn:
        notes.append(f"예정고지세액 {pn:,}원을 22번에 차감했습니다.")
    if pu:
        notes.append(f"예정신고 미환급세액 {pu:,}원을 21번에 차감했습니다.")

    # 26 가산세: 자동 추정값은 넣지 않음
    notes.append("가산세(26번)는 자동 계산하지 않았습니다(가산세 별도 검토). 검증 단계의 가산세 추정 항목을 확인하세요.")

    # 27 = ㉰ - 20 - 20-1 - 21 - 22 - 23 - 24 - 25 + 26
    minus = [Line.C_TOTAL, Line.C_SMALL_BIZ, Line.PRELIM_UNREFUNDED, Line.PRELIM_NOTICE,
             Line.PROXY_TRANSFEREE, Line.PROXY_BUYER, Line.PROXY_CARD]
    final = ret.line(Line.FINAL)
    final.tax = payable.tax - sum(ret.line(x).tax for x in minus) + ret.line(Line.PENALTY).tax

    notes.append(
        f"산식: ㉮ 매출세액 {s9.tax:,} - ㉯ 매입세액 {p17.tax:,} = ㉰ {payable.tax:,}; "
        f"- 경감공제 {c20.tax:,} - 예정미환급 {pu:,} - 예정고지 {pn:,} + 가산세 {ret.line(Line.PENALTY).tax:,} "
        f"(기타 차감 {sum(ret.line(x).tax for x in (Line.C_SMALL_BIZ, Line.PROXY_TRANSFEREE, Line.PROXY_BUYER, Line.PROXY_CARD)):,}) "
        f"= 27번 {final.tax:,}원 {'환급' if final.tax < 0 else '납부'}."
    )

    unverified = sorted(k for k in lr.used if k in law.used_unverified)
    if unverified:
        notes.append("미검증 법 파라미터 사용: " + ", ".join(unverified) + " (세법검증 섹터 확인 전 값)")

    meta = {
        "period": period_code,
        "coverage_start": filing.coverage_start.isoformat(),
        "coverage_end": filing.coverage_end.isoformat(),
        "months": _months_between(filing.coverage_start, filing.coverage_end),
        "law_keys_used": sorted(lr.used),
        "unverified_keys": unverified,
        "card_sales_dup_excluded_ids": sorted(dup_ids),
        "prelim_omitted_ids": sorted(t.id for t in omitted),
        "prelim_already_reported_ids": sorted(t.id for t in already),
        "out_of_range_ids": sorted(t.id for t in out_of_range),
        "card_credit_base": card_credit_base,
        "deemed_input_base": deemed_base,
        "tax_free_income": tax_free_income,
    }
    return ComputeResult(ret=ret, issues=issues, meta=meta)


# ---------------------------------------------------------------------------
# 신용카드매출전표등 발행세액공제 (19)
# ---------------------------------------------------------------------------


def _card_issue_credit(client: Client, settings: FilingSettings, base: int, payable_before: int, lr: _LawReader, issue, notes) -> int:
    if base <= 0:
        return 0
    if client.taxpayer_type != TaxpayerType.INDIVIDUAL:
        notes.append("법인(또는 간이)은 신용카드매출전표등 발행세액공제 대상이 아니어서 19번 세액은 0입니다(발행금액만 기재).")
        return 0
    cap_supply = lr.get("card_issue_credit.prior_year_supply_cap", "신용카드발행공제(직전연도 공급가액 기준)")
    if cap_supply is None:
        return 0
    if client.prior_year_supply is None:
        issue(
            "C002_PRIOR_YEAR_SUPPLY_UNKNOWN",
            Severity.WARN,
            "직전연도 공급가액 미입력 - 신용카드발행공제를 적용해 계산함",
            detail=f"직전연도 공급가액이 {int(cap_supply):,}원을 넘으면 공제 대상이 아닙니다.",
            action="거래처 명부(clients.yaml)에 prior_year_supply 입력 후 재실행. 초과 사업자면 위하고 19번 공제 삭제",
        )
    elif client.prior_year_supply > int(cap_supply):
        notes.append(
            f"직전연도 공급가액 {client.prior_year_supply:,}원이 기준({int(cap_supply):,}원)을 넘어 신용카드발행공제를 적용하지 않았습니다."
        )
        return 0
    rate = lr.get("card_issue_credit.rate", "신용카드발행공제")
    limit = lr.get("card_issue_credit.annual_limit", "신용카드발행공제(연 한도)")
    if rate is None or limit is None:
        return 0
    raw = mul_floor(base, rate)
    remaining = max(0, int(limit) - int(settings.card_credit_used_this_year))
    credit = min(raw, remaining)
    msg = (
        f"신용카드발행공제: 발행금액(공급대가) {base:,}원 × {rate} = {raw:,}원, "
        f"연 한도 {int(limit):,}원 - 올해 기공제 {settings.card_credit_used_this_year:,}원 = 잔여 {remaining:,}원"
    )
    cap_flag = lr.get("card_issue_credit.cap_to_payable", "", warn=False)
    if cap_flag is None:
        cap_flag = True
        msg += " (납부세액 한도 키 없음 → 한도 적용으로 가정)"
    if cap_flag:
        cap = max(0, payable_before)
        if credit > cap:
            msg += f", 납부세액({cap:,}원) 초과분 불인정"
        credit = min(credit, cap)
    notes.append(msg + f" → 19번 {credit:,}원.")
    return credit


# ---------------------------------------------------------------------------
# 의제매입세액 (14 내역)
# ---------------------------------------------------------------------------


def _deemed_rate_key(client: Client, lr: _LawReader, sales_base: int, issue) -> str | None:
    t = client.deemed_input_type
    corp = client.is_corporation
    if t == "restaurant":
        if corp:
            return "deemed_input.restaurant_corp"
        thr = lr.get("deemed_input.restaurant_individual_le_threshold", "", warn=False)
        if thr is not None and sales_base <= int(thr):
            return "deemed_input.restaurant_individual_le_200m"
        if thr is None:
            issue(
                "C003_DEEMED_INPUT_RATE",
                Severity.WARN,
                "의제매입 공제율: 개인 음식점 과세표준 기준 확인 필요(낮은 율 적용)",
                detail="과세표준 기준 금액 파라미터(deemed_input.restaurant_individual_le_threshold)가 없어 "
                "deemed_input.restaurant_individual 율로 계산했습니다.",
                action="과세표준이 기준 이하이면 위하고 의제매입세액공제신고서에서 높은 공제율로 변경",
            )
        return "deemed_input.restaurant_individual"
    if t == "manufacturing":
        issue(
            "C003_DEEMED_INPUT_RATE",
            Severity.WARN,
            "의제매입 공제율: 제조업 세부업종 확인 필요",
            detail="중소·개인 제조업 기본율로 계산했습니다. 과자점·도정·떡류 등은 더 높은 율, 중소기업 외 법인은 낮은 율입니다.",
            action="위하고 의제매입세액공제신고서에서 업종별 공제율 확인",
        )
        return "deemed_input.manufacturing_individual_sme"
    if t == "other":
        return "deemed_input.other"
    return None


def _deemed_limit_ratio(client: Client, lr: _LawReader, sales_base: int) -> Fraction | None:
    """deemed_input.limit.* 키를 관대한 형식으로 해석. 숫자(비율) 또는 [{upto, ratio}] 구간 목록."""
    who = "corp" if client.is_corporation else "individual"
    for key in (f"deemed_input.limit.{client.deemed_input_type}_{who}", f"deemed_input.limit.{who}"):
        v = lr.get(key, "", warn=False)
        if v is None:
            continue
        if isinstance(v, (int, float, str)):
            return to_fraction(v)
        if isinstance(v, list):
            for tier in v:
                if not isinstance(tier, dict):
                    continue
                upto = tier.get("upto", tier.get("max"))
                r = tier.get("ratio", tier.get("value"))
                if r is not None and (upto is None or sales_base <= int(upto)):
                    return to_fraction(r)
    return None


def _deemed_input(client: Client, base: int, sales_base: int, filing: Filing, lr: _LawReader, issue, notes, ids) -> tuple[int, int]:
    if not client.deemed_input_type:
        issue(
            "C003_DEEMED_INPUT_TYPE",
            Severity.WARN,
            "의제매입 후보가 있으나 거래처 의제매입 업종이 설정되지 않음 - 0원 처리",
            action="clients.yaml 에 deemed_input_type 지정 후 재실행",
            tx_ids=ids,
        )
        return 0, base
    key = _deemed_rate_key(client, lr, sales_base, issue)
    if key is None:
        issue("C003_DEEMED_INPUT_TYPE", Severity.WARN, f"알 수 없는 의제매입 업종: {client.deemed_input_type} - 0원 처리", tx_ids=ids)
        return 0, base
    rate = lr.get(key, "의제매입세액")
    if rate is None:
        return 0, base
    used_base = base
    ratio = _deemed_limit_ratio(client, lr, sales_base)
    if ratio is None:
        issue(
            "C003_DEEMED_INPUT_LIMIT",
            Severity.WARN,
            "의제매입세액 한도 미적용 - 한도 계산 필요",
            detail=f"한도율 법 파라미터(deemed_input.limit.*)가 없어 매입가액 {base:,}원 전액에 공제율을 적용했습니다.",
            action="위하고 의제매입세액공제신고서에서 과세표준 × 한도율 한도를 확인해 14번 의제매입 금액 조정",
            tx_ids=ids,
        )
    else:
        limit_base = int(Fraction(sales_base) * ratio)
        if base > limit_base:
            notes.append(f"의제매입 한도: 과세표준 {sales_base:,}원 × 한도율 {ratio} = {limit_base:,}원까지만 공제대상으로 반영했습니다.")
            used_base = limit_base
        if filing.period.kind == "P" or filing.filed_preliminary:
            notes.append("의제매입 한도는 과세기간(6개월) 단위로 확정신고 때 정산해야 합니다(이번 계산은 집계기간 기준 근사).")
    credit = mul_floor(used_base, rate)
    notes.append(f"의제매입세액: 매입가액 {used_base:,}원 × {rate} = {credit:,}원 (의제매입세액공제신고서 첨부 필요).")
    return credit, used_base

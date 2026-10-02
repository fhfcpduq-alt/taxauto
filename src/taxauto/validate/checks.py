"""검증 함수 모음. 함수 1개 = 검증코드 1개, 순수 함수(CheckInput → list[ReviewItem]).

tax_impact 부호: +면 (지적대로 고치면) 납부세액 증가, -면 감소. 가산세 추정액은 + 로 표시.
가산세 추정액은 참고용이며 신고서 26번에는 자동 반영하지 않는다.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction
from typing import Callable

from ..compute.lawutil import law_try, mul_floor, policy_get
from ..compute.matching import (
    card_sales_duplicate_ids,
    is_card_like_purchase,
    is_card_like_sale,
    match_card_sales_to_tax_invoices,
)
from ..law import Law
from ..models import (
    Client,
    DecidedBy,
    Direction,
    DocType,
    Filing,
    Line,
    PurchaseCategory,
    ReviewItem,
    Severity,
    Source,
    TaxPeriod,
    TaxpayerType,
    Transaction,
    VatReturn,
)
from ..period import due_date as period_due_date
from ..period import next_business_day
from ..registry import FilingSettings

MAX_DETAIL_ROWS = 15


@dataclass
class CheckInput:
    client: Client
    filing: Filing
    txns: list[Transaction]
    law: Law
    policy: dict
    ret: VatReturn | None = None
    settings: FilingSettings = field(default_factory=FilingSettings)
    parse_issues: list[dict] = field(default_factory=list)
    raw_files: list[str] = field(default_factory=list)
    wehago_return: VatReturn | None = None
    previous_sales: int | None = None        # 전기 과세표준(9번 금액)
    previous_months: int | None = None       # 전기 집계 개월수(모르면 None)
    compute_meta: dict = field(default_factory=dict)
    holidays: set[date] = field(default_factory=set)

    # -- 편의
    @property
    def period(self) -> str:
        return self.filing.period.code

    def in_coverage(self, t: Transaction) -> bool:
        return self.filing.coverage_start <= t.tx_date <= self.filing.coverage_end

    @property
    def data_txns(self) -> list[Transaction]:
        """원천(홈택스 등) 거래 - 위하고 전표 제외."""
        return [t for t in self.txns if t.source != Source.WEHAGO_LEDGER]

    @property
    def reported_txns(self) -> list[Transaction]:
        """신고서 집계 대상(집계기간 내 + 예정신고누락분)."""
        omitted = set(self.compute_meta.get("prelim_omitted_ids") or [])
        return [t for t in self.data_txns if self.in_coverage(t) or t.id in omitted]


def _item(ci: CheckInput, code: str, sev: Severity, title: str, detail: str = "", tx_ids=None, impact: int = 0, action: str = "") -> ReviewItem:
    return ReviewItem(
        client_id=ci.client.id,
        period=ci.period,
        code=code,
        severity=sev,
        title=title,
        detail=detail,
        tx_ids=sorted(set(tx_ids or [])),
        tax_impact=int(impact),
        suggested_action=action,
    )


def _row(t: Transaction) -> str:
    return f"{t.tx_date.isoformat()} {t.counterparty_name or '(상호없음)'} 공급가액 {t.supply_amount:,} 세액 {t.vat:,}"


def _rows(ts: list[Transaction]) -> str:
    lines = [_row(t) for t in ts[:MAX_DETAIL_ROWS]]
    if len(ts) > MAX_DETAIL_ROWS:
        lines.append(f"... 외 {len(ts) - MAX_DETAIL_ROWS}건")
    return "\n".join(lines)


def _is_purchase_deductible(t: Transaction) -> bool:
    c = t.classification
    return c is None or c.category in (PurchaseCategory.GENERAL, PurchaseCategory.FIXED_ASSET)


def _final_due(ci: CheckInput, d: date) -> date | None:
    """공급일이 속한 과세기간의 확정신고기한."""
    try:
        return period_due_date(TaxPeriod(d.year, 1 if d.month <= 6 else 2, "F"), ci.law, ci.holidays)
    except Exception:
        return None


def _issue_deadline(ci: CheckInput, d: date) -> date | None:
    day, err = law_try(ci.law, "tax_invoice.issue_deadline_day", d)
    if err:
        return None
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return next_business_day(date(y, m, int(day)), ci.holidays)


def _rate(ci: CheckInput, key: str, on: date):
    v, err = law_try(ci.law, key, on)
    return None if err else v


# ---------------------------------------------------------------------------
# V001 홈택스 수집자료 vs 위하고 전표 / 위하고 신고서 대사
# ---------------------------------------------------------------------------

_CARDLIKE_DOCS = {DocType.CARD, DocType.CASH_RECEIPT, DocType.NONE}
_RECON_TAX_LINES = [ln for ln in Line if ln != Line.PENALTY]
_RECON_AMOUNT_LINES = [
    Line.S_TAX_INVOICE, Line.S_BUYER_ISSUED, Line.S_CARD_CASH, Line.S_OTHER, Line.S_ZR_TAX_INVOICE, Line.S_ZR_OTHER,
    Line.S_PRELIM_OMITTED, Line.S_TOTAL, Line.P_TI_GENERAL, Line.P_TI_FIXED, Line.P_PRELIM_OMITTED, Line.P_BUYER_ISSUED,
    Line.P_OTHER_DEDUCTIBLE, Line.P_TOTAL, Line.P_NON_DEDUCTIBLE, Line.P_NET,
]


def v001_reconcile_wehago(ci: CheckInput) -> list[ReviewItem]:
    out: list[ReviewItem] = []
    ledger = [t for t in ci.txns if t.source == Source.WEHAGO_LEDGER and ci.in_coverage(t)]
    if ledger:
        def key(t: Transaction) -> tuple[str, str]:
            return t.direction.value, t.doc_type.value

        home: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0])
        for t in ci.data_txns:
            if not ci.in_coverage(t):
                continue
            if t.direction == Direction.PURCHASE and is_card_like_purchase(t) and not _is_purchase_deductible(t):
                continue  # 위하고에는 공제분만 카드매입으로 입력
            v = home[key(t)]
            v[0] += t.supply_amount; v[1] += t.vat; v[2] += t.total
        led: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0])
        for t in ledger:
            v = led[key(t)]
            v[0] += t.supply_amount; v[1] += t.vat; v[2] += t.total
        for k in sorted(set(home) | set(led)):
            h, w = home.get(k, [0, 0, 0]), led.get(k, [0, 0, 0])
            if DocType(k[1]) in _CARDLIKE_DOCS:
                differs = h[2] != w[2]
                desc = f"합계 홈택스 {h[2]:,} / 위하고 {w[2]:,} (차이 {h[2] - w[2]:,})"
            else:
                differs = h[0] != w[0] or h[1] != w[1]
                desc = f"공급가액 홈택스 {h[0]:,} / 위하고 {w[0]:,}, 세액 홈택스 {h[1]:,} / 위하고 {w[1]:,}"
            if differs:
                sign = 1 if k[0] == Direction.SALES.value else -1
                out.append(
                    _item(
                        ci, "V001_LEDGER_MISMATCH", Severity.WARN,
                        f"{k[0]} {k[1]}: 홈택스 수집자료와 위하고 전표 합계 불일치",
                        detail=desc, impact=sign * (h[1] - w[1]),
                        action=f"위하고 매입매출전표 입력에서 {k[0]} '{k[1]}' 유형 전표를 홈택스 자료와 비교해 누락·중복 전표를 고친 뒤 전표를 다시 내보내기",
                    )
                )
    if ci.wehago_return is not None and ci.ret is not None:
        wl = ci.wehago_return.lines
        for ln in _RECON_TAX_LINES:
            if ln.name not in wl:
                continue
            ours, theirs = ci.ret.line(ln), wl[ln.name]
            if ln == Line.FINAL:  # 가산세는 우리 계산에 없으므로 제외하고 비교
                o_tax = ours.tax - ci.ret.line(Line.PENALTY).tax
                t_tax = theirs.tax - (wl[Line.PENALTY.name].tax if Line.PENALTY.name in wl else 0)
            else:
                o_tax, t_tax = ours.tax, theirs.tax
            amt_diff = ln in _RECON_AMOUNT_LINES and ours.amount != theirs.amount
            if o_tax != t_tax or amt_diff:
                out.append(
                    _item(
                        ci, "V001_RETURN_MISMATCH", Severity.BLOCKER,
                        f"위하고 신고서 {ln.value}번이 재계산 값과 다름",
                        detail=f"재계산 금액 {ours.amount:,} 세액 {o_tax:,} / 위하고 금액 {theirs.amount:,} 세액 {t_tax:,}",
                        impact=o_tax - t_tax if ln.value in ("9", "다", "27") else 0,
                        action=f"위하고 부가가치세 신고서 {ln.value}번 칸의 원천 전표(매입매출전표·부속서류)를 열어 차이 원인을 찾고 수정 후 신고서 재집계",
                    )
                )
    return out


# ---------------------------------------------------------------------------
# V002 공급가액 × 세율 vs 세액
# ---------------------------------------------------------------------------


def v002_vat_amount(ci: CheckInput) -> list[ReviewItem]:
    tol = int(policy_get(ci.policy, "review.vat_rounding_tolerance", 1))
    bad: list[Transaction] = []
    for t in ci.reported_txns:
        if t.doc_type != DocType.TAX_INVOICE:
            continue
        if t.zero_rated:
            if t.vat != 0:
                bad.append(t)
            continue
        rate = _rate(ci, "vat.rate", t.tx_date)
        if rate is None or t.vat == 0:  # 세액 0 세금계산서는 분류 단계(영세율 확인)에서 다룸
            continue
        if abs(mul_floor(t.supply_amount, rate) - t.vat) > tol:
            bad.append(t)
    if not bad:
        return []
    return [
        _item(
            ci, "V002_VAT_MISMATCH", Severity.WARN,
            f"세금계산서 {len(bad)}건: 공급가액×세율과 세액이 다름",
            detail=_rows(bad), tx_ids=[t.id for t in bad],
            action="홈택스 원본 세금계산서와 위하고 전표의 공급가액·세액을 대조하고, 오기재면 거래처에 수정세금계산서 발급 요청",
        )
    ]


# ---------------------------------------------------------------------------
# V003 승인번호 중복
# ---------------------------------------------------------------------------


def v003_duplicate_approval(ci: CheckInput) -> list[ReviewItem]:
    groups: dict[tuple, list[Transaction]] = defaultdict(list)
    for t in ci.data_txns:
        if not t.approval_no:
            continue
        k = (t.source.value, t.approval_no, t.card_no_masked, t.tx_date) if t.doc_type in (DocType.CARD, DocType.CASH_RECEIPT) else (t.source.value, t.approval_no)
        groups[k].append(t)
    out = []
    for k, ts in sorted(groups.items(), key=lambda x: str(x[0])):
        if len({t.id for t in ts}) < 2:
            continue
        sign = 1 if ts[0].direction == Direction.SALES else -1
        out.append(
            _item(
                ci, "V003_DUP_APPROVAL", Severity.WARN,
                f"승인번호 중복 {len(ts)}건({k[0]} {k[1]})",
                detail=_rows(ts), tx_ids=[t.id for t in ts], impact=-sign * sum(t.vat for t in ts[1:]),
                action="같은 자료가 두 번 수집됐는지(다른 파일에 중복 포함) 확인하고 위하고 전표에서 중복 전표 삭제",
            )
        )
    return out


# ---------------------------------------------------------------------------
# V004 폐업자 세금계산서 수취
# ---------------------------------------------------------------------------


def v004_closed_seller(ci: CheckInput) -> list[ReviewItem]:
    bad = [
        t for t in ci.reported_txns
        if t.direction == Direction.PURCHASE and t.doc_type == DocType.TAX_INVOICE
        and ((t.counterparty_closed_on and t.counterparty_closed_on <= t.tx_date) or (t.counterparty_status == "폐업" and not t.counterparty_closed_on))
    ]
    if not bad:
        return []
    deducted = sum(t.vat for t in bad if _is_purchase_deductible(t))
    return [
        _item(
            ci, "V004_CLOSED_SELLER", Severity.WARN,
            f"폐업자 발행 세금계산서 수취 {len(bad)}건",
            detail=_rows(bad) + "\n(폐업일 이후 작성분은 사실과 다른 세금계산서로 매입세액 불공제 대상일 수 있음)",
            tx_ids=[t.id for t in bad], impact=deducted,
            action="거래처에 폐업 사실·실제 거래 여부 확인 후, 불공제면 위하고 매입매출전표 유형을 '불공'(사유: 기타)으로 변경",
        )
    ]


# ---------------------------------------------------------------------------
# V005 카드매입 신고제외 요약
# ---------------------------------------------------------------------------


def v005_card_excluded_summary(ci: CheckInput) -> list[ReviewItem]:
    ex = [
        t for t in ci.reported_txns
        if is_card_like_purchase(t) and t.classification
        and t.classification.category in (PurchaseCategory.NOT_APPLICABLE, PurchaseCategory.NON_DEDUCTIBLE)
    ]
    if not ex:
        return []
    by: dict[str, list[Transaction]] = defaultdict(list)
    for t in ex:
        c = t.classification
        r = (c.exclusion_reason.value if c.exclusion_reason else None) or (c.non_deductible_reason.value if c.non_deductible_reason else "기타")
        by[r].append(t)
    detail = "\n".join(f"- {r}: {len(ts)}건, 세액 {sum(t.vat for t in ts):,}원" for r, ts in sorted(by.items()))
    return [
        _item(
            ci, "V005_CARD_EXCLUDED", Severity.INFO,
            f"카드·현금영수증 매입 {len(ex)}건(세액 {sum(t.vat for t in ex):,}원)을 공제에서 제외",
            detail=detail,
            action="위하고 신용카드매출전표등 수령명세서에 위 거래가 빠져 있는지 확인(공제 대상으로 바꿀 건은 검토화면에서 결정)",
        )
    ]


# ---------------------------------------------------------------------------
# V006 카드매출 - 세금계산서 중복 의심
# ---------------------------------------------------------------------------


def v006_card_ti_duplicate(ci: CheckInput) -> list[ReviewItem]:
    days = int(policy_get(ci.policy, "review.card_vs_ti_match_days", 7))
    txns = ci.reported_txns
    excluded = set(ci.compute_meta.get("card_sales_dup_excluded_ids") or card_sales_duplicate_ids(txns, days))
    pairs = match_card_sales_to_tax_invoices(txns, days)
    out = []
    auto = [t for t in txns if t.id in excluded]
    if auto:
        ti_of = {p.receipt.id: p.tax_invoice for p in pairs}
        detail = "\n".join(
            _row(t) + (f"  ↔ 세금계산서 {ti_of[t.id].tx_date.isoformat()} {ti_of[t.id].counterparty_name}" if t.id in ti_of else "  (원천자료 표시)")
            for t in auto[:MAX_DETAIL_ROWS]
        )
        out.append(
            _item(
                ci, "V006_CARD_TI_DUP_EXCLUDED", Severity.WARN,
                f"세금계산서와 중복으로 보고 카드매출 {len(auto)}건을 3번에서 제외함",
                detail=detail, tx_ids=[t.id for t in auto] + [ti_of[t.id].id for t in auto if t.id in ti_of],
                impact=sum(t.vat for t in auto),  # 중복이 아니면 이만큼 납부세액 증가
                action="위하고 신용카드매출전표등 발행금액집계표의 '세금계산서 발급금액'란에 넣고 3번 과세표준에서 뺐는지 확인",
            )
        )
    weak = [p for p in pairs if p.receipt.id not in excluded]
    if weak:
        out.append(
            _item(
                ci, "V006_CARD_TI_DUP_SUSPECT", Severity.WARN,
                f"카드매출과 세금계산서 중복 의심 {len(weak)}건(자동 제외 안 함)",
                detail="\n".join(f"{_row(p.receipt)} ↔ {p.tax_invoice.counterparty_name} ({p.reason}, {p.day_diff}일 차이)" for p in weak[:MAX_DETAIL_ROWS]),
                tx_ids=[p.receipt.id for p in weak] + [p.tax_invoice.id for p in weak],
                impact=-sum(p.receipt.vat for p in weak),
                action="같은 거래면 위하고에서 해당 카드매출 전표를 세금계산서 발급분으로 표시(발행금액집계표 세금계산서 발급금액란)해 이중 신고를 막기",
            )
        )
    return out


# ---------------------------------------------------------------------------
# V007 매출 세금계산서 지연발급·지연전송
# ---------------------------------------------------------------------------


def v007_sales_ti_timing(ci: CheckInput) -> list[ReviewItem]:
    kinds: dict[str, list[tuple[Transaction, int | None]]] = defaultdict(list)
    keys = {
        "late_issue": ("penalty.ti_late_issue", "지연발급"),
        "not_issued": ("penalty.ti_not_issued", "미발급(확정신고기한 후 발급)"),
        "late_transmit": ("penalty.eti_late_transmit", "지연전송"),
        "not_transmit": ("penalty.eti_not_transmit", "미전송(확정신고기한 후 전송)"),
        "paper": ("penalty.ti_paper_issued", "전자발급 의무자 종이발급"),
    }
    for t in ci.reported_txns:
        if t.direction != Direction.SALES or t.doc_type != DocType.TAX_INVOICE or t.is_amended:
            continue
        final_due = _final_due(ci, t.tx_date)
        if t.source == Source.PAPER_TAX_INVOICE and ci.client.is_corporation:
            kinds["paper"].append((t, None))
            continue  # 종이발급분은 발급 가산세만(전송 가산세 중복 없음)
        if t.issue_date is None or final_due is None:
            continue
        dl = _issue_deadline(ci, t.tx_date)
        if dl is not None and t.issue_date > dl:
            kinds["late_issue" if t.issue_date <= final_due else "not_issued"].append((t, None))
            continue  # 발급 가산세 적용분에는 전송 가산세 중복 적용 안 함
        if t.source == Source.ETAX_SALES and t.transmit_date is not None:
            tdl = next_business_day(t.issue_date + timedelta(days=1), ci.holidays)
            if t.transmit_date > tdl:
                kinds["late_transmit" if t.transmit_date <= final_due else "not_transmit"].append((t, None))
    out = []
    for kind, lst in kinds.items():
        key, label = keys[kind]
        ts = [t for t, _ in lst]
        est, missing = 0, False
        for t in ts:
            r = _rate(ci, key, t.tx_date)
            if r is None:
                missing = True
                continue
            est += mul_floor(abs(t.supply_amount), r)
        detail = _rows(ts) + (
            f"\n가산세 추정 {est:,}원(공급가액 × {key})" if not missing else f"\n법 파라미터 {key} 없음 → 가산세 추정 생략"
        )
        detail += "\n(발급기한은 공급일 다음 달 기한일 기준 - 월합계 등 특례 기준. 가산세는 신고서 26번에 자동 반영하지 않음)"
        out.append(
            _item(
                ci, f"V007_SALES_TI_{kind.upper()}", Severity.WARN,
                f"매출 세금계산서 {label} {len(ts)}건",
                detail=detail, tx_ids=[t.id for t in ts], impact=est,
                action="위하고 부가가치세 신고서 가산세명세(26번)에 세금계산서 "
                + ("지연발급·미발급" if kind in ("late_issue", "not_issued", "paper") else "지연전송·미전송")
                + " 가산세를 입력할지 담당 세무사 확인",
            )
        )
    return out


# ---------------------------------------------------------------------------
# V008 매입 세금계산서 지연수취
# ---------------------------------------------------------------------------


def v008_purchase_ti_late_receipt(ci: CheckInput) -> list[ReviewItem]:
    within, after, over = [], [], []
    for t in ci.data_txns:
        if t.direction != Direction.PURCHASE or t.doc_type != DocType.TAX_INVOICE or t.is_amended or t.issue_date is None:
            continue
        if not (ci.in_coverage(t) or t.id in set(ci.compute_meta.get("prelim_omitted_ids") or [])):
            continue
        dl, final_due = _issue_deadline(ci, t.tx_date), _final_due(ci, t.tx_date)
        if dl is None or final_due is None or t.issue_date <= dl:
            continue
        try:
            one_year = final_due.replace(year=final_due.year + 1)
        except ValueError:
            one_year = final_due.replace(year=final_due.year + 1, day=28)
        if t.issue_date <= final_due:
            within.append(t)
        elif t.issue_date <= one_year:
            after.append(t)
        else:
            over.append(t)
    out = []

    def est(ts):
        s, miss = 0, False
        for t in ts:
            r = _rate(ci, "penalty.ti_late_receipt", t.tx_date)
            if r is None:
                miss = True
            else:
                s += mul_floor(abs(t.supply_amount), r)
        return s, miss

    if within:
        s, miss = est(within)
        out.append(
            _item(
                ci, "V008_LATE_RECEIPT", Severity.WARN,
                f"매입 세금계산서 지연수취 {len(within)}건(공제 가능, 가산세 대상)",
                detail=_rows(within) + (f"\n지연수취 가산세 추정 {s:,}원" if not miss else "\n법 파라미터 penalty.ti_late_receipt 없음 → 추정 생략"),
                tx_ids=[t.id for t in within], impact=s,
                action="위하고 신고서 가산세명세(26번) '세금계산서 지연수취'에 공급가액 입력 여부 확인",
            )
        )
    if after:
        s, _ = est(after)
        out.append(
            _item(
                ci, "V008_RECEIPT_AFTER_DEADLINE", Severity.WARN,
                f"확정신고기한 후 발급받은 매입 세금계산서 {len(after)}건 - 해당 과세기간 수정신고·경정청구로 공제",
                detail=_rows(after) + f"\n지연수취 가산세 추정 {s:,}원",
                tx_ids=[t.id for t in after], impact=sum(t.vat for t in after if _is_purchase_deductible(t)),
                action="이번 신고서에서 빼고, 공급시기가 속한 과세기간에 대해 위하고에서 경정청구(또는 수정신고) 작성",
            )
        )
    if over:
        out.append(
            _item(
                ci, "V008_RECEIPT_OVER_1Y", Severity.WARN,
                f"공급시기 과세기간 확정신고기한 후 1년이 지나 받은 세금계산서 {len(over)}건 - 매입세액 불공제",
                detail=_rows(over), tx_ids=[t.id for t in over],
                impact=sum(t.vat for t in over if _is_purchase_deductible(t)),
                action="위하고 매입매출전표 유형을 '불공'(사유: 기타)으로 입력",
            )
        )
    return out


# ---------------------------------------------------------------------------
# V009 전기 대비 매출 변동
# ---------------------------------------------------------------------------


def v009_sales_change(ci: CheckInput) -> list[ReviewItem]:
    if ci.ret is None or ci.previous_sales is None:
        return []
    cur = ci.ret.line(Line.S_TOTAL).amount
    prev = int(ci.previous_sales)
    cur_m = int(ci.compute_meta.get("months") or 0) or (
        (ci.filing.coverage_end.year - ci.filing.coverage_start.year) * 12 + ci.filing.coverage_end.month - ci.filing.coverage_start.month + 1
    )
    prev_m = ci.previous_months or cur_m
    # 집계 개월수가 다르면 월평균으로 비교
    cur_n, prev_n = Fraction(cur, cur_m), Fraction(prev, prev_m)
    ratio = float(policy_get(ci.policy, "review.sales_change_warn_ratio", 0.3))
    if prev_n == 0:
        if cur_n == 0:
            return []
        change = None
    else:
        change = (cur_n - prev_n) / prev_n
        if abs(change) < Fraction(str(ratio)):
            return []
    pct = "전기 0원" if change is None else f"{float(change) * 100:+.1f}%"
    return [
        _item(
            ci, "V009_SALES_CHANGE", Severity.WARN,
            f"전기 대비 매출 변동 {pct}",
            detail=f"이번 과세표준 {cur:,}원({cur_m}개월) / 전기 {prev:,}원({prev_m}개월), 월평균으로 비교. 기준 ±{ratio * 100:.0f}%",
            action="매출 누락(카드·현금영수증·배달앱 정산자료 미수집) 또는 휴업·업종변경 여부를 거래처에 확인",
        )
    ]


# ---------------------------------------------------------------------------
# V010 고정자산 → 감가상각자산취득명세서
# ---------------------------------------------------------------------------


def v010_fixed_asset(ci: CheckInput) -> list[ReviewItem]:
    fa = [
        t for t in ci.reported_txns
        if t.direction == Direction.PURCHASE and t.classification and t.classification.category == PurchaseCategory.FIXED_ASSET
    ]
    if not fa:
        return []
    return [
        _item(
            ci, "V010_FIXED_ASSET", Severity.WARN,
            f"고정자산 매입 {len(fa)}건 - 감가상각자산취득명세서 작성 필요",
            detail=_rows(fa), tx_ids=[t.id for t in fa],
            action="위하고 부가세 부속서류 '건물등감가상각자산취득명세서'에 자산 구분(건물·기계·차량·기타)별로 입력하고, 소모품이면 일반매입으로 변경",
        )
    ]


# ---------------------------------------------------------------------------
# V011 검토 필요 판정 묶음
# ---------------------------------------------------------------------------


def v011_needs_review(ci: CheckInput) -> list[ReviewItem]:
    groups: dict[str, list[Transaction]] = defaultdict(list)
    for t in ci.reported_txns:
        c = t.classification
        if t.direction != Direction.PURCHASE or c is None or not c.needs_review:
            continue
        if c.category == PurchaseCategory.FIXED_ASSET:
            continue  # V010
        k = "llm" if c.decided_by == DecidedBy.LLM else (c.rule_id or c.decided_by.value)
        groups[k].append(t)
    out = []
    for k, ts in sorted(groups.items()):
        c0 = ts[0].classification
        nd = [t for t in ts if t.classification.category == PurchaseCategory.NON_DEDUCTIBLE]
        # 판정을 뒤집으면: 불공제 → 공제(-), 공제 → 불공제(+)
        impact = -sum(t.vat for t in nd) + sum(
            t.vat for t in ts if t.classification.category == PurchaseCategory.GENERAL
        )
        if k == "llm":
            title = f"AI 판정 매입 {len(ts)}건 확인 필요"
        else:
            title = f"매입 {len(ts)}건 확인 필요: {c0.note.split(' - ')[0] if c0.note else c0.category.value}"
        out.append(
            _item(
                ci, "V011_NEEDS_REVIEW", Severity.WARN, title,
                detail="\n".join(
                    f"{_row(t)} → {t.classification.category.value}"
                    f"{'(' + t.classification.non_deductible_reason.value + ')' if t.classification.non_deductible_reason else ''}"
                    for t in ts[:MAX_DETAIL_ROWS]
                ) + (f"\n... 외 {len(ts) - MAX_DETAIL_ROWS}건" if len(ts) > MAX_DETAIL_ROWS else ""),
                tx_ids=[t.id for t in ts], impact=impact,
                action="검토화면에서 건별로 공제/불공제를 확정(거래처 단위로 저장하면 다음 신고부터 자동)하고, 위하고 매입매출전표 유형(과세/불공/카과/카면)을 맞추기",
            )
        )
    return out


# ---------------------------------------------------------------------------
# V012 환급
# ---------------------------------------------------------------------------


def v012_refund(ci: CheckInput) -> list[ReviewItem]:
    if ci.ret is None:
        return []
    final = ci.ret.line(Line.FINAL).tax
    if final >= 0:
        return []
    return [
        _item(
            ci, "V012_REFUND", Severity.WARN,
            f"환급세액 {abs(final):,}원 발생",
            detail="환급 신고는 현장확인·소명 요청 가능성이 있습니다. 고정자산 취득·수출 등 환급 사유를 확인하세요.",
            action="위하고 신고서에서 환급 구분(일반/조기) 확인, 환급계좌 입력, 고정자산이면 감가상각자산취득명세서 첨부",
        )
    ]


# ---------------------------------------------------------------------------
# V013 예정고지세액 확인
# ---------------------------------------------------------------------------


def v013_prelim_notice(ci: CheckInput) -> list[ReviewItem]:
    f = ci.filing
    if f.period.kind != "F" or f.filed_preliminary or ci.client.taxpayer_type == TaxpayerType.SIMPLE:
        return []
    if (f.preliminary_notice_tax or ci.settings.preliminary_notice_tax) != 0:
        return []
    return [
        _item(
            ci, "V013_PRELIM_NOTICE_ZERO", Severity.WARN,
            "예정고지 대상인데 예정고지세액이 0원 - 홈택스 고지내역 확인",
            detail="예정신고를 하지 않은 확정 회차입니다. 고지세액이 50만원 미만이라 고지되지 않았는지, 고지됐는데 입력이 빠졌는지 확인하세요.",
            action="홈택스 [조회/발급 > 세금신고납부 > 고지내역]에서 예정고지세액 확인 후 회차설정(preliminary_notice_tax)과 위하고 신고서 22번에 입력",
        )
    ]


# ---------------------------------------------------------------------------
# V014 미검증 법 파라미터
# ---------------------------------------------------------------------------


def v014_unverified_law(ci: CheckInput) -> list[ReviewItem]:
    keys = set(ci.compute_meta.get("unverified_keys") or [])
    used = set(ci.compute_meta.get("law_keys_used") or [])
    keys |= {k for k in ci.law.used_unverified if not used or k in used}
    if not keys:
        return []
    return [
        _item(
            ci, "V014_UNVERIFIED_LAW", Severity.WARN,
            f"미검증 세법 파라미터 {len(keys)}개로 계산함",
            detail=", ".join(sorted(keys)),
            action="config/law/vat.yaml 해당 값의 근거를 세법검증 담당이 확인(verified: true)할 때까지 위하고 계산값과 직접 대조",
        )
    ]


# ---------------------------------------------------------------------------
# V015 파싱 이슈
# ---------------------------------------------------------------------------


def v015_parse_issues(ci: CheckInput) -> list[ReviewItem]:
    if not ci.parse_issues:
        return []
    blockers = [p for p in ci.parse_issues if p.get("severity") == Severity.BLOCKER.value]
    files = sorted({str(p.get("source_file", "")) for p in ci.parse_issues})
    detail = "\n".join(
        f"{p.get('source_file', '')} {p.get('row_no', '')}행: {p.get('message', '')}" for p in ci.parse_issues[:MAX_DETAIL_ROWS]
    )
    return [
        _item(
            ci, "V015_PARSE_ISSUES", Severity.BLOCKER if blockers else Severity.WARN,
            f"자료 읽기 오류 {len(ci.parse_issues)}건(파일 {len(files)}개)",
            detail=detail,
            action="해당 엑셀을 홈택스·위하고에서 다시 내려받아 inbox 에 넣고 재실행(양식이 바뀌었으면 담당자에게 알림)",
        )
    ]


# ---------------------------------------------------------------------------
# V016 자료 누락 의심
# ---------------------------------------------------------------------------

_CARD_HEAVY_INDUSTRY = ("음식", "식당", "요식", "카페", "커피", "주점", "제과", "베이커리", "치킨", "피자", "분식", "소매", "미용", "편의점")


def v016_missing_data(ci: CheckInput) -> list[ReviewItem]:
    data = ci.data_txns
    if not data and not ci.raw_files:
        return [
            _item(
                ci, "V016_NO_DATA", Severity.BLOCKER,
                "수집된 자료가 하나도 없음",
                detail="inbox·raw 에 파일이 없고 거래도 0건입니다.",
                action="홈택스에서 세금계산서·카드·현금영수증 자료를 내려받아 inbox/{회차}/{거래처} 에 넣고 재실행(무실적이면 무실적 신고로 처리)",
            )
        ]
    out = []
    sources = {t.source for t in data if ci.in_coverage(t)}
    card_sales = any(is_card_like_sale(t) for t in data if ci.in_coverage(t))
    ind = ci.client.industry or ""
    if not card_sales and any(k in ind for k in _CARD_HEAVY_INDUSTRY):
        out.append(
            _item(
                ci, "V016_NO_CARD_SALES", Severity.WARN,
                f"{ind} 업종인데 카드·현금영수증 매출 자료가 없음",
                detail="소매·음식점 등 소비자 대상 업종은 보통 카드매출이 있습니다.",
                action="홈택스 [신용카드 매출자료 조회]·현금영수증 매출내역·배달앱(판매대행) 정산자료를 내려받아 추가",
            )
        )
    if data and not any(t.direction == Direction.SALES for t in data if ci.in_coverage(t)):
        out.append(
            _item(
                ci, "V016_NO_SALES", Severity.WARN,
                "집계기간 매출 자료가 0건",
                detail="수집 자료: " + ", ".join(sorted(s.value for s in sources)),
                action="무실적 여부를 거래처에 확인하고, 아니면 매출 세금계산서·카드매출 자료 수집 누락 확인",
            )
        )
    return out


# ---------------------------------------------------------------------------
# V017 간이과세자
# ---------------------------------------------------------------------------


def v017_simple_taxpayer(ci: CheckInput) -> list[ReviewItem]:
    if ci.client.taxpayer_type != TaxpayerType.SIMPLE:
        return []
    return [
        _item(
            ci, "V017_SIMPLE_TAXPAYER", Severity.BLOCKER,
            "간이과세자 - 자동 계산 범위 밖",
            detail="이 엔진은 일반과세자 신고서만 계산합니다. 재계산 값은 사용하지 마세요.",
            action="위하고 간이과세자 신고서로 직접 작성",
        )
    ]


# ---------------------------------------------------------------------------
# V018 겸영사업자 공통매입세액 안분
# ---------------------------------------------------------------------------


def v018_mixed_business(ci: CheckInput) -> list[ReviewItem]:
    has_tf_sales = any(
        t.direction == Direction.SALES and (t.doc_type == DocType.INVOICE or t.source == Source.EINV_SALES)
        for t in ci.reported_txns
    )
    if not (ci.client.has_tax_free_business or has_tf_sales):
        return []
    if ci.client.has_tax_free_business:
        title = "겸영(과세+면세)사업자 - 공통매입세액 안분 필요(자동 계산 안 함)"
        detail = "공통매입세액 중 면세사업분은 공급가액 비율로 안분해 16번(공통매입세액면세사업분)에 넣어야 합니다."
    else:
        title = "면세 매출(계산서)이 있으나 겸영사업자로 등록되지 않음"
        detail = "면세 매출이 있으면 공통매입세액 안분 대상일 수 있습니다."
    return [
        _item(
            ci, "V018_COMMON_INPUT_TAX", Severity.WARN, title, detail=detail,
            action="위하고 '공제받지못할매입세액명세서 > 공통매입세액안분계산'에 과세·면세 공급가액을 넣어 계산하고, 결과를 회차설정 수동조정(P_NON_DEDUCTIBLE)에 입력",
        )
    ]


# ---------------------------------------------------------------------------
# V019 영세율 첨부서류
# ---------------------------------------------------------------------------


def v019_zero_rated(ci: CheckInput) -> list[ReviewItem]:
    zr = [t for t in ci.reported_txns if t.direction == Direction.SALES and t.zero_rated]
    if not zr:
        return []
    return [
        _item(
            ci, "V019_ZERO_RATED_DOCS", Severity.WARN,
            f"영세율 매출 {len(zr)}건(공급가액 {sum(t.supply_amount for t in zr):,}원) - 첨부서류 확인",
            detail=_rows(zr) + "\n영세율 첨부서류 미제출 시 영세율과세표준 신고불성실 가산세 대상이 될 수 있습니다.",
            tx_ids=[t.id for t in zr],
            action="수출실적명세서·내국신용장/구매확인서 전자발급명세서·영세율매출명세서를 위하고 부속서류에 입력·첨부",
        )
    ]


# ---------------------------------------------------------------------------
# V020 집계기간 밖 거래
# ---------------------------------------------------------------------------


def v020_out_of_coverage(ci: CheckInput) -> list[ReviewItem]:
    omitted_ids = set(ci.compute_meta.get("prelim_omitted_ids") or [])
    already = set(ci.compute_meta.get("prelim_already_reported_ids") or [])
    omitted = [t for t in ci.data_txns if t.id in omitted_ids]
    others = [t for t in ci.data_txns if not ci.in_coverage(t) and t.id not in omitted_ids and t.id not in already]
    out = []
    if omitted:
        st = sum(t.vat for t in omitted if t.direction == Direction.SALES)
        pt = sum(t.vat for t in omitted if t.direction == Direction.PURCHASE and _is_purchase_deductible(t))
        out.append(
            _item(
                ci, "V020_PRELIM_OMITTED", Severity.WARN,
                f"예정신고누락분으로 반영한 거래 {len(omitted)}건(매출세액 {st:,} / 매입세액 {pt:,})",
                detail=_rows(omitted) + "\n예정신고에 이미 포함된 거래면 제외해야 합니다. 누락분이 맞으면 과소신고·납부지연 가산세 검토.",
                tx_ids=[t.id for t in omitted], impact=st - pt,
                action="예정신고서와 대조 후 누락분이 맞으면 위하고 신고서 7번/12번(예정신고누락분 명세)에 입력하고 가산세 검토",
            )
        )
    if others:
        out.append(
            _item(
                ci, "V020_OUT_OF_COVERAGE", Severity.INFO,
                f"집계기간 밖 거래 {len(others)}건은 신고서에서 제외함",
                detail=_rows(others),
                tx_ids=[t.id for t in others],
                action="다른 회차 자료가 섞였는지 확인. 지난 과세기간 누락분이면 해당 기간 수정신고·경정청구 검토",
            )
        )
    return out


ALL_CHECKS: list[Callable[[CheckInput], list[ReviewItem]]] = [
    v001_reconcile_wehago,
    v002_vat_amount,
    v003_duplicate_approval,
    v004_closed_seller,
    v005_card_excluded_summary,
    v006_card_ti_duplicate,
    v007_sales_ti_timing,
    v008_purchase_ti_late_receipt,
    v009_sales_change,
    v010_fixed_asset,
    v011_needs_review,
    v012_refund,
    v013_prelim_notice,
    v014_unverified_law,
    v015_parse_issues,
    v016_missing_data,
    v017_simple_taxpayer,
    v018_mixed_business,
    v019_zero_rated,
    v020_out_of_coverage,
]


def run_checks(ci: CheckInput, checks=None) -> list[ReviewItem]:
    """모든 검증 실행. 개별 검증 오류는 검토항목으로 바꿔 계속 진행."""
    out: list[ReviewItem] = []
    for fn in checks or ALL_CHECKS:
        try:
            out.extend(fn(ci))
        except Exception as e:  # 검증 하나의 버그가 전체를 막지 않게
            out.append(
                _item(
                    ci, "V999_CHECK_ERROR", Severity.WARN,
                    f"검증 실행 오류: {fn.__name__}",
                    detail=f"{type(e).__name__}: {e}",
                    action="개발 담당자에게 알리고, 해당 항목은 위하고에서 수동 확인",
                )
            )
    return out

"""공통 데이터 모델 (모든 섹터가 공유하는 계약).

원칙
- 금액은 int(원). 부가세는 원 미만 절사.
- 모든 모델은 to_dict()/from_dict()로 JSON 왕복이 가능해야 한다.
  (파이프라인 각 단계 결과를 JSON 파일로 남겨서 사람·AI가 그대로 읽을 수 있게 하기 위함)
- 이 파일의 필드명/Enum 값을 바꾸면 전 섹터가 영향을 받는다. 추가는 자유, 변경·삭제는 금지.
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


class TaxpayerType(str, Enum):
    INDIVIDUAL = "개인일반"   # 개인 일반과세자
    CORPORATION = "법인"      # 법인 (일반과세자)
    SIMPLE = "간이"           # 간이과세자 - v1 범위 밖(검토항목으로만 표시)


class Direction(str, Enum):
    SALES = "매출"
    PURCHASE = "매입"


class Source(str, Enum):
    """자료 출처(수집 단위). 홈택스/위하고에서 내려받는 자료 단위와 1:1로 맞춘다."""

    ETAX_SALES = "전자세금계산서_매출"
    ETAX_PURCHASE = "전자세금계산서_매입"
    EINV_SALES = "전자계산서_매출"            # 면세(계산서)
    EINV_PURCHASE = "전자계산서_매입"
    CARD_SALES = "신용카드_매출"              # 카드사 매출자료(여신금융협회/홈택스)
    CASH_RECEIPT_SALES = "현금영수증_매출"
    PG_SALES = "판매대행_매출"                # 배민·쿠팡이츠·PG 등 판매(결제)대행 자료
    CARD_PURCHASE = "신용카드_매입"           # 사업용카드 + 화물복지카드 + 기타 카드
    CASH_RECEIPT_PURCHASE = "현금영수증_매입"  # 지출증빙
    PAPER_TAX_INVOICE = "종이세금계산서"       # 수기 입력분
    OTHER_SALES = "기타매출"                   # 정규영수증 외 매출(현금매출 등)
    WEHAGO_LEDGER = "위하고_전표"              # 위하고 매입매출전표 내보내기(대사용)
    MANUAL = "수동입력"


class DocType(str, Enum):
    TAX_INVOICE = "세금계산서"
    INVOICE = "계산서"               # 면세
    CARD = "신용카드"
    CASH_RECEIPT = "현금영수증"
    NONE = "증빙없음"


class CardKind(str, Enum):
    BUSINESS = "사업용신용카드"
    WELFARE = "화물운전자복지카드"
    OTHER = "기타신용카드"


class PurchaseCategory(str, Enum):
    """매입 분류 결과. 신고서 라인으로 바로 연결된다."""

    GENERAL = "일반매입"               # (10) 또는 (14) 일반
    FIXED_ASSET = "고정자산매입"        # (11) 또는 (14) 고정
    NON_DEDUCTIBLE = "불공제"          # (16) - reason 필수
    NOT_APPLICABLE = "신고제외"         # 면세·부가세 0·중복 등 신고서에 안 들어감
    DEEMED_INPUT = "의제매입후보"        # 계산서/카드 면세 농축수산물 등


class NonDeductibleReason(str, Enum):
    """공제받지 못할 매입세액 명세서 사유 (부가가치세법 제39조 기준)."""

    MISSING_INFO = "필요적기재사항누락"
    UNRELATED = "사업과직접관련없는지출"
    PASSENGER_CAR = "비영업용소형승용자동차"
    ENTERTAINMENT = "접대비및이와유사한비용"
    TAX_FREE_BIZ = "면세사업등관련"
    LAND = "토지의자본적지출관련"
    PRE_REGISTRATION = "사업자등록전매입세액"
    COMMON_TAX_FREE = "공통매입세액면세사업분"
    OTHER = "기타"


class ExclusionReason(str, Enum):
    """NOT_APPLICABLE(신고제외) 사유. 카드매입은 '공제 불가'가 아니라 '수령명세서 제외'로 처리."""

    NO_VAT = "부가세없음"                     # 면세물품·세액 0
    SIMPLE_TAXPAYER_SELLER = "간이과세자(영수증)가맹점"
    TAX_FREE_SELLER = "면세사업자가맹점"
    CLOSED_SELLER = "폐업자"
    DUPLICATE_TAX_INVOICE = "세금계산서중복"   # 같은 거래에 세금계산서도 받음
    PERSONAL = "개인사용"
    NON_DEDUCTIBLE_CARD = "카드공제불가업종"    # 목욕·이발·여객운송(전세버스 제외)·입장권 등
    OTHER = "기타"


class Severity(str, Enum):
    BLOCKER = "차단"   # 해결 전 신고 불가
    WARN = "경고"      # 사람 확인 필요
    INFO = "참고"


class ReviewStatus(str, Enum):
    OPEN = "미해결"
    RESOLVED = "해결"
    ACCEPTED = "확인후유지"


class DecidedBy(str, Enum):
    RULE = "rule"
    MEMORY = "memory"   # 거래처별 학습(과거 사람 결정)
    LLM = "llm"
    HUMAN = "human"
    DEFAULT = "default"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ser(v: Any) -> Any:
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, date):
        return v.isoformat()
    if dataclasses.is_dataclass(v):
        return {k: _ser(getattr(v, k)) for k in (f.name for f in dataclasses.fields(v))}
    if isinstance(v, (list, tuple)):
        return [_ser(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _ser(x) for k, x in v.items()}
    return v


def _date(v: Any) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def normalize_biz_no(v: Any) -> str:
    """사업자번호 → 숫자 10자리 문자열. 형식이 다르면 숫자만 남긴 값 그대로."""
    if v is None:
        return ""
    return "".join(ch for ch in str(v) if ch.isdigit())


# ---------------------------------------------------------------------------
# 거래처 / 신고 단위
# ---------------------------------------------------------------------------


@dataclass
class Vehicle:
    plate: str                       # 차량번호
    model: str = ""
    deductible: bool = False         # True: 화물·경차·9인승 이상 등 공제 대상 / False: 비영업용 소형승용차


@dataclass
class Client:
    id: str                          # 내부코드(위하고 회사코드 권장)
    name: str
    biz_no: str
    taxpayer_type: TaxpayerType = TaxpayerType.INDIVIDUAL
    industry: str = ""               # 업태/종목 자유기재
    industry_code: str = ""          # 업종코드(있으면)
    prior_year_supply: int | None = None   # 직전연도 공급가액(사업장 기준) - 카드발행공제 판정
    has_tax_free_business: bool = False   # 겸영(면세) 여부 → 공통매입세액 안분 필요
    deemed_input_type: str = ""      # "" | restaurant | manufacturing | other  (의제매입 업종)
    vehicles: list[Vehicle] = field(default_factory=list)
    contact_name: str = ""
    notes: str = ""
    active: bool = True

    def to_dict(self) -> dict:
        return _ser(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Client":
        d = dict(d)
        d["biz_no"] = normalize_biz_no(d.get("biz_no"))
        d["id"] = str(d["id"])
        if "taxpayer_type" in d:
            d["taxpayer_type"] = TaxpayerType(d["taxpayer_type"])
        d["vehicles"] = [v if isinstance(v, Vehicle) else Vehicle(**v) for v in d.get("vehicles") or []]
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def is_corporation(self) -> bool:
        return self.taxpayer_type == TaxpayerType.CORPORATION


@dataclass
class TaxPeriod:
    """과세기간 신고 회차. code 예: '2026-2P'(2기 예정), '2026-2F'(2기 확정)."""

    year: int
    half: int        # 1 | 2
    kind: str        # "P"(예정) | "F"(확정)

    @property
    def code(self) -> str:
        return f"{self.year}-{self.half}{self.kind}"

    @classmethod
    def parse(cls, code: str) -> "TaxPeriod":
        y, rest = code.strip().upper().split("-")
        half, kind = int(rest[0]), rest[1]
        if half not in (1, 2) or kind not in ("P", "F"):
            raise ValueError(f"잘못된 신고회차 코드: {code}")
        return cls(int(y), half, kind)

    @property
    def label(self) -> str:
        return f"{self.year}년 {self.half}기 {'예정' if self.kind == 'P' else '확정'}"

    def __str__(self) -> str:  # pragma: no cover
        return self.code


@dataclass
class Filing:
    """거래처 x 신고회차 = 1건의 신고 작업.

    coverage_start/end: 실제 집계 대상 기간.
      - 법인·예정신고한 개인의 확정: 3개월(4~6월 / 10~12월)
      - 예정고지 받은 개인의 확정: 6개월 전체(1~6월 / 7~12월)
    """

    client_id: str
    period: TaxPeriod
    coverage_start: date
    coverage_end: date
    due_date: date
    filed_preliminary: bool = False      # 예정신고를 했는지(개인 선택 예정신고 포함)
    preliminary_notice_tax: int = 0      # 예정고지세액 (22)
    preliminary_unrefunded: int = 0      # 예정신고 미환급세액 (21)

    def to_dict(self) -> dict:
        d = _ser(self)
        d["period"] = self.period.code
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Filing":
        d = dict(d)
        d["period"] = TaxPeriod.parse(d["period"]) if isinstance(d["period"], str) else d["period"]
        for k in ("coverage_start", "coverage_end", "due_date"):
            d[k] = _date(d[k])
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# 거래
# ---------------------------------------------------------------------------


@dataclass
class Classification:
    category: PurchaseCategory
    non_deductible_reason: NonDeductibleReason | None = None
    exclusion_reason: ExclusionReason | None = None
    decided_by: DecidedBy = DecidedBy.DEFAULT
    rule_id: str = ""
    confidence: float = 1.0
    needs_review: bool = False
    note: str = ""
    # --- 위하고 전표 작성 스타일(학습 섹터가 채움). 비어 있으면 위하고 기본값 유지 ---
    account_code: str = ""        # 계정코드 (예: "830")
    account_name: str = ""        # 계정과목 (예: "소모품비")
    entry_type: str = ""          # 위하고 매입매출 유형 (예: "과세","불공","카과","카면","현과","면세")
    settlement: str = ""          # 결제/분개 상대: "현금" | "외상" | "카드" | "혼합"
    summary_text: str = ""        # 적요
    style_source: str = ""        # 판정 근거 (예: "memory:C001", "industry:restaurant#r12", "fewshot")

    @classmethod
    def from_dict(cls, d: dict) -> "Classification":
        return cls(
            account_code=str(d.get("account_code", "") or ""),
            account_name=d.get("account_name", "") or "",
            entry_type=d.get("entry_type", "") or "",
            settlement=d.get("settlement", "") or "",
            summary_text=d.get("summary_text", "") or "",
            style_source=d.get("style_source", "") or "",
            category=PurchaseCategory(d["category"]),
            non_deductible_reason=NonDeductibleReason(d["non_deductible_reason"]) if d.get("non_deductible_reason") else None,
            exclusion_reason=ExclusionReason(d["exclusion_reason"]) if d.get("exclusion_reason") else None,
            decided_by=DecidedBy(d.get("decided_by", "default")),
            rule_id=d.get("rule_id", ""),
            confidence=float(d.get("confidence", 1.0)),
            needs_review=bool(d.get("needs_review", False)),
            note=d.get("note", ""),
        )


@dataclass
class Transaction:
    client_id: str
    source: Source
    direction: Direction
    doc_type: DocType
    tx_date: date                         # 작성일자/거래일자(공급시기)
    supply_amount: int                    # 공급가액
    vat: int                              # 세액
    total: int = 0                        # 합계(공급대가)
    issue_date: date | None = None        # 발급일자
    transmit_date: date | None = None     # 국세청 전송일자
    approval_no: str = ""                 # 승인번호(전자세금계산서/카드승인번호)
    counterparty_biz_no: str = ""
    counterparty_name: str = ""
    counterparty_tax_type: str = ""       # 국세청 상태조회 결과 문자열(enrich 단계에서 채움)
    counterparty_status: str = ""         # 계속/휴업/폐업
    counterparty_closed_on: date | None = None
    item: str = ""                        # 품목
    merchant_category: str = ""           # 카드 가맹점 업종
    card_kind: CardKind | None = None
    card_no_masked: str = ""
    zero_rated: bool = False              # 영세율
    is_amended: bool = False              # 수정세금계산서
    deductible_flag_from_source: bool | None = None  # 홈택스 카드매입 '공제여부' 등 원천 표시
    memo: str = ""
    source_file: str = ""
    row_no: int = 0
    raw: dict = field(default_factory=dict)
    classification: Classification | None = None
    id: str = ""

    def __post_init__(self) -> None:
        if not self.total:
            self.total = self.supply_amount + self.vat
        self.counterparty_biz_no = normalize_biz_no(self.counterparty_biz_no)
        if not self.id:
            self.id = self.make_id()

    def make_id(self) -> str:
        """같은 거래는 어떤 파일에서 읽어도 같은 id가 나오도록(중복 제거 키)."""
        key = "|".join(
            str(x)
            for x in (
                self.client_id,
                self.source.value,
                self.approval_no or "",
                self.tx_date.isoformat(),
                self.counterparty_biz_no,
                self.supply_amount,
                self.vat,
                "" if self.approval_no else self.row_no,
                "" if self.approval_no else self.source_file,
            )
        )
        return hashlib.sha1(key.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return _ser(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        d = dict(d)
        d["source"] = Source(d["source"])
        d["direction"] = Direction(d["direction"])
        d["doc_type"] = DocType(d["doc_type"])
        for k in ("tx_date", "issue_date", "transmit_date", "counterparty_closed_on"):
            d[k] = _date(d.get(k))
        d["card_kind"] = CardKind(d["card_kind"]) if d.get("card_kind") else None
        d["classification"] = Classification.from_dict(d["classification"]) if d.get("classification") else None
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class ParseIssue:
    source_file: str
    row_no: int
    message: str
    severity: Severity = Severity.WARN

    def to_dict(self) -> dict:
        return _ser(self)


# ---------------------------------------------------------------------------
# 검토 항목 (사람/AI가 처리하는 예외 큐)
# ---------------------------------------------------------------------------


@dataclass
class ReviewItem:
    client_id: str
    period: str                 # TaxPeriod.code
    code: str                   # 검증룰 코드 예: "V001_TOTAL_MISMATCH"
    severity: Severity
    title: str                  # 한 줄 요약(실무자용)
    detail: str = ""
    tx_ids: list[str] = field(default_factory=list)
    tax_impact: int = 0         # 추정 세액 영향(+: 납부세액 증가)
    suggested_action: str = ""
    status: ReviewStatus = ReviewStatus.OPEN
    resolution: str = ""
    resolved_by: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            key = f"{self.client_id}|{self.period}|{self.code}|{','.join(sorted(self.tx_ids))}|{self.title}"
            self.id = hashlib.sha1(key.encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        return _ser(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewItem":
        d = dict(d)
        d["severity"] = Severity(d["severity"])
        d["status"] = ReviewStatus(d.get("status", ReviewStatus.OPEN.value))
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# 신고서
# ---------------------------------------------------------------------------


class Line(str, Enum):
    """일반과세자 부가가치세 신고서 주요 칸. 값 = 서식상 번호(참고용, 서식 개정 시 대조 필요)."""

    # 과세표준 및 매출세액
    S_TAX_INVOICE = "1"            # 과세 세금계산서 발급분
    S_BUYER_ISSUED = "2"           # 과세 매입자발행 세금계산서
    S_CARD_CASH = "3"              # 과세 신용카드·현금영수증 발행분
    S_OTHER = "4"                  # 과세 기타(정규영수증 외 매출분)
    S_ZR_TAX_INVOICE = "5"         # 영세율 세금계산서 발급분
    S_ZR_OTHER = "6"               # 영세율 기타
    S_PRELIM_OMITTED = "7"         # 예정신고 누락분
    S_BAD_DEBT = "8"               # 대손세액 가감
    S_TOTAL = "9"                  # 합계 (㉮)
    # 매입세액
    P_TI_GENERAL = "10"            # 세금계산서 수취분 일반매입
    P_TI_EXPORT_DEFER = "10-1"     # 수출기업 수입분 납부유예
    P_TI_FIXED = "11"              # 세금계산서 수취분 고정자산 매입
    P_PRELIM_OMITTED = "12"        # 예정신고 누락분
    P_BUYER_ISSUED = "13"          # 매입자발행 세금계산서
    P_OTHER_DEDUCTIBLE = "14"      # 그 밖의 공제매입세액(카드·현금영수증 수령분, 의제매입 등)
    P_TOTAL = "15"                 # 합계
    P_NON_DEDUCTIBLE = "16"        # 공제받지 못할 매입세액
    P_NET = "17"                   # 차감계 (㉯)
    PAYABLE = "다"                 # 납부(환급)세액 ㉰ = ㉮ - ㉯
    C_OTHER = "18"                 # 그 밖의 경감·공제세액
    C_CARD_ISSUE = "19"            # 신용카드매출전표등 발행공제 등
    C_TOTAL = "20"                 # 경감·공제 합계 ㉱
    C_SMALL_BIZ = "20-1"           # 소규모 개인사업자 감면세액
    PRELIM_UNREFUNDED = "21"       # 예정신고 미환급세액
    PRELIM_NOTICE = "22"           # 예정고지세액
    PROXY_TRANSFEREE = "23"        # 사업양수자 대리납부
    PROXY_BUYER = "24"             # 매입자 납부특례
    PROXY_CARD = "25"              # 신용카드업자 대리납부
    PENALTY = "26"                 # 가산세액계
    FINAL = "27"                   # 차가감 납부할 세액(환급받을 세액)


@dataclass
class LineValue:
    amount: int = 0     # 금액(공급가액/과세표준)
    tax: int = 0        # 세액
    count: int = 0      # 매수/건수(가능할 때)

    def add(self, amount: int, tax: int, count: int = 1) -> None:
        self.amount += amount
        self.tax += tax
        self.count += count


@dataclass
class VatReturn:
    client_id: str
    period: str
    lines: dict[str, LineValue] = field(default_factory=dict)        # key = Line.name
    non_deductible_breakdown: dict[str, LineValue] = field(default_factory=dict)  # key = NonDeductibleReason.value
    other_deductible_breakdown: dict[str, LineValue] = field(default_factory=dict)  # 14번 내역: "카드_일반","카드_고정","의제매입" ...
    card_receipt_summary: dict[str, LineValue] = field(default_factory=dict)  # 신용카드매출전표등 수령명세서: CardKind.value / "현금영수증"
    notes: list[str] = field(default_factory=list)                   # 계산 근거·가정(사람이 읽는 문장)
    computed_by: str = "taxauto"

    def line(self, ln: Line) -> LineValue:
        return self.lines.setdefault(ln.name, LineValue())

    def to_dict(self) -> dict:
        return _ser(self)

    @classmethod
    def from_dict(cls, d: dict) -> "VatReturn":
        def lv(m: dict) -> dict[str, LineValue]:
            return {k: LineValue(**v) for k, v in (m or {}).items()}

        return cls(
            client_id=d["client_id"],
            period=d["period"],
            lines=lv(d.get("lines")),
            non_deductible_breakdown=lv(d.get("non_deductible_breakdown")),
            other_deductible_breakdown=lv(d.get("other_deductible_breakdown")),
            card_receipt_summary=lv(d.get("card_receipt_summary")),
            notes=list(d.get("notes") or []),
            computed_by=d.get("computed_by", "taxauto"),
        )

"""오케스트레이션 섹터 테스트 공용: 가짜 작업폴더·가짜 단계·합성 결과 파일."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

from taxauto.context import RunContext, StageResult
from taxauto.models import (
    CardKind,
    Classification,
    DecidedBy,
    Direction,
    DocType,
    LineValue,
    NonDeductibleReason,
    PurchaseCategory,
    ReviewItem,
    Severity,
    Source,
    Transaction,
    VatReturn,
)

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 2)


def make_home(tmp_path: Path) -> Path:
    base = tmp_path / "office"
    (base / "clients").mkdir(parents=True)
    shutil.copy(ROOT / "examples" / "clients.example.yaml", base / "clients" / "clients.yaml")
    return base


def write_filing_settings(base: Path, client_id: str, period: str, text: str) -> None:
    p = base / "clients" / client_id / "filings" / f"{period}.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def sample_txns(client_id: str) -> list[Transaction]:
    t1 = Transaction(
        client_id=client_id, source=Source.CARD_PURCHASE, direction=Direction.PURCHASE, doc_type=DocType.CARD,
        tx_date=date(2026, 8, 3), supply_amount=100000, vat=10000, counterparty_name="한우마을",
        counterparty_biz_no="1112233333", approval_no="A1", card_kind=CardKind.BUSINESS,
        card_no_masked="1234-****-****-5678",
        classification=Classification(PurchaseCategory.NON_DEDUCTIBLE, NonDeductibleReason.ENTERTAINMENT,
                                      decided_by=DecidedBy.LLM, confidence=0.6, needs_review=True),
    )
    t2 = Transaction(
        client_id=client_id, source=Source.CARD_PURCHASE, direction=Direction.PURCHASE, doc_type=DocType.CARD,
        tx_date=date(2026, 8, 10), supply_amount=50000, vat=5000, counterparty_name="농협하나로마트",
        counterparty_biz_no="2223344444", approval_no="A2", card_kind=CardKind.BUSINESS,
        classification=Classification(PurchaseCategory.GENERAL, decided_by=DecidedBy.RULE, rule_id="R01"),
    )
    t3 = Transaction(
        client_id=client_id, source=Source.ETAX_SALES, direction=Direction.SALES, doc_type=DocType.TAX_INVOICE,
        tx_date=date(2026, 8, 15), supply_amount=3000000, vat=300000, counterparty_name="거래처A",
        counterparty_biz_no="3334455555", approval_no="S1",
    )
    return [t1, t2, t3]


def sample_return(client_id: str, period: str, final_tax: int) -> VatReturn:
    r = VatReturn(client_id=client_id, period=period)
    r.lines["S_TAX_INVOICE"] = LineValue(3000000, 300000, 1)
    r.lines["S_TOTAL"] = LineValue(3000000, 300000, 0)
    r.lines["P_OTHER_DEDUCTIBLE"] = LineValue(150000, 15000, 2)
    r.lines["P_NON_DEDUCTIBLE"] = LineValue(100000, 10000, 1)
    r.lines["P_NET"] = LineValue(50000, 5000, 0)
    r.lines["PAYABLE"] = LineValue(0, 295000, 0)
    r.lines["FINAL"] = LineValue(0, final_tax, 0)
    r.non_deductible_breakdown["접대비및이와유사한비용"] = LineValue(100000, 10000, 1)
    r.notes.append("카드매입 1건 접대비 추정(AI) — 검토 필요")
    return r


def sample_review(client_id: str, period: str, txns: list[Transaction], blockers: int = 1, warns: int = 1) -> list[ReviewItem]:
    out = []
    for i in range(blockers):
        out.append(ReviewItem(client_id, period, f"V00{i}_BLK", Severity.BLOCKER, f"차단 항목 {i}",
                              detail="세금계산서 합계 불일치 — 주민번호 900101-1234567 포함 메모", tx_ids=[txns[0].id],
                              tax_impact=10000, suggested_action="위하고 전표 확인"))
    for i in range(warns):
        out.append(ReviewItem(client_id, period, f"V10{i}_WARN", Severity.WARN, f"경고 항목 {i}",
                              tx_ids=[txns[1].id], tax_impact=-5000))
    out.append(ReviewItem(client_id, period, "V200_INFO", Severity.INFO, "참고 항목"))
    return out


def fake_stages(final_tax: dict[str, int] | None = None, fail: dict[str, str] | None = None,
                blockers: dict[str, int] | None = None, calls: list | None = None) -> dict:
    """가짜 단계 세트. fail={client_id: stage} 이면 그 단계에서 예외."""
    final_tax = final_tax or {}
    fail = fail or {}
    blockers = blockers or {}

    def mk(name):
        def run(ctx: RunContext) -> StageResult:
            if calls is not None:
                calls.append((ctx.client.id, name))
            if fail.get(ctx.client.id) == name:
                raise RuntimeError(f"{name} 고장: 카드 1234-5678-9012-3456")
            ws = ctx.workspace
            if name == "normalize":
                ws.save_transactions(sample_txns(ctx.client.id))
            elif name == "compute":
                ws.save_return(sample_return(ctx.client.id, ctx.period_code, final_tax.get(ctx.client.id, 295000)))
            elif name == "validate":
                ws.save_review(sample_review(ctx.client.id, ctx.period_code, ws.load_transactions(),
                                             blockers=blockers.get(ctx.client.id, 1)))
            return StageResult(ok=True, message=f"{name} done", counts={"n": 1})

        return run

    return {n: mk(n) for n in ["collect", "normalize", "enrich", "classify", "compute", "validate", "report"]}

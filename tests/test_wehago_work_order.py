"""위하고 작업지시서 + 업로드 xlsx (가짜 workspace JSON)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import yaml
from openpyxl import load_workbook

from taxauto.models import (
    CardKind,
    Classification,
    DecidedBy,
    Direction,
    DocType,
    ExclusionReason,
    Filing,
    Line,
    LineValue,
    NonDeductibleReason,
    PurchaseCategory,
    ReviewItem,
    Severity,
    Source,
    TaxPeriod,
    Transaction,
    VatReturn,
)
from taxauto.wehago.upload_file import derive_entry_type, load_template, write_upload_xlsx
from taxauto.wehago.work_order import build_work_order, is_stale, load_work_order, save_work_order

CID, PERIOD = "C001", "2026-2P"


def tx(src, direction, doc, d, supply, vat, name, biz, cls=None, **kw) -> Transaction:
    return Transaction(client_id=CID, source=src, direction=direction, doc_type=doc, tx_date=d,
                       supply_amount=supply, vat=vat, counterparty_name=name, counterparty_biz_no=biz,
                       classification=cls, **kw)


def make_ws(tmp_path: Path, with_blocker: bool = True) -> Path:
    (tmp_path / "clients").mkdir()
    (tmp_path / "clients" / "clients.yaml").write_text(yaml.safe_dump({"clients": [
        {"id": CID, "name": "(주)테스트상사", "biz_no": "123-45-67890", "taxpayer_type": "법인"}]}, allow_unicode=True),
        encoding="utf-8")
    root = tmp_path / "data" / PERIOD / CID
    root.mkdir(parents=True)
    filing = Filing(CID, TaxPeriod.parse(PERIOD), date(2026, 7, 1), date(2026, 9, 30), date(2026, 10, 26),
                    preliminary_notice_tax=0, preliminary_unrefunded=0)
    (root / "filing.json").write_text(json.dumps(filing.to_dict(), ensure_ascii=False), encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"status": "ok", "client_id": CID, "period": PERIOD}), encoding="utf-8")

    ent = tx(Source.ETAX_PURCHASE, Direction.PURCHASE, DocType.TAX_INVOICE, date(2026, 7, 3), 300000, 30000, "가나식당", "2223344556",
             Classification(PurchaseCategory.NON_DEDUCTIBLE, non_deductible_reason=NonDeductibleReason.ENTERTAINMENT,
                            decided_by=DecidedBy.RULE, rule_id="P010", account_code="813", account_name="접대비",
                            entry_type="불공", settlement="외상", summary_text="거래처 식대", style_source="memory:C001"),
             approval_no="20260703-41000000-11111111")
    fixed = tx(Source.ETAX_PURCHASE, Direction.PURCHASE, DocType.TAX_INVOICE, date(2026, 8, 1), 5000000, 500000, "다라전자", "3334455667",
               Classification(PurchaseCategory.FIXED_ASSET, decided_by=DecidedBy.RULE), approval_no="20260801-1")
    card_personal = tx(Source.CARD_PURCHASE, Direction.PURCHASE, DocType.CARD, date(2026, 8, 5), 10000, 1000, "마바마트", "4445566778",
                       Classification(PurchaseCategory.NOT_APPLICABLE, exclusion_reason=ExclusionReason.PERSONAL),
                       approval_no="12345678", card_kind=CardKind.BUSINESS, card_no_masked="1234-****-****-5678")
    card_ok = tx(Source.CARD_PURCHASE, Direction.PURCHASE, DocType.CARD, date(2026, 8, 6), 20000, 2000, "사아문구", "5556677889",
                 Classification(PurchaseCategory.GENERAL), approval_no="87654321")
    styled = tx(Source.ETAX_PURCHASE, Direction.PURCHASE, DocType.TAX_INVOICE, date(2026, 9, 1), 100000, 10000, "자차상사", "6667788990",
                Classification(PurchaseCategory.GENERAL, account_code="830", account_name="소모품비", summary_text="사무용품",
                               style_source="fewshot"), approval_no="20260901-1")
    doubtful = tx(Source.ETAX_PURCHASE, Direction.PURCHASE, DocType.TAX_INVOICE, date(2026, 9, 2), 200000, 20000, "카타상사", "7778899001",
                  Classification(PurchaseCategory.NON_DEDUCTIBLE, non_deductible_reason=NonDeductibleReason.UNRELATED,
                                 decided_by=DecidedBy.LLM, confidence=0.7, needs_review=True), approval_no="20260902-1")
    paper = tx(Source.PAPER_TAX_INVOICE, Direction.PURCHASE, DocType.TAX_INVOICE, date(2026, 9, 3), 50000, 5000, "파하상회", "8889900112",
               Classification(PurchaseCategory.NON_DEDUCTIBLE, non_deductible_reason=NonDeductibleReason.ENTERTAINMENT,
                              account_code="813", settlement="외상", summary_text="선물"), row_no=1, source_file="paper.xlsx")
    manual_sale = tx(Source.MANUAL, Direction.SALES, DocType.NONE, date(2026, 9, 4), 100000, 10000, "", "", None,
                     row_no=2, source_file="manual.xlsx")
    led_ent = tx(Source.WEHAGO_LEDGER, Direction.PURCHASE, DocType.TAX_INVOICE, date(2026, 7, 3), 300000, 30000, "가나식당", "2223344556",
                 approval_no="20260703-41000000-11111111", raw={"유형": "54.불공"}, row_no=3, source_file="ledger.xlsx")
    dup1 = tx(Source.WEHAGO_LEDGER, Direction.PURCHASE, DocType.TAX_INVOICE, date(2026, 9, 9), 70000, 7000, "중복상사", "9990011223",
              raw={"유형": "51.과세"}, row_no=10, source_file="ledger.xlsx")
    dup2 = tx(Source.WEHAGO_LEDGER, Direction.PURCHASE, DocType.TAX_INVOICE, date(2026, 9, 9), 70000, 7000, "중복상사", "9990011223",
              raw={"유형": "51.과세"}, row_no=11, source_file="ledger.xlsx")
    txns = [ent, fixed, card_personal, card_ok, styled, doubtful, paper, manual_sale, led_ent, dup1, dup2]
    (root / "transactions.json").write_text(json.dumps([t.to_dict() for t in txns], ensure_ascii=False), encoding="utf-8")

    items = [ReviewItem(CID, PERIOD, "V011_NEEDS_REVIEW", Severity.WARN, "AI 판정 확인 필요", tx_ids=[doubtful.id])]
    if with_blocker:
        items.append(ReviewItem(CID, PERIOD, "V016_NO_DATA", Severity.BLOCKER, "카드매출 자료 없음"))
    (root / "review.json").write_text(json.dumps([i.to_dict() for i in items], ensure_ascii=False), encoding="utf-8")

    ret = VatReturn(CID, PERIOD)
    ret.lines[Line.P_TI_GENERAL.name] = LineValue(600000, 60000, 4)
    ret.lines[Line.P_TI_FIXED.name] = LineValue(5000000, 500000, 1)
    ret.lines[Line.P_NON_DEDUCTIBLE.name] = LineValue(550000, 55000, 3)
    ret.lines[Line.FINAL.name] = LineValue(0, -495000, 0)
    ret.non_deductible_breakdown[NonDeductibleReason.ENTERTAINMENT.value] = LineValue(350000, 35000, 2)
    ret.card_receipt_summary[CardKind.BUSINESS.value] = LineValue(20000, 2000, 1)
    (root / "return.json").write_text(json.dumps(ret.to_dict(), ensure_ascii=False), encoding="utf-8")
    return root


def by_cp(order: dict, name: str) -> dict:
    return next(c for c in order["corrections"] if c["key"]["counterparty_name"] == name)


def test_work_order_contents(tmp_path):
    root = make_ws(tmp_path)
    order = build_work_order(root)
    json.dumps(order, ensure_ascii=False)  # 직렬화 가능

    assert order["company"]["name"] == "(주)테스트상사" and order["company"]["biz_no_dash"] == "123-45-67890"
    assert order["period"] == PERIOD and order["filing"]["coverage_start"] == "2026-07-01"
    assert order["prepaid"] == {"preliminary_notice_tax": 0, "preliminary_unrefunded": 0}

    # 차단 → 신고서 작성 단계 전에서 멈춤, 제출 없음
    assert order["blocked"] is True
    assert [b["code"] for b in order["block_reasons"]] == ["V016_NO_DATA"]
    assert order["plan"]["stop_before"] == "60_vat_return" and "60_vat_return" not in order["plan"]["procedures"]
    assert order["plan"]["submit"] is False

    ent = by_cp(order, "가나식당")
    assert ent["action"] == "set_non_deductible"
    assert ent["set"]["non_deductible_reason"] == "접대비및이와유사한비용" and ent["set"]["non_deductible_form_row"] == 4
    assert ent["set"]["account_code"] == "813" and ent["set"]["entry_type"] == "불공"
    assert ent["set"]["settlement"] == "외상" and ent["set"]["summary_text"] == "거래처 식대"
    assert ent["basis"]["style_source"] == "memory:C001" and ent["keep_default"] == []
    assert ent["key"]["approval_no"] == "20260703-41000000-11111111" and ent["key"]["supply_amount"] == 300000
    assert ent["already_applied"] is True and ent["ledger_match"]["row_no"] == 3

    fx = by_cp(order, "다라전자")
    assert fx["action"] == "mark_fixed_asset" and fx["set"]["fixed_asset"] is True
    assert set(fx["keep_default"]) == {"account_code", "account_name", "entry_type", "settlement", "summary_text"}

    cp = by_cp(order, "마바마트")
    assert cp["action"] == "exclude_card_deduction" and cp["set"]["exclude_reason"] == "개인사용"
    assert cp["key"]["card_no_masked"] == "1234-****-****-5678"

    assert all(c["key"]["counterparty_name"] != "사아문구" for c in order["corrections"])  # 바꿀 것 없음

    st = by_cp(order, "자차상사")
    assert st["action"] == "set_style" and st["set"] == {"account_code": "830", "account_name": "소모품비", "summary_text": "사무용품"}
    assert "entry_type" in st["keep_default"]

    dbt = by_cp(order, "카타상사")
    assert dbt["requires_human"] is True and "V011_NEEDS_REVIEW" in dbt["human_reasons"][0]

    dels = [c for c in order["corrections"] if c["action"] == "delete_candidate"]
    assert len(dels) == 1 and dels[0]["key"]["row_no"] == 11 and dels[0]["requires_human"] is True

    exp = order["expected_return"]
    assert exp["final_tax"] == -495000
    lines = {x["line"]: x for x in exp["lines"]}
    assert lines["P_TI_FIXED"]["tax"] == 500000 and lines["P_TI_FIXED"]["no"] == "11"
    assert exp["attachments"] == {"신용카드매출전표등수령명세서": True, "공제받지못할매입세액명세서": True, "건물등감가상각자산취득명세서": True}
    assert order["warnings"][0]["code"] == "V011_NEEDS_REVIEW"


def test_not_blocked_plan_goes_to_efile_prepare(tmp_path):
    root = make_ws(tmp_path, with_blocker=False)
    order = build_work_order(root)
    assert order["blocked"] is False and order["plan"]["stop_before"] is None
    assert order["plan"]["procedures"][-1] == "80_efile_prepare" and order["plan"]["efile"] == "prepare_only"


def test_missing_return_blocks(tmp_path):
    root = make_ws(tmp_path, with_blocker=False)
    (root / "return.json").unlink()
    order = build_work_order(root)
    assert order["blocked"] and any(b["code"] == "ENGINE_NO_RETURN" for b in order["block_reasons"])
    assert order["expected_return"]["available"] is False


def test_save_load_and_stale(tmp_path):
    root = make_ws(tmp_path)
    p = save_work_order(root)
    assert p == root / "wehago" / "work_order.json"
    assert load_work_order(root)["client_id"] == CID
    assert not is_stale(root)
    (root / "review.json").write_text("[]", encoding="utf-8")
    assert is_stale(root)


def test_upload_xlsx_uses_style_fields(tmp_path):
    root = make_ws(tmp_path)
    meta = write_upload_xlsx(root)
    assert meta["confirmed"] is False and meta["warning"]
    assert meta["rows"] == 2  # 종이세금계산서 + 수동입력만(기본 include_sources)
    wb = load_workbook(meta["path"])
    ws = wb.active
    tpl = load_template()
    headers = [c.value for c in ws[1]]
    assert headers == [c["header"] for c in tpl["columns"]]
    rows = [dict(zip(headers, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
    paper = next(r for r in rows if r["거래처명"] == "파하상회")
    assert paper["일자"] == "20260903" and paper["유형"] == "54"            # 불공 → 54 (추정 코드)
    assert paper["계정코드"] == "813" and paper["적요"] == "선물" and paper["분개유형"] == "2"
    assert paper["사업자등록번호"] == "888-99-00112" and paper["공급가액"] == 50000
    assert paper["불공제사유"] == "접대비및이와유사한비용"                  # 코드 미확정 → 이름
    sale = next(r for r in rows if r["거래처명"] in (None, ""))
    assert sale["유형"] == "14"                                            # 증빙없는 과세매출 → 건별
    assert sale["계정코드"] is None                                        # 스타일 없음 → 빈칸(위하고 기본값)
    assert json.loads((root / "wehago" / "upload" / "upload_meta.json").read_text(encoding="utf-8"))["rows"] == 2


def test_derive_entry_type_defaults():
    c = Classification(PurchaseCategory.NOT_APPLICABLE, exclusion_reason=ExclusionReason.PERSONAL)
    t = tx(Source.CARD_PURCHASE, Direction.PURCHASE, DocType.CARD, date(2026, 8, 5), 10000, 1000, "x", "1", c)
    assert derive_entry_type(t) == "카면"
    t.classification = Classification(PurchaseCategory.GENERAL)
    assert derive_entry_type(t) == "카과"
    t.classification = Classification(PurchaseCategory.GENERAL, entry_type="현과")
    assert derive_entry_type(t) == "현과"                                   # 스타일 값 우선

"""enrich.py: 국세청 상태조회(가짜 전송)·캐시·건너뛰기."""

from __future__ import annotations

import json
from datetime import date

import pytest

from fixtures.make_fixtures import build_inbox, make_ctx
from taxauto.ingest import collect, enrich, normalize
from taxauto.models import Direction, DocType, Source, Transaction


def _tx(b: str, src=Source.ETAX_PURCHASE, direction=Direction.PURCHASE) -> Transaction:
    return Transaction(client_id="C002", source=src, direction=direction, doc_type=DocType.TAX_INVOICE,
                       tx_date=date(2026, 8, 1), supply_amount=1000, vat=100, counterparty_biz_no=b, approval_no=b)


class FakeNts:
    def __init__(self, fail: bool = False):
        self.calls: list[list[str]] = []
        self.urls: list[str] = []
        self.fail = fail

    def __call__(self, url, body, headers):
        if self.fail:
            raise OSError("timeout")
        b_nos = json.loads(body)["b_no"]
        assert len(b_nos) <= 100
        self.calls.append(b_nos)
        self.urls.append(url)
        data = []
        for b in b_nos:
            if b.endswith("3"):
                data.append({"b_no": b, "b_stt": "폐업자", "b_stt_cd": "03", "tax_type": "부가가치세 일반과세자", "end_dt": "20260715"})
            elif b.endswith("2"):
                data.append({"b_no": b, "b_stt": "계속사업자", "b_stt_cd": "01", "tax_type": "부가가치세 간이과세자", "end_dt": ""})
            elif b.endswith("9"):
                data.append({"b_no": b, "b_stt": "", "b_stt_cd": "", "tax_type": "국세청에 등록되지 않은 사업자등록번호입니다.", "end_dt": ""})
            else:
                data.append({"b_no": b, "b_stt": "계속사업자", "b_stt_cd": "01", "tax_type": "부가가치세 일반과세자", "end_dt": ""})
        return json.dumps({"status_code": "OK", "match_cnt": len(data), "request_cnt": len(b_nos), "data": data}).encode()


def test_enrich_fills_fields_and_batches():
    txns = [_tx(f"{1000000000 + i * 10}") for i in range(250)]  # 끝자리 0 = 계속사업자
    txns.append(_tx("1111111113"))
    txns.append(_tx("2222222222", src=Source.CARD_PURCHASE))
    txns.append(_tx("3333333339"))
    txns.append(_tx("4444444442", src=Source.ETAX_SALES, direction=Direction.SALES))  # 매출 → 대상 아님
    fake = FakeNts()
    cache: dict = {}
    counts = enrich.enrich_transactions(txns, "my+key/==", cache, date(2026, 10, 2), transport=fake)
    assert [len(c) for c in fake.calls] == [100, 100, 53]
    assert "serviceKey=my%2Bkey%2F%3D%3D" in fake.urls[0]
    assert counts["closed"] == 1 and counts["targets"] == 253
    closed = txns[250]
    assert closed.counterparty_status == "폐업" and closed.counterparty_closed_on == date(2026, 7, 15)
    assert txns[251].counterparty_tax_type == "부가가치세 간이과세자" and txns[251].counterparty_status == "계속"
    assert txns[252].counterparty_status == "미등록"
    assert txns[253].counterparty_status == ""

    # 캐시 유효 → 재조회 없음
    fake2 = FakeNts()
    enrich.enrich_transactions(txns, "k", cache, date(2026, 10, 20), cache_days=30, transport=fake2)
    assert fake2.calls == []
    # 캐시 만료 → 재조회
    enrich.enrich_transactions(txns, "k", cache, date(2026, 12, 1), cache_days=30, transport=fake2)
    assert sum(len(c) for c in fake2.calls) == 253


def test_fetch_error_hides_key():
    with pytest.raises(enrich.NtsError) as e:
        enrich.fetch_status(["1234567890"], "SECRETKEY", transport=FakeNts(fail=True))
    assert "SECRETKEY" not in str(e.value)


def test_run_skips_when_disabled_or_no_key(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path, "C002", policy={"nts_status": {"enabled": False}})
    r = enrich.run(ctx)
    assert r.ok and r.skipped
    monkeypatch.delenv("NTS_SERVICE_KEY", raising=False)
    ctx = make_ctx(tmp_path, "C002", policy={"nts_status": {"enabled": True}})
    r = enrich.run(ctx)
    assert r.ok and r.skipped and "NTS_SERVICE_KEY" in r.message


def test_run_with_fake_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("NTS_SERVICE_KEY", "testkey")
    build_inbox(tmp_path / "inbox", "2026-2P", clients=["C001"])
    ctx = make_ctx(tmp_path, "C001", policy={"nts_status": {"enabled": True, "cache_days": 30}})
    collect.run(ctx)
    normalize.run(ctx)
    fake = FakeNts()
    r = enrich.run(ctx, transport=fake)
    assert r.ok and not r.skipped and r.counts["filled"] > 0
    txns = ctx.workspace.load_transactions()
    purch = [t for t in txns if t.direction == Direction.PURCHASE and len(t.counterparty_biz_no) == 10]
    assert purch and all(t.counterparty_status for t in purch)
    assert all(not t.counterparty_status for t in txns if t.direction == Direction.SALES)
    cache = tmp_path / "data" / "_cache" / "nts_status.json"
    assert cache.exists()

    # 캐시가 유효하면 전송하지 않음(실패하는 전송이어도 무관)
    r2 = enrich.run(ctx, transport=FakeNts(fail=True))
    assert r2.ok and r2.counts["fetched"] == 0 and "errors" not in r2.counts

    # 캐시 없음 + 전송 실패 → 보조 단계라 ok, errors 표시
    cache.unlink()
    r3 = enrich.run(ctx, transport=FakeNts(fail=True))
    assert r3.ok and r3.counts["errors"] == 1 and "실패" in r3.message

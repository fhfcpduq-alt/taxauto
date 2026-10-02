import json
import sys
import types

import pytest
from orch_helpers import TODAY, fake_stages, make_home

from taxauto import pipeline
from taxauto.agent import mcp_server as srv
from taxauto.pipeline import Paths, run_period

P = "2026-2F"


@pytest.fixture
def home(tmp_path, monkeypatch):
    base = make_home(tmp_path)
    monkeypatch.setenv("TAXAUTO_HOME", str(base))
    fns = fake_stages(final_tax={"C001": 295000, "C002": -40000})
    real = pipeline.resolve_stage
    monkeypatch.setattr(pipeline, "resolve_stage", lambda name, overrides=None: (overrides or fns).get(name) or real(name, overrides))
    run_period(P, base_dir=base, today=TODAY, stage_fns=fns)
    return base


@pytest.fixture
def fake_memory(monkeypatch):
    calls = []
    mod = types.ModuleType("taxauto.classify.memory")
    mod.add_decision = lambda client_dir, txn, cls, scope="counterparty", by="human", today=None: calls.append(
        {"client": client_dir.name, "tx": txn.id, "category": cls.category.value, "scope": scope, "by": by}) or {"scope": scope}
    monkeypatch.setitem(sys.modules, "taxauto.classify.memory", mod)
    return calls


def test_no_submit_or_delete_tools():
    names = set(srv.mcp._tool_manager._tools)
    assert {"list_filings", "get_filing", "list_review_items", "resolve_review_item", "get_transactions",
            "reclassify_transaction", "run_pipeline", "draft_client_message", "ingest_file", "get_work_order",
            "record_agent_step"} <= names
    for n in names:
        assert not any(bad in n for bad in ("submit", "delete", "remove", "file_return"))


def test_list_and_get_filing(home):
    s = srv.list_filings(P)
    assert s["schema"] == "taxauto.summary/v1" and s["totals"]["clients"] == 2
    f = srv.get_filing("C001", P)
    assert f["final_tax"] == 295000 and f["summary"]["blocker_open"] == 1
    assert any(l["line"] == "FINAL" and l["no"] == "27" and l["tax"] == 295000 for l in f["lines"])
    assert f["open_review_items"][0]["severity"] == "차단"
    assert "900101-1234567" not in json.dumps(f, ensure_ascii=False)
    assert f["wehago_diff"] is None


def test_wehago_diff(home):
    ws = Paths.from_base(home).workspace(P, "C001")
    w = ws.load_json("return.json")
    w["lines"]["FINAL"]["tax"] = 290000
    ws.save_json("wehago_return.json", w)
    d = srv.get_filing("C001", P)["wehago_diff"]
    assert d == [{"line": "FINAL", "amount_diff": 0, "tax_diff": 5000, "engine_tax": 295000, "wehago_tax": 290000}]


def test_list_review_items(home):
    items = srv.list_review_items(P)
    assert [i["severity"] for i in items[:2]] == ["차단", "차단"]
    assert {i["client_id"] for i in items} == {"C001", "C002"}
    only = srv.list_review_items(P, client_id="C002", open_only=False)
    assert all(i["client_id"] == "C002" for i in only)


def test_resolve_review_item_and_remember_guard(home, fake_memory):
    item = srv.list_review_items(P, client_id="C001")[0]
    with pytest.raises(ValueError):
        srv.resolve_review_item("C001", P, item["id"], "해결", "근거", remember=True)  # agent 는 remember 불가
    with pytest.raises(ValueError):
        srv.resolve_review_item("C001", P, item["id"], "해결", "")  # note 필수
    r = srv.resolve_review_item("C001", P, item["id"], "확인후유지", "대표 확인", remember=True, resolved_by="김세무")
    assert r["item"]["status"] == "확인후유지" and r["remembered"] == 1
    assert fake_memory[0]["by"] == "김세무"
    assert srv.list_filings(P)["clients"][0]["blocker_open"] == 0


def test_get_transactions_filters(home):
    r = srv.get_transactions("C001", P, "needs_review")
    assert r["total"] == 1 and r["transactions"][0]["classification"]["decided_by"] == "llm"
    assert srv.get_transactions("C001", P, "non_deductible")["total"] == 1
    assert srv.get_transactions("C001", P, "excluded")["total"] == 0
    a = srv.get_transactions("C001", P, "all", limit=2)
    assert a["total"] == 3 and a["returned"] == 2
    assert "raw" not in a["transactions"][0]
    with pytest.raises(ValueError):
        srv.get_transactions("C001", P, "bogus")


def test_reclassify_agent_then_human(home, fake_memory):
    tx = srv.get_transactions("C001", P, "needs_review")["transactions"][0]
    r = srv.reclassify_transaction("C001", P, tx["id"], "일반매입", reason="식자재 구입", confidence=0.7)
    assert r["after"]["decided_by"] == "llm" and r["after"]["needs_review"] is True
    assert r["rerun"]["status"] == "ok" and r["rerun"]["summary"]["client_id"] == "C001"
    assert fake_memory == []  # 에이전트 판정은 메모리 저장 안 함
    with pytest.raises(ValueError):
        srv.reclassify_transaction("C001", P, tx["id"], "불공제", reason="엉뚱한사유")
    with pytest.raises(ValueError):
        srv.reclassify_transaction("C001", P, tx["id"], "일반매입", remember=True)
    r = srv.reclassify_transaction("C001", P, tx["id"], "불공제", reason="접대비및이와유사한비용",
                                   decided_by="김세무", remember=True)
    assert r["after"]["decided_by"] == "human" and r["after"]["needs_review"] is False
    assert fake_memory[-1]["scope"] == "counterparty" and fake_memory[-1]["by"] == "김세무"
    log = (home / "data" / P / "C001" / "decisions.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(log) == 2
    # 매출 거래는 재분류 불가
    sales = [t for t in srv.get_transactions("C001", P, "all")["transactions"] if t["direction"] == "매출"][0]
    with pytest.raises(ValueError):
        srv.reclassify_transaction("C001", P, sales["id"], "일반매입")


def test_run_pipeline_tool(home):
    r = srv.run_pipeline(P, client_ids=["C002"], from_stage="compute")
    assert r["totals"]["clients"] == 2 and r["last_run"]["ran"] == ["C002"]
    with pytest.raises(ValueError):
        srv.run_pipeline(P, from_stage="nope")
    with pipeline.RunLock(home / "data", "night"):
        with pytest.raises(pipeline.LockBusy):
            srv.run_pipeline(P)


def test_draft_client_message(home):
    m = srv.draft_client_message("C001", P)
    assert m["sendable"] is False and m["text"].startswith("※ 초안 아님 — 발송 금지")
    m2 = srv.draft_client_message("C002", P)
    assert "환급 예정 세액: 40,000원" in m2["text"]


def test_ingest_file(home, tmp_path):
    src = tmp_path / "전표목록.xlsx"
    src.write_bytes(b"fake-xlsx")
    r = srv.ingest_file("C001", P, str(src))
    assert r["copied"] is True and r["rerun"]["status"] == "ok"
    assert (home / "inbox" / P / "C001" / "전표목록.xlsx").read_bytes() == b"fake-xlsx"
    assert srv.ingest_file("C001", P, str(src))["copied"] is False  # 같은 파일
    src.write_bytes(b"changed")
    r3 = srv.ingest_file("C001", P, str(src))
    assert r3["inbox_path"].endswith("전표목록_1.xlsx")
    bad = tmp_path / "x.exe"
    bad.write_bytes(b"x")
    with pytest.raises(ValueError):
        srv.ingest_file("C001", P, str(bad))


def test_work_order_and_agent_steps(home, monkeypatch):
    r = srv.get_work_order("C001", P)
    assert r["status"] in ("not_implemented", "ok")
    mod = types.ModuleType("taxauto.wehago.work_order")
    mod.build_work_order = lambda ctx: {"client": ctx.client.id, "actions": []}
    monkeypatch.setitem(sys.modules, "taxauto.wehago", types.ModuleType("taxauto.wehago"))
    monkeypatch.setitem(sys.modules, "taxauto.wehago.work_order", mod)
    assert srv.get_work_order("C001", P) == {"status": "ok", "work_order": {"client": "C001", "actions": []}}
    mod2 = types.ModuleType("taxauto.wehago.work_order")
    mod2.build_work_order = lambda workspace_root, *, client=None, clients_dir=None: {"root": str(workspace_root), "c": client.id}
    mod2.save_work_order = lambda root, order: root / "wehago" / "work_order.json"
    monkeypatch.setitem(sys.modules, "taxauto.wehago.work_order", mod2)
    r2 = srv.get_work_order("C001", P)
    assert r2["work_order"]["c"] == "C001" and r2["work_order"]["root"].endswith("2026-2F/C001")
    assert r2["path"].endswith("work_order.json")

    srv.record_agent_step("C001", P, "login", "ok")
    rec = srv.record_agent_step("C001", P, "apply_work_order", "needs_human", note="카드 1234-5678-9012-3456 확인 필요",
                                evidence_path="shots/1.png")
    assert "9012-3456" not in rec["note"]
    lines = (home / "data" / P / "C001" / "wehago" / "agent_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    row = next(r for r in srv.list_filings(P)["clients"] if r["client_id"] == "C001")
    assert row["wehago_status"]["step"] == "apply_work_order" and row["wehago_status"]["status"] == "needs_human"
    with pytest.raises(ValueError):
        srv.record_agent_step("C999", P, "login", "ok")

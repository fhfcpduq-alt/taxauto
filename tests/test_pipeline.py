import json
import os
from datetime import datetime, timedelta

import pytest
from orch_helpers import TODAY, fake_stages, make_home, write_filing_settings

from taxauto import pipeline
from taxauto.context import StageResult
from taxauto.pipeline import (
    STAGE_MODULES,
    STAGE_ORDER,
    LockBusy,
    Paths,
    RunLock,
    StageNotImplemented,
    make_context,
    resolve_stage,
    run_client,
    run_period,
)
from taxauto.registry import load_clients


def test_stage_modules_cover_order():
    assert list(STAGE_MODULES) == STAGE_ORDER
    assert STAGE_MODULES["report"] == "taxauto.report.stage"


def test_resolve_missing_module_is_not_implemented(monkeypatch):
    monkeypatch.setitem(STAGE_MODULES, "collect", "taxauto.ingest.no_such_module_xyz")
    with pytest.raises(StageNotImplemented):
        resolve_stage("collect")
    # 실제 report 단계는 존재
    assert callable(resolve_stage("report"))


def test_run_period_success_writes_state_and_summary(tmp_path):
    base = make_home(tmp_path)
    s = run_period("2026-2F", base_dir=base, today=TODAY, stage_fns=fake_stages(final_tax={"C001": 295000, "C002": -40000}))
    assert s["schema"] == "taxauto.summary/v1"
    rows = {r["client_id"]: r for r in s["clients"]}
    assert rows["C001"]["status"] == "ok"
    assert rows["C001"]["final_tax"] == 295000 and rows["C001"]["payable"] == 295000
    assert rows["C002"]["final_tax"] == -40000 and rows["C002"]["refund"] == 40000
    assert rows["C001"]["blocker_open"] == 1 and rows["C001"]["warn_open"] == 1 and rows["C001"]["info_open"] == 1
    assert rows["C001"]["ready_to_file"] is False
    assert rows["C001"]["due_date"] == "2027-01-25" and rows["C001"]["d_day"] == 115
    assert rows["C001"]["biz_no_masked"] == "123-45-67***"
    assert s["totals"]["payable_total"] == 295000 and s["totals"]["refund_total"] == 40000
    # 파일
    pdir = base / "data" / "2026-2F"
    on_disk = json.loads((pdir / "_summary.json").read_text(encoding="utf-8"))
    assert on_disk["totals"] == s["totals"]
    assert (pdir / "_dashboard.html").exists() and (pdir / "_briefing.md").exists()
    st = json.loads((pdir / "C001" / "state.json").read_text(encoding="utf-8"))
    assert st["status"] == "ok"
    for name in STAGE_ORDER:
        rec = st["stages"][name]
        assert rec["status"] == "ok" and rec["started_at"] and rec["finished_at"]
        assert rec["duration_sec"] >= 0
    assert (pdir / "C001" / "filing.json").exists()
    # lock 해제됨
    assert not (base / "data" / ".lock").exists()


def test_failure_stops_client_but_others_continue(tmp_path):
    base = make_home(tmp_path)
    calls = []
    s = run_period("2026-2F", base_dir=base, today=TODAY, stage_fns=fake_stages(fail={"C001": "classify"}, calls=calls))
    rows = {r["client_id"]: r for r in s["clients"]}
    assert rows["C001"]["status"] == "failed" and rows["C001"]["failed_stage"] == "classify"
    assert "RuntimeError" in rows["C001"]["error"]
    assert "9012-3456" not in rows["C001"]["error"]  # 카드번호 마스킹
    assert rows["C002"]["status"] == "ok"
    assert ("C001", "compute") not in calls and ("C002", "report") in calls
    st = json.loads((base / "data/2026-2F/C001/state.json").read_text(encoding="utf-8"))
    assert st["stages"]["classify"]["traceback"] and "Traceback" in st["stages"]["classify"]["traceback"]
    assert st["stages"]["compute"]["status"] == "pending"


def test_not_implemented_stage_recorded(tmp_path, monkeypatch):
    base = make_home(tmp_path)
    monkeypatch.setitem(STAGE_MODULES, "enrich", "taxauto.ingest.not_here_yet")
    fns = fake_stages()
    del fns["enrich"]  # 가짜 주입 없이 모듈 해석 → 없음
    s = run_period("2026-2F", base_dir=base, today=TODAY, stage_fns=fns)
    for r in s["clients"]:
        assert r["status"] == "not_implemented" and r["failed_stage"] == "enrich"
    assert s["totals"]["not_implemented"] == 2


def test_stage_returning_not_ok_is_failure(tmp_path):
    base = make_home(tmp_path)
    fns = fake_stages()
    fns["collect"] = lambda ctx: StageResult(ok=False, message="inbox 비어 있음")
    s = run_period("2026-2F", client_ids=["C002"], base_dir=base, today=TODAY, stage_fns=fns)
    row = s["clients"][[r["client_id"] for r in s["clients"]].index("C002")]
    assert row["status"] == "failed" and row["error"] == "inbox 비어 있음"


def test_preliminary_period_excludes_individual(tmp_path):
    base = make_home(tmp_path)
    s = run_period("2026-2P", base_dir=base, today=TODAY, stage_fns=fake_stages())
    assert [r["client_id"] for r in s["clients"]] == ["C002"]
    assert s["not_required"] == ["C001"]


def test_skip_setting_and_filing_settings_applied(tmp_path):
    base = make_home(tmp_path)
    write_filing_settings(base, "C002", "2026-2F", "skip: true\n")
    write_filing_settings(base, "C001", "2026-2F", "filed_preliminary: false\npreliminary_notice_tax: 1250000\n")
    seen = {}

    def compute(ctx):
        seen["filing"] = ctx.filing
        return StageResult(ok=True)

    fns = fake_stages()
    fns["compute"] = compute
    s = run_period("2026-2F", base_dir=base, today=TODAY, stage_fns=fns)
    rows = {r["client_id"]: r for r in s["clients"]}
    assert rows["C002"]["status"] == "skipped"
    f = seen["filing"]
    assert f.preliminary_notice_tax == 1250000 and f.filed_preliminary is False
    assert f.coverage_start.isoformat() == "2026-07-01"  # 예정고지 → 6개월


def test_only_failed_and_from_stage(tmp_path):
    base = make_home(tmp_path)
    run_period("2026-2F", base_dir=base, today=TODAY, stage_fns=fake_stages(fail={"C001": "compute"}))
    calls = []
    s = run_period("2026-2F", base_dir=base, today=TODAY, only_failed=True, from_stage="compute",
                   stage_fns=fake_stages(calls=calls))
    assert {c for c, _ in calls} == {"C001"}
    assert [n for _, n in calls] == ["compute", "validate", "report"]
    st = json.loads((base / "data/2026-2F/C001/state.json").read_text(encoding="utf-8"))
    assert st["status"] == "ok" and st["stages"]["collect"]["status"] == "ok"  # 앞 단계 기록 유지
    assert s["last_run"]["ran"] == ["C001"]


def test_unknown_stage_rejected(tmp_path):
    base = make_home(tmp_path)
    with pytest.raises(ValueError):
        run_period("2026-2F", base_dir=base, stages=["bogus"], stage_fns=fake_stages())


def test_lock_busy_and_stale(tmp_path):
    base = make_home(tmp_path)
    data = base / "data"
    with RunLock(data, "first"):
        with pytest.raises(LockBusy):
            run_period("2026-2F", base_dir=base, today=TODAY, stage_fns=fake_stages())
    assert not (data / ".lock").exists()
    # 오래된 lock 은 자동 해제
    old = (datetime.now().astimezone() - timedelta(hours=10)).isoformat()
    (data / ".lock").write_text(json.dumps({"pid": 999999, "host": "other-pc", "started_at": old}), encoding="utf-8")
    s = run_period("2026-2F", base_dir=base, today=TODAY, stage_fns=fake_stages())
    assert s["totals"]["ok"] == 2
    # 같은 PC, 죽은 pid
    if os.name == "posix":
        import socket

        (data / ".lock").write_text(json.dumps({"pid": 2 ** 22 + 12345, "host": socket.gethostname(),
                                                "started_at": datetime.now().astimezone().isoformat()}), encoding="utf-8")
        run_period("2026-2F", base_dir=base, today=TODAY, stage_fns=fake_stages())


def test_make_context_paths(tmp_path):
    base = make_home(tmp_path)
    paths = Paths.from_base(base)
    c = load_clients(paths.clients_dir)[0]
    ctx = make_context(c, "2026-2F", paths=paths, today=TODAY)
    assert ctx.workspace.root == base / "data" / "2026-2F" / "C001"
    assert ctx.inbox_dir == base / "inbox" / "2026-2F" / "C001"
    assert ctx.clients_dir == base / "clients"
    assert ctx.filing.due_date.isoformat() == "2027-01-25"


def test_run_client_direct_and_wehago_status(tmp_path):
    base = make_home(tmp_path)
    paths = Paths.from_base(base)
    c = load_clients(paths.clients_dir)[1]
    ctx = make_context(c, "2026-2F", paths=paths, today=TODAY)
    st = run_client(ctx, stages=["normalize", "compute", "validate"], stage_fns=fake_stages())
    assert st["status"] == "ok" and set(st["stages"]) == {"normalize", "compute", "validate"}
    log = ctx.workspace.root / "wehago" / "agent_log.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"ts":"t1","step":"login","status":"ok"}\n{"ts":"t2","step":"write_return","status":"needs_human","note":"2차인증"}\n',
                   encoding="utf-8")
    s = pipeline.write_summary("2026-2F", paths=paths, today=TODAY)
    row = next(r for r in s["clients"] if r["client_id"] == "C002")
    assert row["wehago_status"] == {"step": "write_return", "status": "needs_human", "at": "t2", "note": "2차인증"}

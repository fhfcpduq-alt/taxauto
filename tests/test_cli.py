import json
import sys
import types

import pytest
from orch_helpers import TODAY, fake_stages, make_home

from taxauto import cli, pipeline
from taxauto.pipeline import Paths, run_period

P = "2026-2F"


@pytest.fixture
def home(tmp_path, monkeypatch):
    base = make_home(tmp_path)
    monkeypatch.setenv("TAXAUTO_HOME", str(base))
    return base


@pytest.fixture
def fake_resolve(monkeypatch):
    """CLI 경로에서도 가짜 단계가 쓰이게 resolve_stage 교체."""
    fns = fake_stages()
    real = pipeline.resolve_stage
    monkeypatch.setattr(pipeline, "resolve_stage", lambda name, overrides=None: fns.get(name) or real(name, overrides))
    return fns


def test_init_creates_dirs(tmp_path, monkeypatch, capsys):
    base = tmp_path / "new"
    assert cli.main(["--home", str(base), "init"]) == 0
    for d in ("clients", "inbox", "data", "logs"):
        assert (base / d).is_dir()
    assert (base / "clients" / "clients.yaml").exists()
    assert (base / "clients" / "C001" / "filings" / "2026-2F.yaml").exists()
    # 기존 명부는 덮어쓰지 않음
    (base / "clients" / "clients.yaml").write_text("clients: []\n", encoding="utf-8")
    cli.main(["--home", str(base), "init"])
    assert (base / "clients" / "clients.yaml").read_text(encoding="utf-8") == "clients: []\n"


def test_run_and_status(home, fake_resolve, capsys):
    rc = cli.main(["run", "--period", P])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OO식당" in out and "295,000" in out
    rc = cli.main(["status", "--period", P])
    out = capsys.readouterr().out
    assert rc == 0 and "(주)민테크" in out and "완료" in out


def test_run_exit_code_on_failure(home, monkeypatch, capsys):
    fns = fake_stages(fail={"C002": "normalize"})
    monkeypatch.setattr(pipeline, "resolve_stage", lambda name, overrides=None: fns[name])
    assert cli.main(["run", "--period", P, "--client", "C002"]) == 1
    assert "실패(normalize)" in capsys.readouterr().out


def test_night_writes_log_and_briefing(home, fake_resolve, capsys):
    rc = cli.main(["night", "--date", "2026-10-02"])
    assert rc == 0
    log = home / "logs" / "2026-10-02.log"
    assert log.exists() and "야간 실행 끝" in log.read_text(encoding="utf-8")
    # 10월 → 2기 예정, 개인 C001 은 대상 아님
    s = json.loads((home / "data" / "2026-2P" / "_summary.json").read_text(encoding="utf-8"))
    assert [r["client_id"] for r in s["clients"]] == ["C002"]
    assert (home / "data" / "2026-2P" / "_briefing.md").exists()


def test_night_exit_1_when_stage_missing(home, monkeypatch):
    monkeypatch.setitem(pipeline.STAGE_MODULES, "collect", "taxauto.ingest.missing_for_test")
    assert cli.main(["night", "--date", "2026-10-02"]) == 1


def test_night_lock_busy(home, fake_resolve):
    with pipeline.RunLock(home / "data", "other"):
        assert cli.main(["night", "--date", "2026-10-02"]) == 1


def test_review_list_and_resolve(home, capsys):
    run_period(P, base_dir=home, today=TODAY, stage_fns=fake_stages())
    capsys.readouterr()
    assert cli.main(["review", "list", "--period", P, "--client", "C001", "--open"]) == 0
    out = capsys.readouterr().out
    assert "차단 항목 0" in out and "경고 항목 0" in out
    ws = Paths.from_base(home).workspace(P, "C001")
    blk = next(i for i in ws.load_review() if i.severity.value == "차단")
    rc = cli.main(["review", "resolve", blk.id[:8], "--period", P, "--client", "C001",
                   "--status", "확인후유지", "--note", "대표 확인: 거래처 접대", "--by", "김세무"])
    assert rc == 0
    it = next(i for i in ws.load_review() if i.id == blk.id)
    assert it.status.value == "확인후유지" and it.resolved_by == "김세무" and it.resolution == "대표 확인: 거래처 접대"
    s = json.loads((home / "data" / P / "_summary.json").read_text(encoding="utf-8"))
    assert next(r for r in s["clients"] if r["client_id"] == "C001")["blocker_open"] == 0
    # 재실행해도 사람 결정 유지(workspace merge)
    run_period(P, base_dir=home, today=TODAY, stage_fns=fake_stages())
    assert next(i for i in ws.load_review() if i.id == blk.id).status.value == "확인후유지"


def test_review_resolve_remember_calls_memory(home, monkeypatch, capsys):
    run_period(P, base_dir=home, today=TODAY, stage_fns=fake_stages())
    calls = []
    fake = types.ModuleType("taxauto.classify.memory")
    fake.add_decision = lambda client_dir, txn, cls, scope="counterparty", by="human", today=None: calls.append(
        (client_dir.name, txn.id, cls.category.value, scope, by)) or {"key": "biz:x"}
    monkeypatch.setitem(sys.modules, "taxauto.classify.memory", fake)
    ws = Paths.from_base(home).workspace(P, "C001")
    blk = next(i for i in ws.load_review() if i.severity.value == "차단")
    rc = cli.main(["review", "resolve", blk.id, "--period", P, "--status", "해결", "--note", "접대비 맞음",
                   "--remember", "--by", "김세무"])
    assert rc == 0
    assert calls == [("C001", blk.tx_ids[0], "불공제", "counterparty", "김세무")]
    assert "메모리에 1건 저장" in capsys.readouterr().out


def test_review_resolve_errors(home, capsys):
    run_period(P, base_dir=home, today=TODAY, stage_fns=fake_stages())
    assert cli.main(["review", "resolve", "zzzzzz", "--period", P, "--status", "해결", "--note", "x"]) == 1
    assert "찾을 수 없음" in capsys.readouterr().err


def test_doctor_hides_key_values(home, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-VALUE-123")
    (home / "inbox").mkdir()
    (home / "data").mkdir()
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "SECRET" not in out and "sk-ant" not in out
    assert "ANTHROPIC_API_KEY: 설정됨" in out
    assert "거래처 명부 2곳" in out
    assert "미검증" in out
    assert "공휴일" in out
    assert rc == 0


def test_doctor_missing_roster(tmp_path, capsys):
    rc = cli.main(["--home", str(tmp_path / "empty"), "doctor"])
    assert rc == 1 and "거래처 명부 없음" in capsys.readouterr().out


def test_table_width_korean():
    t = cli.table(["코드", "이름"], [["C1", "가나다"], ["C22", "ab"]])
    lines = t.splitlines()
    assert lines[2].startswith("C1 ") and "가나다" in lines[2]

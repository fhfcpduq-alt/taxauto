"""위하고 안전장치: 훅 스크립트(subprocess) + 판정 함수 + 설정 파일 일관성."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from taxauto.wehago.guard import Guard, write_init_script

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "hooks" / "wehago_guard.py"


def run_hook(payload, tmp_path, raw: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "TAXAUTO_HOME": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    env.pop("CLAUDE_PROJECT_DIR", None)
    data = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
    return subprocess.run([sys.executable, str(HOOK)], input=data.encode("utf-8"), capture_output=True, env=env, timeout=60)


def pw(name: str, **inp) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": f"mcp__playwright__{name}", "tool_input": inp}


BLOCK = [
    pw("browser_click", element="전자신고 제출 버튼", target="e12"),
    pw("browser_click", element="신고서 제출", target="e3"),
    pw("browser_click", element="일괄 삭제", target="e3"),
    pw("browser_click", element="회사삭제", target="e3"),
    pw("browser_click", element="마감 취소", target="e3"),
    pw("browser_click", element="비밀번호 변경", target="e3"),
    pw("browser_click", element="공동인증서 삭제", target="e3"),
    pw("browser_click", element="조회 버튼", target="button:has-text('제출')"),   # selector 에 금지어
    pw("browser_click", target="e15"),                                          # 설명 없는 클릭
    pw("browser_evaluate", function="() => document.querySelector('#x').click()"),
    pw("browser_run_code", code="await page.click('text=제출')"),
    pw("browser_run_code_unsafe", code="await page.click('text=제출')"),
    pw("browser_mouse_click_xy", x=10, y=20),
    pw("browser_navigate", url="https://www.hometax.go.kr/websquare/websquare.html"),
    pw("browser_navigate", url="https://hometax.go.kr/"),
    pw("browser_navigate", url="https://evil.example.com/"),
    pw("browser_navigate", url="http://www.wehago.com/"),        # https 아님
    pw("browser_tabs", action="new", url="https://www.hometax.go.kr/"),
    pw("browser_type", element="비밀번호 입력란", target="e5", text="x"),
    pw("browser_fill_form", fields=[{"name": "비밀번호", "type": "textbox", "target": "e2", "value": "x"}]),
    pw("browser_select_option", element="처리 구분", target="e9", values=["전자신고 제출"]),
    pw("browser_press_key", key="Delete"),
    pw("browser_file_upload", paths=["C:/Users/x/Desktop/secret.xlsx"]),
    pw("browser_file_upload", paths=["data/2026-2P/C001/wehago/upload/a.exe"]),
    pw("browser_some_new_tool", x=1),                             # 모르는 도구 = 차단(fail-closed)
    {"tool_name": "mcp__other-browser__javascript_tool", "tool_input": {"code": "1"}},
    {"tool_name": "Bash", "tool_input": {"command": "python -c \"from playwright.sync_api import sync_playwright; connect_over_cdp('http://127.0.0.1:9222')\""}},
    {"tool_name": "Bash", "tool_input": {"command": "python -m taxauto.wehago.replay --recipe x && curl https://www.hometax.go.kr"}},
]

ALLOW = [
    pw("browser_click", element="조회 버튼", target="e12"),
    pw("browser_click", element="매입매출전표입력 메뉴", target="e7"),
    pw("browser_type", element="거래처명 검색칸", target="e4", text="(주)테스트상사"),
    pw("browser_select_option", element="회사 선택", target="e9", values=["(주)테스트상사"]),
    pw("browser_navigate", url="https://www.wehago.com/#/main"),
    pw("browser_navigate", url="https://smarta.wehago.com/some/path"),
    pw("browser_snapshot"),
    pw("browser_take_screenshot", filename="a.png"),
    pw("browser_press_key", key="Enter"),
    pw("browser_tabs", action="list"),
    pw("browser_file_upload", paths=["data/2026-2P/C001/wehago/upload/매입매출전표_upload.xlsx"]),
    {"tool_name": "Read", "tool_input": {"file_path": "/x"}},
    {"tool_name": "mcp__taxauto__get_work_order", "tool_input": {"client_id": "C001", "period": "2026-2P"}},
    {"tool_name": "Bash", "tool_input": {"command": "python -m taxauto.wehago.replay --recipe recipes/x.yaml --client C001 --period 2026-2P --cdp http://127.0.0.1:9222"}},
    {"tool_name": "Bash", "tool_input": {"command": "python -m pytest -q tests/test_wehago_guard.py"}},
]


@pytest.mark.parametrize("payload", BLOCK, ids=lambda p: f"{p['tool_name'].split('__')[-1]}:{json.dumps(p['tool_input'], ensure_ascii=False)[:40]}")
def test_hook_blocks(payload, tmp_path):
    r = run_hook(payload, tmp_path)
    assert r.returncode == 2, r.stderr.decode("utf-8", "replace")
    assert "차단" in r.stderr.decode("utf-8")


@pytest.mark.parametrize("payload", ALLOW, ids=lambda p: f"{p['tool_name'].split('__')[-1]}:{json.dumps(p['tool_input'], ensure_ascii=False)[:40]}")
def test_hook_allows(payload, tmp_path):
    r = run_hook(payload, tmp_path)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")


def test_hook_malformed_input_fails_closed(tmp_path):
    r = run_hook(None, tmp_path, raw="{not json")
    assert r.returncode == 2


def test_hook_writes_audit_log(tmp_path):
    run_hook(pw("browser_click", element="전자신고 제출", target="e1"), tmp_path)
    log = tmp_path / "data" / "_wehago_guard" / "guard_log.jsonl"
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["allowed"] is False and rec["rule"] == "forbidden_text"


def test_exceptions_cover_only_whole_phrase():
    g = Guard({"forbidden_text": ["전송", "제출"], "allow_exceptions": ["전송내역조회"]})
    assert g.check_text("전송내역 조회")            # 예외 문구 안의 '전송' → 허용
    assert not g.check_text("국세청 전송")          # 예외 밖 → 차단
    assert not g.check_text("전송내역조회 후 제출")  # 다른 금지어
    assert not g.check_text("전송내역조회 그리고 전송")  # 예외 밖에 또 나오면 차단


def test_normalization():
    g = Guard.load()
    assert not g.check_text("전 자 신 고  제\u200b출")
    assert not g.check_text("SUBMIT")


def test_url_rules():
    g = Guard.load()
    assert g.check_url("https://www.wehago.com/")
    assert g.check_url("about:blank")
    assert not g.check_url("https://hometax.go.kr.evil.com/")       # 허용 목록에 없음
    assert not g.check_url("https://www.wehago.com.evil.com/")
    assert not g.check_url("file:///C:/x.html")
    assert not g.check_url("javascript:alert(1)")
    assert not g.check_url("")


def test_settings_json_registers_hook_and_denies_js():
    s = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    pre = s["hooks"]["PreToolUse"]
    cmds = [h["command"] for m in pre for h in m["hooks"]]
    assert any("scripts/hooks/wehago_guard.py" in c for c in cmds)
    assert any("mcp__" in m["matcher"] and "Bash" in m["matcher"] for m in pre)
    deny = set(s["permissions"]["deny"])
    for t in ("browser_evaluate", "browser_run_code", "browser_run_code_unsafe"):
        assert f"mcp__playwright__{t}" in deny
    assert not deny & set(s["permissions"]["allow"])
    night = json.loads((ROOT / "config" / "wehago" / "claude_night_settings.json").read_text(encoding="utf-8"))
    assert "Edit(config/wehago/guard.yaml)" in night["permissions"]["deny"]


def test_mcp_json_has_playwright_with_guard():
    m = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["playwright"]
    args = m["args"]
    assert "--cdp-endpoint" in args and "http://127.0.0.1:9222" in args
    assert "scripts/wehago_guard_init.js" in args


def test_init_script_in_sync_with_config(tmp_path):
    p = write_init_script(tmp_path / "g.js", Guard.load())
    committed = (ROOT / "scripts" / "wehago_guard_init.js").read_text(encoding="utf-8")
    assert p.read_text(encoding="utf-8") == committed, "guard.yaml 변경 후 'python -m taxauto.wehago.guard --write-init-script' 실행 필요"


def test_config_always_block_is_not_in_allow():
    g = Guard.load()
    s = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    allowed = {a.split("__")[-1] for a in s["permissions"]["allow"] if a.startswith("mcp__playwright__")}
    assert not allowed & g.always_block
    known = g.readonly | set(g.text_checked) | set(g.url_checked) | set(g.path_checked)
    assert allowed <= known, allowed - known

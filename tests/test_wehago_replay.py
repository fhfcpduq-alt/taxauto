"""레시피 재생기: 모의 위하고 페이지(file://)로 실제 재생·체크포인트·실패 보고·금지 버튼 차단 검증.

playwright 파이썬 패키지와 크로미움이 없으면 브라우저 테스트는 건너뛴다(레시피 형식 테스트는 항상 실행).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from taxauto.wehago.guard import Guard
from taxauto.wehago.replay import (
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_GUARD,
    EXIT_OK,
    RecipeError,
    build_context,
    normalize_recipe,
    precheck,
    resume_index,
    run_recipe,
    subst,
)

ROOT = Path(__file__).resolve().parents[1]
MOCK = (ROOT / "tests" / "fixtures" / "mock_wehago.html").resolve()
MOCK_URL = MOCK.as_uri()


def _browser_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            b = p.chromium.launch()
            b.close()
        return True
    except Exception:
        return False


BROWSER = _browser_ok()
needs_browser = pytest.mark.skipif(not BROWSER, reason="playwright/chromium 없음")


def test_guard(base: Path) -> Guard:
    # 테스트에서만 file:// 허용(실제 운영 설정은 https + 위하고 도메인만)
    return Guard.load(base_dir=base, overrides={"allowed_schemes": ["https", "about", "file"]})


test_guard.__test__ = False  # pytest 수집 제외(헬퍼)

LOGIN_TO_MENU = [
    {"id": "uid", "action": "fill", "label": "아이디", "value": "agent01"},
    {"id": "login", "action": "click", "role": "button", "name": "로그인", "checkpoint": True},
    {"id": "co", "action": "select", "label": "회사 선택", "value": "${client.name}"},
    {"id": "switch", "action": "click", "role": "button", "name": "회사 전환"},
    {"id": "check_co", "action": "expect_text", "text": "현재 회사: ${client.name}", "checkpoint": True},
]


def flow_recipe() -> dict:
    return {
        "id": "mock_export_ledger",
        "requires": ["client.name", "period.code"],
        "steps": LOGIN_TO_MENU + [
            {"id": "menu", "action": "click", "role": "button", "name": "매입매출전표"},
            {"id": "from", "action": "fill", "label": "조회 시작일", "value": "${period.code}"},
            {"id": "search", "action": "click", "role": "button", "name": "조회", "exact": True},
            {"id": "searched", "action": "expect_text", "text": "조회 완료 (${period.code})", "checkpoint": True},
            {"id": "download", "action": "download", "role": "link", "name": "엑셀 다운로드",
             "save_as": "ledger_${client.id}_${period.code}.csv"},
            {"id": "save", "action": "click", "role": "button", "name": "신고서 저장", "dialog": "accept"},
            {"id": "saved", "action": "expect_text", "text": "저장 완료"},
        ],
    }


def run(recipe, base, **kw):
    kw.setdefault("variables", {"client.name": "(주)테스트상사"})
    return run_recipe(recipe, client_id="C001", period="2026-2P", base_dir=base, launch=True,
                      start_url=MOCK_URL, guard=test_guard(base), **kw)


# ---------------------------------------------------------------------------
# 형식·변수·사전검사 (브라우저 불필요)
# ---------------------------------------------------------------------------


def test_normalize_assigns_ids_and_validates():
    r = normalize_recipe({"steps": [{"action": "click", "text": "조회"}, {"action": "screenshot"}]})
    assert [s["id"] for s in r["steps"]] == ["s01", "s02"]
    with pytest.raises(RecipeError):
        normalize_recipe({"steps": [{"action": "hack"}]})
    with pytest.raises(RecipeError):
        normalize_recipe({"steps": [{"action": "click"}]})          # 로케이터 없음
    with pytest.raises(RecipeError):
        normalize_recipe({"steps": []})                              # 미학습 레시피
    with pytest.raises(RecipeError):
        normalize_recipe({"steps": [{"id": "a", "action": "screenshot"}, {"id": "a", "action": "screenshot"}]})


def test_subst_and_missing_var(tmp_path):
    ctx = build_context("C001", "2026-2P", tmp_path, tmp_path / "ws", {"menu": "매입매출"}, {"client.name": "가나"})
    assert subst("${client.name}/${period.code}/${period.label}/${menu}", ctx) == "가나/2026-2P/2026년 2기 예정/매입매출"
    with pytest.raises(RecipeError):
        subst("${client.password}", ctx)


def test_precheck_blocks_forbidden_steps(tmp_path):
    g = Guard.load(base_dir=tmp_path)
    ctx = build_context("C001", "2026-2P", tmp_path, tmp_path / "ws", None, {"client.name": "가나"})
    r = normalize_recipe({"steps": [
        {"id": "ok", "action": "click", "role": "button", "name": "조회"},
        {"id": "bad", "action": "click", "role": "button", "name": "전자신고 제출"},
        {"id": "ht", "action": "goto", "url": "https://www.hometax.go.kr/"},
        {"id": "pw", "action": "fill", "label": "비밀번호", "value": "x"},
        {"id": "up", "action": "upload", "selector": "input[type=file]", "value": "/etc/passwd"},
        {"id": "key", "action": "press", "value": "Delete"},
    ]})
    bad = {p["step"] for p in precheck(r, ctx, g) if p["kind"] == "guard"}
    assert bad == {"bad", "ht", "pw", "up", "key"}


def test_precheck_blocks_without_launching_browser(tmp_path):
    recipe = {"id": "evil", "steps": [{"action": "click", "role": "button", "name": "신고서 제출"}]}
    code, res = run_recipe(recipe, client_id="C001", period="2026-2P", base_dir=tmp_path, launch=True)
    assert code == EXIT_GUARD and res["error_kind"] == "precheck"
    assert not (tmp_path / "data").exists() or not list((tmp_path / "data").rglob("*.png"))


def test_missing_required_var_is_config_error(tmp_path):
    code, res = run_recipe({"requires": ["client.name"], "steps": [{"action": "screenshot"}]},
                           client_id="C001", period="2026-2P", base_dir=tmp_path, launch=True)
    assert code == EXIT_CONFIG


def test_start_url_guarded(tmp_path):
    code, res = run_recipe({"steps": [{"action": "screenshot"}]}, client_id="C001", period="2026-2P",
                           base_dir=tmp_path, launch=True, start_url="https://www.hometax.go.kr/")
    assert code == EXIT_GUARD


def test_cli_check_only_reports_guard(tmp_path):
    rp = tmp_path / "evil.yaml"
    rp.write_text("id: evil\nsteps:\n  - action: click\n    role: button\n    name: 전자신고 제출\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "taxauto.wehago.replay", "--recipe", str(rp), "--client", "C001",
                        "--period", "2026-2P", "--base-dir", str(tmp_path), "--check-only"],
                       capture_output=True, cwd=ROOT, env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")}, timeout=60)
    assert r.returncode == EXIT_GUARD
    out = json.loads(r.stdout.decode("utf-8"))
    assert out["problems"][0]["kind"] == "guard"


def test_example_recipe_is_valid_format():
    from taxauto.wehago.replay import load_recipe

    for p in (ROOT / "recipes" / "examples").glob("*.yaml"):
        r = load_recipe(p)
        assert r["steps"]


# ---------------------------------------------------------------------------
# 실제 재생 (모의 위하고 페이지)
# ---------------------------------------------------------------------------


@needs_browser
def test_full_flow_with_download_checkpoints_and_logs(tmp_path):
    code, res = run(flow_recipe(), tmp_path)
    assert code == EXIT_OK, res
    assert res["completed_steps"][-1] == "saved"
    dl = Path(res["downloads"]["download"])
    assert dl.name == "ledger_C001_2026-2P.csv" and "가나상사" in dl.read_text(encoding="utf-8")
    run_dir = Path(res["run_dir"])
    assert run_dir.is_relative_to(tmp_path / "data" / "2026-2P" / "C001" / "wehago")
    assert len(list(run_dir.glob("*.png"))) >= 10
    log = [json.loads(x) for x in (run_dir / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(1 for x in log if x.get("status") == "ok") == len(flow_recipe()["steps"])
    st = json.loads((tmp_path / "data/2026-2P/C001/wehago/replay_state.json").read_text(encoding="utf-8"))
    assert st["mock_export_ledger"]["status"] == "ok"
    assert st["mock_export_ledger"]["last_checkpoint"]["id"] == "searched"
    agent_log = (tmp_path / "data/2026-2P/C001/wehago/agent_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(agent_log[-1])["status"] == "ok"


@needs_browser
def test_failure_report_and_resume_from_checkpoint(tmp_path):
    r = flow_recipe()
    r["steps"][5] = {"id": "menu", "action": "click", "role": "button", "name": "없는메뉴", "timeout_ms": 800}
    code, res = run(r, tmp_path)
    assert code == EXIT_FAILED
    assert res["failed_step"]["id"] == "menu" and res["error_kind"] == "step"
    assert Path(res["screenshot"]).exists()
    assert "현재 회사: (주)테스트상사" in res["page_text_excerpt"]
    assert res["last_checkpoint"]["id"] == "check_co"
    assert res["completed_steps"] == ["uid", "login", "co", "switch", "check_co"]
    wdir = tmp_path / "data/2026-2P/C001/wehago"
    fixed = normalize_recipe(flow_recipe())
    assert resume_index(fixed, wdir, resume=True) == 5          # 마지막 체크포인트 다음 단계
    assert resume_index(fixed, wdir, from_step="search") == 7


@needs_browser
def test_runtime_guard_blocks_disguised_submit_button(tmp_path):
    """설명/selector 에는 금지어가 없지만 실제 요소(title='신고서 제출')가 제출 버튼 → 실행 시 차단."""
    r = {"id": "sneaky", "steps": LOGIN_TO_MENU + [
        {"id": "sneak", "action": "click", "selector": "#send"},
        {"id": "after", "action": "expect_text", "text": "제출됨", "timeout_ms": 500},
    ]}
    code, res = run(r, tmp_path)
    assert code == EXIT_GUARD, res
    assert res["failed_step"]["id"] == "sneak" and res["error_kind"] == "guard"
    assert "제출됨" not in res["page_text_excerpt"]


@needs_browser
def test_init_script_blocks_direct_dom_click(tmp_path):
    """재생기 검사를 거치지 않는 직접 클릭도 페이지 주입 가드가 막는다(DOM 화면 한정)."""
    from playwright.sync_api import sync_playwright

    g = test_guard(tmp_path)
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context()
        ctx.add_init_script(g.render_init_script())
        page = ctx.new_page()
        page.goto(MOCK_URL)
        page.fill("#uid", "a")
        page.click("#btn-login")
        page.click("text=회사 전환")
        page.click("#btn-submit")          # '전자신고 제출'
        page.click("#send")                # title='신고서 제출'
        page.click("#ht")                  # 홈택스 링크
        status = page.inner_text("#status")
        blocked = page.evaluate("() => window.__taxautoGuardBlocked || []")
        url = page.url
        b.close()
    assert status != "제출됨"
    assert url.startswith("file:")
    whys = [x["why"] for x in blocked]
    assert any("제출" in w for w in whys) and "차단도메인" in whys


@needs_browser
def test_unexpected_dialog_is_dismissed(tmp_path):
    r = {"id": "dlg", "steps": LOGIN_TO_MENU + [
        {"id": "save", "action": "click", "role": "button", "name": "신고서 저장"},   # dialog 미지정 → 취소
        {"id": "cancelled", "action": "expect_text", "text": "저장 취소"},
    ]}
    code, res = run(r, tmp_path)
    assert code == EXIT_OK, res


@needs_browser
def test_optional_step_and_deadline(tmp_path):
    from taxauto.wehago.replay import ReplayOptions

    r = {"id": "opt", "steps": LOGIN_TO_MENU[:2] + [
        {"id": "maybe", "action": "click", "role": "button", "name": "공지 닫기", "optional": True, "timeout_ms": 300},
        {"id": "done", "action": "expect_text", "text": "로그인됨"},
    ]}
    code, res = run(r, tmp_path, options=ReplayOptions(step_retries=0, step_screenshots=False))
    assert code == EXIT_OK and "maybe" not in res["completed_steps"]
    code, res = run(r, tmp_path, options=ReplayOptions(deadline_sec=0.0, step_screenshots=False))
    assert code == EXIT_FAILED and res["error_kind"] == "timeout"

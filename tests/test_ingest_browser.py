"""browser.py: 레시피 검증·치환·가짜 page 로 실행 (playwright 불필요)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fixtures.make_fixtures import make_ctx
from taxauto.ingest import browser

ROOT = Path(__file__).resolve().parents[1]


class FakeDownload:
    suggested_filename = "export.xlsx"

    def save_as(self, path):
        Path(path).write_bytes(b"PK fake")


class FakeDownloadCtx:
    def __enter__(self):
        self.value = FakeDownload()
        return self

    def __exit__(self, *a):
        return False


class FakePage:
    def __init__(self):
        self.log = []

    def goto(self, url):
        self.log.append(("goto", url))

    def click(self, sel):
        self.log.append(("click", sel))

    def fill(self, sel, val):
        self.log.append(("fill", sel, val))

    def select_option(self, sel, val):
        self.log.append(("select", sel, val))

    def wait_for_selector(self, sel, timeout=None):
        self.log.append(("wait", sel))

    def wait_for_timeout(self, ms):
        self.log.append(("sleep", ms))

    def wait_for_url(self, url, timeout=None):
        self.log.append(("url", url))

    def press(self, sel, key):
        self.log.append(("press", sel, key))

    def expect_download(self):
        return FakeDownloadCtx()


def test_example_recipe_is_valid():
    r = browser.load_recipe(ROOT / "config" / "recipes" / "wehago_ledger_export.example.yaml")
    assert r.steps and r.name == "wehago_ledger_export"


def test_plaintext_password_rejected():
    with pytest.raises(browser.RecipeError):
        browser.load_recipe({"name": "x", "steps": [{"fill": {"selector": "#password", "value": "hunter2"}}]})
    with pytest.raises(browser.RecipeError):
        browser.load_recipe({"name": "x", "steps": [{"hover": "#a"}]})
    browser.load_recipe({"name": "x", "steps": [{"fill": {"selector": "#password", "value": "${env:PW}"}}]})


def test_run_with_fake_page(tmp_path):
    ctx = make_ctx(tmp_path, "C002", "2026-2P")
    recipe = {
        "name": "t",
        "start_url": "https://example.invalid/{client_id}",
        "steps": [
            {"fill": {"selector": "#id", "value": "${env:WEHAGO_ID}"}},
            {"fill": {"selector": "#pw", "secret": "WEHAGO_PW"}},
            {"click": "text=로그인"},
            {"wait": {"ms": 10}},
            {"fill": {"selector": "#from", "value": "{start_ymd}"}},
            {"download": {"click": "text=엑셀", "save_as": "{client_id}_{period}.xlsx"}},
        ],
    }
    page = FakePage()
    env = {"WEHAGO_ID": "staff01", "WEHAGO_PW": "s3cret"}
    files = browser.run_recipe(recipe, tmp_path / "dl", browser.recipe_vars(ctx.client, ctx.filing), page=page, env=env)
    assert files == [tmp_path / "dl" / "C002_2026-2P.xlsx"] and files[0].exists()
    assert ("goto", "https://example.invalid/C002") in page.log
    assert ("fill", "#pw", "s3cret") in page.log
    assert ("fill", "#from", "20260701") in page.log


def test_missing_secret_errors(tmp_path):
    recipe = {"name": "t", "steps": [{"fill": {"selector": "#pw", "secret": "NOPE_PW"}}]}
    with pytest.raises(browser.RecipeError):
        browser.run_recipe(recipe, tmp_path, {}, page=FakePage(), env={})


def test_no_playwright_raises_clean_error(tmp_path, monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError("x")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(browser.BrowserUnavailable):
        browser.run_recipe({"name": "t", "steps": []}, tmp_path, {})

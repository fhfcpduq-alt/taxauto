"""(실험적) 브라우저 '레시피 실행기' — 사람이 녹화한 화면 조작 단계를 재생해 파일을 inbox 로 내려받는다.

위하고·홈택스는 공개 API 가 없으므로, 반복 다운로드만 자동화하는 범용 러너.
레시피: config/recipes/*.yaml

  name: wehago_ledger_export
  start_url: https://www.wehago.com/
  steps:
    - goto: "https://..."
    - fill:   {selector: "#userId", value: "${env:WEHAGO_ID}"}
    - fill:   {selector: "#userPw", secret: WEHAGO_PW}          # 비밀번호는 secret(환경변수/키체인 이름)만
    - click:  "text=로그인"
    - wait:   {selector: "text=회계", timeout: 30000}           # 또는 {ms: 2000} / {url: "**/main**"}
    - select: {selector: "#period", value: "{period}"}
    - download: {click: "text=엑셀", save_as: "{client_id}_위하고전표_{period}.xlsx"}

치환변수: {client_id} {client_name} {biz_no} {period} {start} {end} {start_ymd} {end_ymd} {year}
          ${env:이름}  ${keyring:서비스/계정}

비밀번호·인증서 암호는 레시피 파일에 절대 저장하지 않는다(검증에서 차단).
  - secret: NAME  → 환경변수 NAME, 없으면 OS 키체인(keyring 패키지: 서비스 'taxauto', 계정 NAME)

녹화 방법 (playwright codegen):
  pip install playwright && playwright install chromium
  playwright codegen https://www.wehago.com --target python -o rec.py
  → 브라우저에서 직접 로그인·메뉴 이동·엑셀 다운로드까지 클릭
  → rec.py 의 page.goto / page.get_by_role(...).click() / page.fill(...) 를 위 steps 형식으로 옮겨 적는다
     (get_by_role("button", name="엑셀") 은 selector "role=button[name=\"엑셀\"]" 로 쓰면 된다)
  → 녹화본에 찍힌 아이디·비밀번호 값은 반드시 ${env:...} / secret 으로 바꾼다.

playwright 미설치여도 이 모듈 import 는 실패하지 않는다(실행 시점에만 필요).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..models import Client, Filing

STEP_TYPES = ("goto", "click", "fill", "wait", "download", "select", "press")
_PW_HINT = re.compile(r"pass|pw|pwd|비밀번호|암호|cert", re.I)
_VAR_RE = re.compile(r"\$\{(env|keyring):([^}]+)\}")


class RecipeError(ValueError):
    pass


class BrowserUnavailable(RuntimeError):
    pass


@dataclass
class Recipe:
    name: str
    steps: list[dict]
    start_url: str = ""
    description: str = ""
    headless: bool = True
    timeout_ms: int = 30000


def load_recipe(src: Path | dict) -> Recipe:
    d = yaml.safe_load(Path(src).read_text(encoding="utf-8")) if not isinstance(src, dict) else src
    d = d or {}
    r = Recipe(
        name=str(d.get("name") or (Path(src).stem if not isinstance(src, dict) else "recipe")),
        steps=list(d.get("steps") or []),
        start_url=str(d.get("start_url") or ""),
        description=str(d.get("description") or ""),
        headless=bool(d.get("headless", True)),
        timeout_ms=int(d.get("timeout_ms", 30000)),
    )
    validate_recipe(r)
    return r


def validate_recipe(r: Recipe) -> None:
    """형식 검사 + 비밀번호 평문 저장 차단."""
    for i, st in enumerate(r.steps, 1):
        if not isinstance(st, dict) or len(st) != 1:
            raise RecipeError(f"{r.name} 단계 {i}: '{{종류: 값}}' 한 쌍이어야 함")
        kind, arg = next(iter(st.items()))
        if kind not in STEP_TYPES:
            raise RecipeError(f"{r.name} 단계 {i}: 모르는 단계 '{kind}' (가능: {', '.join(STEP_TYPES)})")
        if kind == "fill":
            if not isinstance(arg, dict) or "selector" not in arg:
                raise RecipeError(f"{r.name} 단계 {i}: fill 은 {{selector, value|secret}}")
            val = arg.get("value")
            if "secret" not in arg and _PW_HINT.search(str(arg["selector"])):
                if not (isinstance(val, str) and _VAR_RE.fullmatch(val.strip())):
                    raise RecipeError(f"{r.name} 단계 {i}: 비밀번호 칸은 secret: 이름 또는 ${{env:..}} 만 허용(평문 저장 금지)")
        if kind == "download" and not (isinstance(arg, dict) and "click" in arg):
            raise RecipeError(f"{r.name} 단계 {i}: download 는 {{click, save_as}}")


def recipe_vars(client: Client, filing: Filing) -> dict[str, str]:
    return {
        "client_id": client.id,
        "client_name": client.name,
        "biz_no": client.biz_no,
        "period": filing.period.code,
        "year": str(filing.period.year),
        "start": filing.coverage_start.isoformat(),
        "end": filing.coverage_end.isoformat(),
        "start_ymd": filing.coverage_start.strftime("%Y%m%d"),
        "end_ymd": filing.coverage_end.strftime("%Y%m%d"),
    }


def resolve_secret(name: str, env: dict | None = None) -> str:
    env = os.environ if env is None else env
    if env.get(name):
        return env[name]
    try:
        import keyring  # type: ignore
    except ImportError:
        keyring = None
    if keyring is not None:
        v = keyring.get_password("taxauto", name)
        if v:
            return v
    raise RecipeError(f"비밀값 '{name}' 없음 - 환경변수 또는 OS 키체인(서비스 taxauto)에 등록")


def render(value: Any, variables: dict[str, str], env: dict | None = None) -> str:
    s = str(value)

    def sub(m: re.Match) -> str:
        src, key = m.group(1), m.group(2).strip()
        if src == "env":
            return resolve_secret(key, env)
        svc, _, user = key.partition("/")
        try:
            import keyring  # type: ignore

            v = keyring.get_password(svc, user)
        except ImportError:
            v = None
        if not v:
            raise RecipeError(f"키체인 값 없음: {svc}/{user}")
        return v

    s = _VAR_RE.sub(sub, s)
    for k, v in variables.items():
        s = s.replace("{" + k + "}", v)
    return s


def run_recipe(
    recipe: Recipe | Path | dict,
    dest_dir: Path,
    variables: dict[str, str],
    page: Any = None,
    env: dict | None = None,
) -> list[Path]:
    """레시피 실행 → 내려받은 파일 경로 목록. page 를 주입하면(테스트) 브라우저를 띄우지 않는다."""
    r = recipe if isinstance(recipe, Recipe) else load_recipe(recipe)
    dest_dir = Path(dest_dir)
    if page is not None:
        return _execute(r, page, dest_dir, variables, env)
    try:
        from playwright.sync_api import sync_playwright  # 지연 import
    except ImportError as e:
        raise BrowserUnavailable("playwright 미설치: pip install 'taxauto[browser]' && playwright install chromium") from e
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=r.headless)
        try:
            context = browser.new_context(accept_downloads=True, locale="ko-KR")
            pg = context.new_page()
            pg.set_default_timeout(r.timeout_ms)
            return _execute(r, pg, dest_dir, variables, env)
        finally:
            browser.close()


def _execute(r: Recipe, page: Any, dest_dir: Path, variables: dict[str, str], env: dict | None) -> list[Path]:
    out: list[Path] = []
    if r.start_url:
        page.goto(render(r.start_url, variables, env))
    for i, st in enumerate(r.steps, 1):
        kind, arg = next(iter(st.items()))
        try:
            if kind == "goto":
                page.goto(render(arg, variables, env))
            elif kind == "click":
                page.click(render(arg, variables, env))
            elif kind == "press":
                a = arg if isinstance(arg, dict) else {"selector": "body", "key": arg}
                page.press(render(a["selector"], variables, env), str(a["key"]))
            elif kind == "fill":
                val = resolve_secret(arg["secret"], env) if "secret" in arg else render(arg.get("value", ""), variables, env)
                page.fill(render(arg["selector"], variables, env), val)
            elif kind == "select":
                page.select_option(render(arg["selector"], variables, env), render(arg["value"], variables, env))
            elif kind == "wait":
                a = arg if isinstance(arg, dict) else {"selector": arg}
                if "ms" in a:
                    page.wait_for_timeout(int(a["ms"]))
                elif "url" in a:
                    page.wait_for_url(render(a["url"], variables, env), timeout=int(a.get("timeout", r.timeout_ms)))
                else:
                    page.wait_for_selector(render(a["selector"], variables, env), timeout=int(a.get("timeout", r.timeout_ms)))
            elif kind == "download":
                with page.expect_download() as info:
                    page.click(render(arg["click"], variables, env))
                dl = info.value
                name = render(arg.get("save_as") or dl.suggested_filename, variables, env)
                name = re.sub(r'[\\/:*?"<>|]', "_", name)
                dest_dir.mkdir(parents=True, exist_ok=True)
                target = dest_dir / name
                dl.save_as(str(target))
                out.append(target)
        except RecipeError:
            raise
        except Exception as e:
            # 비밀값이 메시지에 섞이지 않게 단계 정보만
            raise RuntimeError(f"레시피 '{r.name}' 단계 {i}({kind}) 실패: {type(e).__name__}") from None
    return out


def run_recipes_for(client: Client, filing: Filing, recipes_dir: Path, inbox_root: Path, page: Any = None) -> dict[str, list[Path]]:
    """config/recipes/*.yaml(예시 *.example.yaml 제외) 전부 실행 → inbox/{period}/{client_id}/"""
    dest = Path(inbox_root) / filing.period.code / client.id
    out: dict[str, list[Path]] = {}
    for p in sorted(Path(recipes_dir).glob("*.yaml")):
        if p.name.endswith(".example.yaml"):
            continue
        out[p.stem] = run_recipe(p, dest, recipe_vars(client, filing), page=page)
    return out

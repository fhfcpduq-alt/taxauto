"""위하고 레시피 재생기 (결정적 재생 = 토큰 0).

학습 모드에서 에이전트가 저장한 recipes/*.yaml 을 Playwright(Python sync API)로 그대로 재생한다.
실패하면 그 지점 정보({failed_step, error, screenshot, page_text_excerpt})를 돌려주고,
에이전트(Playwright MCP)가 거기서부터 이어받아 처리한 뒤 레시피를 고친다(self-healing).

레시피 형식 (recipes/README.md 에 상세)
  id: export_ledger                 # 레시피 id (파일명과 같게)
  version: 1
  procedure: procedures/40_export_ledger.md
  description: 매입매출 전표 목록 엑셀 다운로드
  vars: {menu: 매입매출전표입력}     # 레시피 기본 변수
  requires: [client.name, period.code]   # 실행 전 반드시 바인딩돼야 하는 변수
  steps:
    - id: open_menu                 # 생략 시 s01, s02 ...
      action: click                 # goto|click|fill|select|press|wait_for|expect_text|download|upload|screenshot
      role: button                  # 로케이터 우선순위: role+name > label > placeholder > text > test_id > selector(최후수단)
      name: 조회
      exact: false
      nth: 0                        # 같은 이름이 여러 개일 때만
      frame: "iframe#main"          # iframe 안이면
      value: "${client.name}"       # fill/select/press/upload 값. ${...} 변수 치환
      timeout_ms: 15000
      checkpoint: true              # 성공하면 재개 지점으로 기록
      optional: false               # true 면 실패해도 계속
      dialog: accept                # 이 단계에서 뜨는 확인창 처리(accept|dismiss). 기본 dismiss
      popup: false                  # 클릭으로 새 창이 뜨면 true → 이후 단계는 새 창에서
      save_as: "ledger_${period.code}.xlsx"   # download 저장 파일명

변수: client.{id,name,biz_no,biz_no_dash} period.{code,year,half,kind,label}
      filing.{coverage_start,coverage_end,due_date,preliminary_notice_tax,...} work.* (work_order.json)
      downloads.<step_id> (앞 단계에서 받은 파일 경로) + recipe vars + --var k=v
      비밀번호·인증서 같은 비밀값 변수는 지원하지 않는다(의도). 로그인은 브라우저 프로필 세션 유지로.

안전장치(taxauto.wehago.guard)
  - 실행 전 레시피 전체 사전검사(금지 문구·차단 URL·업로드 경로)
  - 매 단계 실제 화면 요소의 글자(aria-label/title/value/innerText)를 다시 검사
  - 매 단계 후 현재 URL 검사(홈택스 등으로 이동했으면 즉시 중단)
  - 페이지 주입 스크립트(브라우저 안 클릭 차단) 설치

결과물: data/{period}/{client}/wehago/
  replay/{recipe_id}/{시각}/  NN_step.png, log.jsonl, result.json
  downloads/                  다운로드 파일
  replay_state.json           레시피별 마지막 체크포인트(재개용)
  agent_log.jsonl             단계 기록 1줄 추가(source=replay)

CLI
  python -m taxauto.wehago.replay --recipe recipes/40_export_ledger.yaml --client C001 --period 2026-2P
        [--cdp http://127.0.0.1:9222 | --launch [--headed] [--url URL]] [--resume | --from-step ID]
        [--var key=value ...] [--no-step-screenshots]
  종료코드: 0 성공 / 1 단계 실패 / 2 안전장치 차단 / 3 레시피·변수 오류 / 4 브라우저 연결 실패
  stdout: 결과 JSON (에이전트가 읽음)
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..redact import redact
from .guard import Guard

ACTIONS = {"goto", "click", "fill", "select", "press", "wait_for", "expect_text", "download", "upload", "screenshot"}
LOCATOR_KEYS = ("role", "label", "placeholder", "text", "test_id", "selector")
# 요소를 건드리는 동작(실제 글자 가드 검사 대상)
ELEMENT_ACTIONS = {"click", "fill", "select", "press", "download", "upload"}
_VAR = re.compile(r"\$\{([A-Za-z0-9_.\-]+)\}")
REPO_ROOT = Path(__file__).resolve().parents[3]

EXIT_OK, EXIT_FAILED, EXIT_GUARD, EXIT_CONFIG, EXIT_BROWSER = 0, 1, 2, 3, 4

_ELEMENT_INFO_JS = """el => ({
  text: [el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('alt'),
         (el.tagName === 'INPUT' && /^(button|submit|image)$/i.test(el.type || '')) ? el.value : '',
         el.innerText || el.textContent].filter(Boolean).join(' ').slice(0, 300),
  type: (el.getAttribute('type') || '').toLowerCase(),
  name: el.getAttribute('name') || '',
  id: el.id || '',
  placeholder: el.getAttribute('placeholder') || '',
  autocomplete: el.getAttribute('autocomplete') || ''
})"""


class RecipeError(ValueError):
    """레시피 형식·변수 오류(재생 전)."""


class GuardBlocked(RuntimeError):
    def __init__(self, reason: str, rule: str = ""):
        super().__init__(reason)
        self.rule = rule


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 레시피 로딩·변수
# ---------------------------------------------------------------------------


def load_recipe(path: Path | str) -> dict:
    p = Path(path)
    try:
        r = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise RecipeError(f"레시피 읽기 실패: {p}: {e}") from None
    return normalize_recipe(r, default_id=p.stem)


def normalize_recipe(r: dict, default_id: str = "recipe") -> dict:
    if not isinstance(r, dict):
        raise RecipeError("레시피 최상위는 딕셔너리여야 함")
    r = dict(r)
    r.setdefault("id", default_id)
    steps = r.get("steps")
    if not isinstance(steps, list) or not steps:
        raise RecipeError("steps 가 비어 있음 (학습 전 레시피는 재생할 수 없음)")
    seen: set[str] = set()
    out = []
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            raise RecipeError(f"{i}번째 단계가 딕셔너리가 아님")
        s = dict(s)
        s.setdefault("id", f"s{i:02d}")
        s["id"] = str(s["id"])
        if s["id"] in seen:
            raise RecipeError(f"단계 id 중복: {s['id']}")
        seen.add(s["id"])
        act = s.get("action")
        if act not in ACTIONS:
            raise RecipeError(f"[{s['id']}] 알 수 없는 action: {act} (가능: {', '.join(sorted(ACTIONS))})")
        if act in ELEMENT_ACTIONS and not any(s.get(k) for k in LOCATOR_KEYS):
            if not (act == "press" and s.get("value")):
                raise RecipeError(f"[{s['id']}] {act} 에 대상 로케이터(role/name, text, selector 등)가 없음")
        if act == "goto" and not (s.get("url") or s.get("value")):
            raise RecipeError(f"[{s['id']}] goto 에 url 이 없음")
        out.append(s)
    r["steps"] = out
    return r


def lookup(ctx: dict, dotted: str) -> Any:
    cur: Any = ctx
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise KeyError(dotted)
    return cur


def subst(value: Any, ctx: dict) -> Any:
    """'${a.b}' 치환. 값 전체가 변수 하나면 원래 타입 유지. 없는 변수는 RecipeError."""
    if isinstance(value, list):
        return [subst(v, ctx) for v in value]
    if not isinstance(value, str):
        return value

    def rep(m: re.Match) -> str:
        try:
            v = lookup(ctx, m.group(1))
        except KeyError:
            raise RecipeError(f"정의되지 않은 변수: ${{{m.group(1)}}}") from None
        return "" if v is None else str(v)

    return _VAR.sub(rep, value)


def set_dotted(ctx: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = ctx
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def build_context(client_id: str, period_code: str, base_dir: Path, ws_root: Path,
                  recipe_vars: dict | None = None, extra: dict | None = None) -> dict:
    """재생 변수 바인딩. 거래처 명부·filing.json·work_order.json 이 있으면 읽는다."""
    ctx: dict[str, Any] = {"client": {"id": client_id}, "period": {"code": period_code}, "downloads": {}}
    try:
        from ..models import TaxPeriod

        tp = TaxPeriod.parse(period_code)
        ctx["period"].update({"year": tp.year, "half": tp.half, "kind": tp.kind, "label": tp.label})
    except Exception:
        pass
    try:
        from ..registry import load_clients

        for c in load_clients(base_dir / "clients"):
            if c.id == client_id:
                d = c.biz_no
                ctx["client"].update({"name": c.name, "biz_no": d,
                                      "biz_no_dash": f"{d[:3]}-{d[3:5]}-{d[5:]}" if len(d) == 10 else d,
                                      "taxpayer_type": c.taxpayer_type.value})
                break
    except Exception:
        pass
    for name, key in (("filing.json", "filing"), ("wehago/work_order.json", "work")):
        p = ws_root / name
        if p.exists():
            try:
                ctx[key] = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                pass
    for k, v in (recipe_vars or {}).items():
        ctx.setdefault(k, v)
    for k, v in (extra or {}).items():
        set_dotted(ctx, k, v)
    return ctx


def precheck(recipe: dict, ctx: dict, guard: Guard) -> list[dict]:
    """재생 전에 레시피 전체를 검사. 반환: 문제 목록 [{step, kind, reason}]  (kind: var|guard)"""
    problems: list[dict] = []
    for k in recipe.get("requires") or []:
        try:
            lookup(ctx, str(k))
        except KeyError:
            problems.append({"step": None, "kind": "var", "reason": f"필수 변수 없음: {k}"})
    for s in recipe["steps"]:
        sid, act = s["id"], s["action"]
        try:
            texts = [subst(s.get(k), ctx) for k in ("name", "text", "label", "placeholder", "selector", "test_id")]
            value = subst(s.get("value"), ctx)
            url = subst(s.get("url") or (s.get("value") if act == "goto" else None), ctx)
            save_as = subst(s.get("save_as"), ctx)
        except RecipeError as e:
            problems.append({"step": sid, "kind": "var", "reason": str(e)})
            continue
        if act in ELEMENT_ACTIONS:
            d = guard.check_text(*[t for t in texts if t])
            if not d:
                problems.append({"step": sid, "kind": "guard", "reason": d.reason, "rule": d.rule})
        if act in ("fill", "select"):
            for d in (guard.check_text(value), guard.check_input_target(*[t for t in texts if t])):
                if not d:
                    problems.append({"step": sid, "kind": "guard", "reason": d.reason, "rule": d.rule})
        if act == "press" and value:
            d = guard.check_key(value)
            if not d:
                problems.append({"step": sid, "kind": "guard", "reason": d.reason, "rule": d.rule})
        if act == "goto":
            d = guard.check_url(url)
            if not d:
                problems.append({"step": sid, "kind": "guard", "reason": d.reason, "rule": d.rule})
        if act == "upload" and value and "${downloads." not in str(s.get("value")):
            d = guard.check_upload_path(value)
            if not d:
                problems.append({"step": sid, "kind": "guard", "reason": d.reason, "rule": d.rule})
        if save_as and (Path(str(save_as)).name != str(save_as) or str(save_as).startswith(".")):
            problems.append({"step": sid, "kind": "var", "reason": f"save_as 는 파일명만: {save_as}"})
    return problems


# ---------------------------------------------------------------------------
# 재생
# ---------------------------------------------------------------------------


@dataclass
class ReplayOptions:
    step_timeout_ms: int = 15000
    deadline_sec: float | None = None        # 거래처당 시간 상한
    step_retries: int = 1
    step_screenshots: bool = True
    install_init_script: bool = True


@dataclass
class Replayer:
    recipe: dict
    ctx: dict
    guard: Guard
    wehago_dir: Path                      # data/{period}/{client}/wehago
    options: ReplayOptions = field(default_factory=ReplayOptions)

    def __post_init__(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = self.wehago_dir / "replay" / str(self.recipe["id"]) / stamp
        n = 1
        while self.run_dir.exists():
            n += 1
            self.run_dir = self.wehago_dir / "replay" / str(self.recipe["id"]) / f"{stamp}_{n}"
        self.downloads_dir = self.wehago_dir / "downloads"
        self.page: Any = None
        self.completed: list[str] = []
        self.last_checkpoint: dict | None = None
        self._pending_dialog: str = "dismiss"
        self._dialog_problem: str | None = None
        self._t0 = time.monotonic()
        self._initial_pages: set[int] = set()

    # -- 기록
    def _log(self, rec: dict) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        rec = {"ts": now_iso(), **rec}
        with (self.run_dir / "log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _shot(self, name: str) -> str | None:
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            p = self.run_dir / f"{name}.png"
            self.page.screenshot(path=str(p), full_page=False, timeout=5000)
            return str(p)
        except Exception:
            return None

    def _page_text(self, limit: int = 1500) -> str:
        try:
            return redact(self.page.inner_text("body", timeout=3000))[:limit]
        except Exception:
            return ""

    # -- 페이지 준비
    def attach(self, page: Any) -> None:
        self.page = page
        page.on("dialog", self._on_dialog)
        if self.options.install_init_script:
            script = self.guard.render_init_script()
            try:
                page.context.add_init_script(script)
            except Exception:
                pass
            for fr in page.frames:  # 이미 열린 문서에도 설치
                try:
                    fr.evaluate("() => {\n" + script + "\n}")
                except Exception:
                    pass

    def _on_dialog(self, dialog: Any) -> None:
        msg = getattr(dialog, "message", "") or ""
        d = self.guard.check_text(msg)
        try:
            if not d:
                self._dialog_problem = f"금지 문구가 있는 확인창 → 취소함: {msg[:80]}"
                dialog.dismiss()
            elif self._pending_dialog == "accept":
                dialog.accept()
            else:  # 예상하지 않은 확인창은 취소(레시피에 dialog: accept 로 명시해야 수락)
                dialog.dismiss()
        except Exception:
            pass
        self._log({"event": "dialog", "message": redact(msg)[:200], "handled": "dismiss" if not d else self._pending_dialog})

    # -- 로케이터
    def _locator(self, s: dict) -> Any:
        scope: Any = self.page
        if s.get("frame"):
            scope = self.page.frame_locator(subst(s["frame"], self.ctx))
        exact = bool(s.get("exact", False))
        name = subst(s.get("name"), self.ctx)
        if s.get("role"):
            loc = scope.get_by_role(s["role"], name=name, exact=exact) if name else scope.get_by_role(s["role"])
        elif s.get("label"):
            loc = scope.get_by_label(subst(s["label"], self.ctx), exact=exact)
        elif s.get("placeholder"):
            loc = scope.get_by_placeholder(subst(s["placeholder"], self.ctx), exact=exact)
        elif s.get("text"):
            loc = scope.get_by_text(subst(s["text"], self.ctx), exact=exact)
        elif s.get("test_id"):
            loc = scope.get_by_test_id(subst(s["test_id"], self.ctx))
        elif s.get("selector"):
            loc = scope.locator(subst(s["selector"], self.ctx))
        else:
            return None
        if s.get("nth") is not None:
            loc = loc.nth(int(s["nth"]))
        return loc

    def _guard_element(self, loc: Any, s: dict, timeout: int) -> dict:
        """실제 요소 글자로 가드 재검사. 통과하면 요소 정보 반환."""
        loc.wait_for(state="attached", timeout=timeout)
        info = loc.evaluate(_ELEMENT_INFO_JS)
        act = s["action"]
        d = self.guard.check_text(info.get("text"))
        if not d:
            raise GuardBlocked(f"실제 화면 요소 글자 차단: {d.reason}", d.rule)
        if act in ("fill", "select"):
            pw = "password" if info.get("type") == "password" or "password" in info.get("autocomplete", "") else ""
            d = self.guard.check_input_target(pw, info.get("name"), info.get("id"), info.get("placeholder"), info.get("text"))
            if not d:
                raise GuardBlocked(d.reason, d.rule)
        return info

    def _check_url(self) -> None:
        # 이 재생 중 새로 열린 창 + 현재 창만 검사(사람이 따로 열어 둔 탭은 건드리지 않음)
        try:
            others = [p for p in self.page.context.pages if p is not self.page and id(p) not in self._initial_pages]
        except Exception:
            others = []
        for pg in [self.page, *others]:
            try:
                url = pg.url
            except Exception:
                continue
            if not url or url == "about:blank":
                continue
            d = self.guard.check_url(url)
            if not d:
                raise GuardBlocked(f"현재 열린 페이지가 차단 대상: {d.reason}", d.rule)

    # -- 단계 실행
    def _do(self, s: dict) -> dict:
        act = s["action"]
        timeout = int(s.get("timeout_ms") or self.options.step_timeout_ms)
        value = subst(s.get("value"), self.ctx)
        detail: dict[str, Any] = {}
        self._pending_dialog = str(s.get("dialog") or "dismiss")
        self._dialog_problem = None

        if act == "goto":
            url = subst(s.get("url") or s.get("value"), self.ctx)
            d = self.guard.check_url(url)
            if not d:
                raise GuardBlocked(d.reason, d.rule)
            self.page.goto(url, timeout=timeout, wait_until=s.get("wait_until", "load"))
            detail["url"] = url
        elif act == "screenshot":
            detail["screenshot"] = self._shot(f"{s['id']}_manual")
        elif act == "wait_for":
            loc = self._locator(s)
            if loc is not None:
                loc.wait_for(state=s.get("state", "visible"), timeout=timeout)
            else:
                self.page.wait_for_timeout(min(timeout, 60000))
        elif act == "expect_text":
            text = str(subst(s.get("text") or s.get("value"), self.ctx))
            loc = self._locator({k: v for k, v in s.items() if k != "text"})
            if loc is not None:
                loc.filter(has_text=text).first.wait_for(state="visible", timeout=timeout)
            else:
                self.page.get_by_text(text, exact=bool(s.get("exact", False))).first.wait_for(state="visible", timeout=timeout)
            detail["text"] = text
        else:
            loc = self._locator(s)
            if act == "press" and loc is None:
                d = self.guard.check_key(value)
                if not d:
                    raise GuardBlocked(d.reason, d.rule)
                self.page.keyboard.press(str(value))
            else:
                info = self._guard_element(loc, s, timeout)
                detail["element_text"] = redact(info.get("text"))[:80]
                if act == "click":
                    if s.get("popup"):
                        with self.page.expect_popup(timeout=timeout) as pinfo:
                            loc.click(timeout=timeout)
                        self.attach(pinfo.value)
                        pinfo.value.wait_for_load_state(timeout=timeout)
                        detail["popup"] = True
                    elif s.get("double"):
                        loc.dblclick(timeout=timeout)
                    else:
                        loc.click(timeout=timeout)
                elif act == "fill":
                    d = self.guard.check_text(value)
                    if not d:
                        raise GuardBlocked(d.reason, d.rule)
                    loc.fill("" if value is None else str(value), timeout=timeout)
                elif act == "select":
                    loc.select_option(label=str(value), timeout=timeout)
                elif act == "press":
                    d = self.guard.check_key(value)
                    if not d:
                        raise GuardBlocked(d.reason, d.rule)
                    loc.press(str(value), timeout=timeout)
                elif act == "download":
                    with self.page.expect_download(timeout=timeout) as dinfo:
                        loc.click(timeout=timeout)
                    dl = dinfo.value
                    fname = str(subst(s.get("save_as"), self.ctx) or dl.suggested_filename or f"{s['id']}.bin")
                    fname = Path(fname).name
                    self.downloads_dir.mkdir(parents=True, exist_ok=True)
                    dest = self.downloads_dir / fname
                    dl.save_as(str(dest))
                    self.ctx.setdefault("downloads", {})[s["id"]] = str(dest)
                    detail["file"] = str(dest)
                elif act == "upload":
                    path = Path(str(value))
                    if not path.is_absolute():
                        path = self.guard.base_dir / path
                    d = self.guard.check_upload_path(path)
                    if not d:
                        raise GuardBlocked(d.reason, d.rule)
                    if not path.exists():
                        raise FileNotFoundError(f"업로드 파일 없음: {path}")
                    if s.get("chooser"):
                        with self.page.expect_file_chooser(timeout=timeout) as fc:
                            loc.click(timeout=timeout)
                        fc.value.set_files(str(path))
                    else:
                        loc.set_input_files(str(path), timeout=timeout)
                    detail["file"] = str(path)
        if s.get("settle_ms"):
            self.page.wait_for_timeout(int(s["settle_ms"]))
        if self._dialog_problem:
            raise GuardBlocked(self._dialog_problem, "dialog")
        blocked = self._init_script_blocks()
        if blocked:
            raise GuardBlocked(f"브라우저 주입 가드가 클릭을 막음: {blocked}", "init_script")
        self._check_url()
        return detail

    def _init_script_blocks(self) -> list:
        try:
            got = self.page.evaluate("() => { const b = window.__taxautoGuardBlocked || []; window.__taxautoGuardBlocked = []; return b; }")
            return got or []
        except Exception:
            return []

    def run(self, page: Any, start_index: int = 0) -> dict:
        try:
            self._initial_pages = {id(p) for p in page.context.pages if p is not page}
        except Exception:
            self._initial_pages = set()
        self.attach(page)
        steps = self.recipe["steps"]
        self._log({"event": "start", "recipe": self.recipe["id"], "start_index": start_index, "steps": len(steps)})
        for idx in range(start_index, len(steps)):
            s = steps[idx]
            tag = f"{idx + 1:02d}_{s['id']}"
            if self.options.deadline_sec is not None and time.monotonic() - self._t0 > self.options.deadline_sec:
                return self._fail(idx, s, tag, f"시간 상한 초과({int(self.options.deadline_sec)}초)", "timeout")
            attempts = 1 + max(0, int(s.get("retries", self.options.step_retries)))
            t_step = time.perf_counter()
            last_err: Exception | None = None
            for attempt in range(attempts):
                try:
                    detail = self._do(s)
                    last_err = None
                    break
                except GuardBlocked as e:
                    return self._fail(idx, s, tag, str(e), "guard", rule=e.rule)
                except RecipeError as e:
                    return self._fail(idx, s, tag, str(e), "recipe")
                except Exception as e:  # 타임아웃·요소 없음 등 → 재시도
                    last_err = e
                    self._log({"idx": idx, "id": s["id"], "event": "retry", "attempt": attempt + 1,
                               "error": redact(f"{type(e).__name__}: {e}")[:300]})
            if last_err is not None:
                if s.get("optional"):
                    self._log({"idx": idx, "id": s["id"], "action": s["action"], "status": "skipped_optional",
                               "error": redact(str(last_err))[:300]})
                    continue
                return self._fail(idx, s, tag, f"{type(last_err).__name__}: {last_err}", "step")
            shot = self._shot(tag) if self.options.step_screenshots else None
            self.completed.append(s["id"])
            self._log({"idx": idx, "id": s["id"], "action": s["action"], "status": "ok",
                       "ms": int((time.perf_counter() - t_step) * 1000), "detail": detail, "screenshot": shot})
            if s.get("checkpoint"):
                if not shot:
                    shot = self._shot(f"{tag}_checkpoint")
                self.last_checkpoint = {"index": idx, "id": s["id"], "at": now_iso(), "screenshot": shot}
                self._save_state(status="running")
        result = {
            "ok": True, "recipe": self.recipe["id"], "completed_steps": self.completed,
            "last_checkpoint": self.last_checkpoint, "downloads": self.ctx.get("downloads", {}),
            "run_dir": str(self.run_dir), "elapsed_sec": round(time.monotonic() - self._t0, 1),
        }
        self._finish(result)
        return result

    def _fail(self, idx: int, s: dict, tag: str, error: str, kind: str, rule: str = "") -> dict:
        shot = self._shot(f"{tag}_FAILED")
        result = {
            "ok": False,
            "recipe": self.recipe["id"],
            "failed_step": {"index": idx, "id": s["id"], "action": s["action"],
                            "target": {k: s.get(k) for k in (*LOCATOR_KEYS, "name", "frame", "nth") if s.get(k) is not None},
                            "note": s.get("note", "")},
            "error_kind": kind,          # step | guard | timeout | recipe
            "guard_rule": rule or None,
            "error": redact(error)[:1000],
            "screenshot": shot,
            "page_url": self._safe_url(),
            "page_text_excerpt": self._page_text(),
            "completed_steps": self.completed,
            "last_checkpoint": self.last_checkpoint,
            "resume_hint": (f"이 지점부터 Playwright MCP 로 이어서 처리. 레시피 수정 후 재개: --from-step {s['id']}"
                            if kind != "guard" else "안전장치 차단 — 이 동작은 하지 말고 검토항목으로 남길 것"),
            "run_dir": str(self.run_dir),
            "elapsed_sec": round(time.monotonic() - self._t0, 1),
        }
        self._log({"idx": idx, "id": s["id"], "action": s["action"], "status": "failed", "kind": kind,
                   "error": result["error"], "screenshot": shot})
        self._finish(result)
        return result

    def _safe_url(self) -> str:
        try:
            return str(self.page.url)
        except Exception:
            return ""

    def _save_state(self, status: str, result: dict | None = None) -> None:
        p = self.wehago_dir / "replay_state.json"
        try:
            st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except ValueError:
            st = {}
        st[str(self.recipe["id"])] = {
            "status": status, "updated_at": now_iso(), "last_checkpoint": self.last_checkpoint,
            "failed_step": (result or {}).get("failed_step", {}).get("id") if result and not result.get("ok") else None,
            "run_dir": str(self.run_dir),
        }
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    def _finish(self, result: dict) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self._save_state(status="ok" if result.get("ok") else "failed", result=result)
        note = "완료" if result.get("ok") else f"{(result.get('failed_step') or {}).get('id')}: {result.get('error', '')[:150]}"
        rec = {"ts": now_iso(), "step": f"replay:{self.recipe['id']}", "status": "ok" if result.get("ok") else "failed",
               "note": redact(note), "source": "replay", "run_dir": str(self.run_dir)}
        try:
            with (self.wehago_dir / "agent_log.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


def resume_index(recipe: dict, wehago_dir: Path, from_step: str | None = None, resume: bool = False) -> int:
    ids = [s["id"] for s in recipe["steps"]]
    if from_step:
        if from_step not in ids:
            raise RecipeError(f"--from-step {from_step} 이 레시피에 없음")
        return ids.index(from_step)
    if resume:
        p = wehago_dir / "replay_state.json"
        try:
            st = (json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}).get(str(recipe["id"])) or {}
        except ValueError:
            st = {}
        cp = st.get("last_checkpoint") or {}
        if st.get("status") != "ok" and cp.get("id") in ids:
            return ids.index(cp["id"]) + 1
    return 0


# ---------------------------------------------------------------------------
# 브라우저 연결 + 실행
# ---------------------------------------------------------------------------


def load_limits(base_dir: Path) -> dict:
    for p in (base_dir / "config" / "wehago" / "limits.yaml", REPO_ROOT / "config" / "wehago" / "limits.yaml"):
        if p.exists():
            try:
                return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                return {}
    return {}


def workspace_root(base_dir: Path, period_code: str, client_id: str) -> Path:
    data = "data"
    try:
        pol = yaml.safe_load((base_dir / "config" / "policy.yaml").read_text(encoding="utf-8")) or {}
        data = str((pol.get("run") or {}).get("workspace") or "data")
    except Exception:
        pass
    return base_dir / data / period_code / client_id


def _pick_cdp_page(browser: Any, guard: Guard) -> Any:
    pages = [p for c in browser.contexts for p in c.pages]
    for p in reversed(pages):
        try:
            if p.url and guard.check_url(p.url) and p.url != "about:blank":
                return p
        except Exception:
            continue
    if pages:
        return pages[-1]
    ctx = browser.contexts[0] if browser.contexts else browser.new_context(accept_downloads=True)
    return ctx.new_page()


def run_recipe(
    recipe: dict | Path | str,
    *,
    client_id: str,
    period: str,
    base_dir: Path | str | None = None,
    cdp: str | None = None,
    launch: bool = False,
    headless: bool = True,
    start_url: str | None = None,
    variables: dict | None = None,
    resume: bool = False,
    from_step: str | None = None,
    guard: Guard | None = None,
    options: ReplayOptions | None = None,
    ws_root: Path | None = None,
) -> tuple[int, dict]:
    """레시피 1개 재생. 반환 (종료코드, 결과 dict). 브라우저는 닫지 않는다(CDP 모드)."""
    base = Path(base_dir or REPO_ROOT).resolve()
    guard = guard or Guard.load(base_dir=base)
    try:
        rec = recipe if isinstance(recipe, dict) else load_recipe(recipe)
        rec = normalize_recipe(rec, default_id=str(rec.get("id", "recipe")))
    except RecipeError as e:
        return EXIT_CONFIG, {"ok": False, "error_kind": "recipe", "error": str(e)}
    if str(rec.get("status") or "active") == "example":
        return EXIT_CONFIG, {"ok": False, "recipe": rec["id"], "error_kind": "recipe",
                             "error": "예시 레시피(status: example)는 재생하지 않음 — 학습모드에서 실제 레시피를 만들 것"}
    ws = ws_root or workspace_root(base, period, client_id)
    wehago_dir = ws / "wehago"
    ctx = build_context(client_id, period, base, ws, rec.get("vars"), variables)
    problems = precheck(rec, ctx, guard)
    if problems:
        code = EXIT_GUARD if any(p["kind"] == "guard" for p in problems) else EXIT_CONFIG
        return code, {"ok": False, "recipe": rec["id"], "error_kind": "precheck", "problems": problems,
                      "error": "; ".join(f"[{p['step']}] {p['reason']}" for p in problems)[:1000]}
    limits = load_limits(base)
    if options is None:
        r = limits.get("replay") or {}
        options = ReplayOptions(
            step_timeout_ms=int(r.get("step_timeout_ms") or 15000),
            deadline_sec=float(limits.get("per_client_minutes") or 20) * 60,
            step_retries=int(r.get("step_retries") if r.get("step_retries") is not None else 1),
            step_screenshots=bool(r.get("step_screenshots", True)),
        )
    try:
        start = resume_index(rec, wehago_dir, from_step, resume)
    except RecipeError as e:
        return EXIT_CONFIG, {"ok": False, "error_kind": "recipe", "error": str(e)}

    try:
        from playwright.sync_api import sync_playwright  # 지연 import
    except ImportError:
        return EXIT_BROWSER, {"ok": False, "error_kind": "browser", "error": "playwright 미설치: pip install playwright"}

    if start_url:
        d = guard.check_url(start_url)
        if not d:
            return EXIT_GUARD, {"ok": False, "recipe": rec["id"], "error_kind": "guard", "error": d.reason}
    replayer = Replayer(rec, ctx, guard, wehago_dir, options)
    with sync_playwright() as pw:
        browser = None
        try:
            if launch:
                browser = pw.chromium.launch(headless=headless)
                page = browser.new_context(accept_downloads=True).new_page()
                if start_url:
                    page.goto(start_url)
            else:
                endpoint = cdp or str(limits.get("cdp_endpoint") or "http://127.0.0.1:9222")
                browser = pw.chromium.connect_over_cdp(endpoint, timeout=15000)
                page = _pick_cdp_page(browser, guard)
        except Exception as e:
            return EXIT_BROWSER, {"ok": False, "error_kind": "browser",
                                  "error": redact(f"브라우저 연결 실패: {type(e).__name__}: {e}")[:500],
                                  "hint": "scripts/start_browser.ps1 로 전용 크롬을 먼저 띄웠는지 확인"}
        try:
            result = replayer.run(page, start)
        finally:
            if launch and browser is not None:
                browser.close()
    if result.get("ok"):
        return EXIT_OK, result
    return (EXIT_GUARD if result.get("error_kind") == "guard" else EXIT_FAILED), result


def _parse_vars(items: Iterable[str]) -> dict:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise RecipeError(f"--var 는 key=value 형식: {it}")
        k, v = it.split("=", 1)
        out[k.strip()] = v
    return out


def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m taxauto.wehago.replay", description="위하고 레시피 재생기")
    ap.add_argument("--recipe", required=True)
    ap.add_argument("--client", required=True, help="거래처 id")
    ap.add_argument("--period", required=True, help="신고회차 예: 2026-2P")
    ap.add_argument("--base-dir", help="저장소 루트(기본: 현재 저장소)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--cdp", help="CDP 주소(기본: limits.yaml cdp_endpoint)")
    g.add_argument("--launch", action="store_true", help="새 크롬을 띄워 실행(테스트용)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--url", help="--launch 시 시작 URL")
    ap.add_argument("--var", action="append", default=[], help="변수 key=value (여러 번)")
    r = ap.add_mutually_exclusive_group()
    r.add_argument("--resume", action="store_true", help="마지막 체크포인트 다음부터")
    r.add_argument("--from-step", help="이 단계 id 부터")
    ap.add_argument("--no-step-screenshots", action="store_true")
    ap.add_argument("--check-only", action="store_true", help="브라우저 없이 사전검사만")
    a = ap.parse_args(list(argv) if argv is not None else None)

    base = Path(a.base_dir).resolve() if a.base_dir else REPO_ROOT
    try:
        variables = _parse_vars(a.var)
        recipe = load_recipe(a.recipe if Path(a.recipe).is_absolute() else (base / a.recipe if (base / a.recipe).exists() else a.recipe))
    except RecipeError as e:
        print(json.dumps({"ok": False, "error_kind": "recipe", "error": str(e)}, ensure_ascii=False, indent=2))
        return EXIT_CONFIG
    if a.check_only:
        guard = Guard.load(base_dir=base)
        ws = workspace_root(base, a.period, a.client)
        ctx = build_context(a.client, a.period, base, ws, recipe.get("vars"), variables)
        problems = precheck(recipe, ctx, guard)
        print(json.dumps({"ok": not problems, "recipe": recipe["id"], "problems": problems}, ensure_ascii=False, indent=2))
        return EXIT_OK if not problems else (EXIT_GUARD if any(p["kind"] == "guard" for p in problems) else EXIT_CONFIG)
    opts = None
    if a.no_step_screenshots:
        lim = load_limits(base)
        rr = lim.get("replay") or {}
        opts = ReplayOptions(step_timeout_ms=int(rr.get("step_timeout_ms") or 15000),
                             deadline_sec=float(lim.get("per_client_minutes") or 20) * 60,
                             step_retries=int(rr.get("step_retries") if rr.get("step_retries") is not None else 1),
                             step_screenshots=False)
    code, result = run_recipe(recipe, client_id=a.client, period=a.period, base_dir=base, cdp=a.cdp,
                              launch=a.launch, headless=not a.headed, start_url=a.url, variables=variables,
                              resume=a.resume, from_step=a.from_step, options=opts)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

"""위하고 조작 안전장치(금지행동 판정).

세 군데에서 같은 규칙(config/wehago/guard.yaml)을 쓴다.
  - Claude Code PreToolUse 훅  : scripts/hooks/wehago_guard.py → hook_main()
  - 레시피 재생기              : taxauto.wehago.replay (사전검사 + 실제 요소 글자 검사)
  - 페이지 주입 스크립트        : render_init_script() → scripts/wehago_guard_init.js

한계: 훅이 보는 것은 에이전트가 쓴 '설명 텍스트'와 selector 뿐이다. 실제 화면 글자는
재생기·주입 스크립트가 본다. 가장 강한 방어는 위하고 계정 권한(제출 권한 없는 전용 계정)이다.

CLI
  python -m taxauto.wehago.guard --write-init-script [경로]   주입 스크립트 재생성
  python -m taxauto.wehago.guard --check-text "전자신고 제출"
  python -m taxauto.wehago.guard --check-url https://www.hometax.go.kr/
  python -m taxauto.wehago.guard --hook   < hook.json        (훅과 동일)
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "wehago" / "guard.yaml"
DEFAULT_INIT_SCRIPT_PATH = REPO_ROOT / "scripts" / "wehago_guard_init.js"

# guard.yaml 을 못 읽을 때 쓰는 최소 규칙(엄격). 설정 파일이 없어도 제출·JS 실행은 막힌다.
FALLBACK_CONFIG: dict[str, Any] = {
    "forbidden_text": ["제출", "신고하기", "전송", "발행", "발급", "일괄삭제", "전체삭제", "회사삭제", "초기화",
                       "마감", "비밀번호", "인증서", "탈퇴", "해지", "결제", "납부하기", "이체", "발송",
                       "submit", "password"],
    "allow_exceptions": [],
    "forbidden_input_targets": ["비밀번호", "패스워드", "password", "인증서", "pin", "otp"],
    "allowed_hosts": ["wehago.com", "*.wehago.com", "about:blank"],
    "blocked_hosts": ["hometax.go.kr", "*.hometax.go.kr", "nts.go.kr", "*.nts.go.kr"],
    "blocked_url_keywords": ["submit"],
    "allowed_schemes": ["https", "about"],
    "upload_roots": ["data"],
    "upload_extensions": [".xlsx", ".xls", ".csv"],
    "tools": {
        "readonly": ["browser_snapshot", "browser_take_screenshot", "browser_wait_for"],
        "text_checked": {"browser_click": ["element", "target"], "browser_type": ["element", "target", "text"]},
        "url_checked": {"browser_navigate": "url"},
        "path_checked": {},
        "always_block": ["browser_evaluate", "browser_run_code", "browser_run_code_unsafe"],
    },
    "blocked_keys": ["Delete", "F5"],
    "min_click_description_chars": 2,
    "bash_forbidden": ["9222", "connect_over_cdp", "remote-debugging", "hometax"],
    "bash_allowed_prefixes": ["python -m taxauto.wehago."],
}

# 브라우저 조작 MCP 서버 이름(도구명 mcp__{server}__browser_*). 다른 이름으로 등록해도 browser_* 는 모두 검사.
_BROWSER_TOOL = re.compile(r"^mcp__(?P<server>.+?)__(?P<name>browser_[A-Za-z0-9_]+)$")
# 다른 MCP 서버라도 이름에 이게 들어가면 임의 코드 실행으로 보고 차단
_CODE_EXEC_HINT = re.compile(r"(evaluate|javascript|run_code|run_script|execute_script|exec_js)", re.I)
_SHELL_CHAIN = re.compile(r"(;|&&|\|\||\||`|\$\(|>|<|\n)")
_WS = re.compile(r"[\s​-‍﻿]+")


def norm(s: Any) -> str:
    """비교용 정규화: NFKC + 공백·폭없는문자 제거 + 소문자."""
    if s is None:
        return ""
    return _WS.sub("", unicodedata.normalize("NFKC", str(s))).lower()


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    rule: str = ""

    def __bool__(self) -> bool:  # if guard.check_x(...): 허용
        return self.allowed

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "rule": self.rule}


ALLOW = Decision(True)


def _deny(reason: str, rule: str) -> Decision:
    return Decision(False, reason, rule)


def _flatten(v: Any) -> list[str]:
    """도구 입력값(문자열·리스트·딕셔너리 중첩)을 문자열 목록으로."""
    if v is None:
        return []
    if isinstance(v, (str, int, float, bool)):
        return [str(v)]
    if isinstance(v, dict):
        out: list[str] = []
        for x in v.values():
            out += _flatten(x)
        return out
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            out += _flatten(x)
        return out
    return [str(v)]


def _host_match(host: str, pattern: str) -> bool:
    host, pattern = host.lower().rstrip("."), pattern.lower().strip()
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    return host == pattern or host.endswith("." + pattern)


class Guard:
    def __init__(self, cfg: dict | None = None, base_dir: Path | None = None, source: str = ""):
        c = dict(FALLBACK_CONFIG)
        c.update(cfg or {})
        self.cfg = c
        self.base_dir = Path(base_dir or REPO_ROOT).resolve()
        self.source = source
        self.terms = [t for t in (norm(x) for x in c.get("forbidden_text") or []) if t]
        self.exceptions = [t for t in (norm(x) for x in c.get("allow_exceptions") or []) if t]
        self.input_targets = [t for t in (norm(x) for x in c.get("forbidden_input_targets") or []) if t]
        tools = c.get("tools") or {}
        self.readonly = set(tools.get("readonly") or [])
        self.text_checked: dict[str, list[str]] = dict(tools.get("text_checked") or {})
        self.url_checked: dict[str, str] = dict(tools.get("url_checked") or {})
        self.path_checked: dict[str, str] = dict(tools.get("path_checked") or {})
        self.always_block = set(tools.get("always_block") or [])
        self.blocked_keys = {norm(k) for k in c.get("blocked_keys") or []}

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: Path | str | None = None, base_dir: Path | str | None = None,
             overrides: dict | None = None) -> "Guard":
        base = Path(base_dir or os.environ.get("TAXAUTO_HOME") or os.environ.get("CLAUDE_PROJECT_DIR") or REPO_ROOT)
        p = Path(path) if path else base / "config" / "wehago" / "guard.yaml"
        if not p.exists():
            p = DEFAULT_CONFIG_PATH
        cfg: dict = {}
        src = "fallback"
        try:
            import yaml  # 지연 import: 없으면 최소 규칙

            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            src = str(p)
        except Exception:
            cfg = {}
        if overrides:
            cfg = {**cfg, **overrides}
        return cls(cfg, base_dir=base, source=src)

    # ------------------------------------------------------------------ text
    def find_forbidden(self, text: Any) -> str | None:
        """금지어가 (예외 문구에 덮이지 않고) 있으면 그 금지어, 없으면 None."""
        t = norm(text)
        if not t:
            return None
        for term in self.terms:
            i = t.find(term)
            while i != -1:
                if not self._excused(t, i, len(term)):
                    return term
                i = t.find(term, i + 1)
        return None

    def _excused(self, t: str, i: int, n: int) -> bool:
        for ex in self.exceptions:
            j = t.find(ex)
            while j != -1:
                if j <= i and i + n <= j + len(ex):
                    return True
                j = t.find(ex, j + 1)
        return False

    def check_text(self, *texts: Any) -> Decision:
        for s in texts:
            for x in _flatten(s):
                hit = self.find_forbidden(x)
                if hit:
                    return _deny(f"금지 동작 문구 '{hit}' 포함: {str(x)[:80]}", "forbidden_text")
        return ALLOW

    def check_input_target(self, *texts: Any) -> Decision:
        for s in texts:
            for x in _flatten(s):
                t = norm(x)
                for k in self.input_targets:
                    if k and k in t:
                        return _deny(f"입력 금지 칸('{k}'): {str(x)[:80]} — 로그인·인증 정보는 사람만 입력", "forbidden_input")
        return ALLOW

    def check_key(self, key: Any) -> Decision:
        k = norm(key)
        if k in self.blocked_keys:
            return _deny(f"금지 키 '{key}' (그리드 삭제 단축키일 수 있음)", "blocked_key")
        return self.check_text(key)

    # ------------------------------------------------------------------ url
    def check_url(self, url: Any) -> Decision:
        s = str(url or "").strip()
        if not s:
            return _deny("빈 URL", "url")
        try:
            u = urlsplit(s)
        except ValueError:
            return _deny(f"URL 해석 불가: {s[:80]}", "url")
        scheme = (u.scheme or "").lower()
        schemes = {x.lower() for x in self.cfg.get("allowed_schemes") or []}
        if scheme not in schemes:
            return _deny(f"허용되지 않은 URL 형식({scheme or '상대경로'}): {s[:80]}", "url_scheme")
        if scheme == "about":
            return ALLOW
        host = (u.hostname or "").lower()
        if scheme != "file":
            for b in self.cfg.get("blocked_hosts") or []:
                if _host_match(host, str(b)):
                    return _deny(f"차단 도메인({b}): {s[:80]} — 홈택스 등 신고·제출 사이트는 에이전트 접근 금지", "blocked_host")
            if not any(_host_match(host, str(a)) for a in self.cfg.get("allowed_hosts") or [] if "://" not in str(a) and ":" not in str(a)):
                return _deny(f"허용 목록에 없는 도메인: {host or s[:80]}", "host_not_allowed")
        rest = norm(f"{u.path}?{u.query}#{u.fragment}")
        for kw in self.cfg.get("blocked_url_keywords") or []:
            if norm(kw) and norm(kw) in rest:
                return _deny(f"URL 금지어 '{kw}': {s[:80]}", "blocked_url_keyword")
        return ALLOW

    # ------------------------------------------------------------------ file
    def check_upload_path(self, path: Any) -> Decision:
        try:
            p = Path(str(path))
            p = (p if p.is_absolute() else self.base_dir / p).resolve()
        except (OSError, ValueError):
            return _deny(f"경로 해석 불가: {path}", "upload_path")
        exts = {e.lower() for e in self.cfg.get("upload_extensions") or []}
        if exts and p.suffix.lower() not in exts:
            return _deny(f"업로드 허용 확장자 아님({p.suffix}): {p.name}", "upload_ext")
        roots = [(self.base_dir / r).resolve() for r in self.cfg.get("upload_roots") or []]
        if not any(p == r or r in p.parents for r in roots):
            return _deny(f"업로드 허용 폴더 밖: {p}", "upload_root")
        return ALLOW

    # ------------------------------------------------------------------ bash
    def check_bash(self, command: Any) -> Decision:
        cmd = str(command or "").strip()
        low = cmd.lower()
        if any(low.startswith(p.lower()) for p in self.cfg.get("bash_allowed_prefixes") or []) and not _SHELL_CHAIN.search(cmd):
            return ALLOW
        for kw in self.cfg.get("bash_forbidden") or []:
            if str(kw).lower() in low:
                return _deny(f"브라우저 직접 조종/금지 사이트 의심 명령('{kw}'). 위하고 조작은 Playwright MCP 또는 "
                             f"'python -m taxauto.wehago.replay' 로만", "bash_forbidden")
        return ALLOW

    # ------------------------------------------------------------------ tool call (훅)
    def check_tool_call(self, tool_name: str, tool_input: dict | None) -> Decision:
        tool_name = str(tool_name or "")
        ti = tool_input if isinstance(tool_input, dict) else {}
        if tool_name == "Bash":
            # 기본은 위하고 에이전트 실행(TAXAUTO_WEHAGO_AGENT=1, scripts/wehago_agent_run.ps1 이 설정)에서만 검사.
            # 개발 세션에서 문서·테스트 명령까지 막지 않기 위함. 이 환경변수는 Claude Code 프로세스 것이라 에이전트가 못 바꾼다.
            mode = str(self.cfg.get("bash_check") or "agent_only")
            if mode == "off" or (mode == "agent_only" and os.environ.get("TAXAUTO_WEHAGO_AGENT") != "1"):
                return ALLOW
            return self.check_bash(ti.get("command"))
        m = _BROWSER_TOOL.match(tool_name)
        if not m:
            if tool_name.startswith("mcp__") and _CODE_EXEC_HINT.search(tool_name):
                return _deny(f"임의 코드 실행 도구 차단: {tool_name}", "code_exec")
            return ALLOW
        name = m.group("name")
        if name in self.always_block:
            return _deny(f"항상 차단 도구: {name} (임의 JS 실행·좌표 조작은 가드를 우회하므로 금지)", "always_block")
        if name in self.readonly:
            return ALLOW
        if name in self.text_checked:
            return self._check_text_tool(name, ti)
        if name in self.url_checked:
            url = ti.get(self.url_checked[name])
            if name == "browser_tabs" and not url:
                return ALLOW
            return self.check_url(url)
        if name in self.path_checked:
            paths = ti.get(self.path_checked[name]) or []
            for p in paths if isinstance(paths, list) else [paths]:
                d = self.check_upload_path(p)
                if not d:
                    return d
            return ALLOW
        return _deny(f"허용 목록에 없는 브라우저 도구: {name} (config/wehago/guard.yaml tools 에 사람이 추가해야 함)", "unknown_tool")

    def _check_text_tool(self, name: str, ti: dict) -> Decision:
        fields = self.text_checked.get(name) or []
        values = [ti.get(f) for f in fields]
        if name == "browser_click":
            desc = str(ti.get("element") or "").strip()
            if len(norm(desc)) < int(self.cfg.get("min_click_description_chars") or 0):
                return _deny("클릭 대상 설명(element)이 없음 — 화면에 보이는 이름 그대로 적어야 클릭 가능", "no_description")
        if name == "browser_press_key":
            return self.check_key(ti.get("key"))
        d = self.check_text(*values)
        if not d:
            return d
        if name == "browser_type":
            return self.check_input_target(ti.get("element"), ti.get("target"))
        if name == "browser_fill_form":
            for f in ti.get("fields") or []:
                if isinstance(f, dict):
                    d = self.check_input_target(f.get("element"), f.get("target"), f.get("name"))
                    if not d:
                        return d
        return ALLOW

    # ------------------------------------------------------------------ 페이지 주입 스크립트
    def render_init_script(self) -> str:
        cfg = {
            "terms": self.terms,
            "exceptions": self.exceptions,
            "blockedHosts": [str(h).lower() for h in self.cfg.get("blocked_hosts") or []],
        }
        return _INIT_JS.replace("__CFG__", json.dumps(cfg, ensure_ascii=False))


_INIT_JS = r"""// 자동 생성 파일 — 직접 고치지 말 것.  원본: config/wehago/guard.yaml
// 재생성: python -m taxauto.wehago.guard --write-init-script
// 브라우저 안에서 금지 문구 버튼/링크의 클릭·제출을 막는다(DOM 기반 화면에서만 유효, 캔버스 그리드는 못 막음).
(() => {
  if (window.__taxautoGuardInstalled) return;
  window.__taxautoGuardInstalled = true;
  const CFG = __CFG__;
  const norm = (s) => String(s || '').normalize('NFKC').replace(/[\s​-‍﻿]+/g, '').toLowerCase();
  const excused = (t, i, n) => CFG.exceptions.some((ex) => {
    let j = t.indexOf(ex);
    while (j !== -1) { if (j <= i && i + n <= j + ex.length) return true; j = t.indexOf(ex, j + 1); }
    return false;
  });
  const forbidden = (text) => {
    const t = norm(text);
    if (!t) return null;
    for (const term of CFG.terms) {
      let i = t.indexOf(term);
      while (i !== -1) { if (!excused(t, i, term.length)) return term; i = t.indexOf(term, i + 1); }
    }
    return null;
  };
  const INTERACTIVE = 'button,a,[role=button],[role=menuitem],[role=tab],[role=link],[role=option],input[type=button],input[type=submit],input[type=image],[onclick]';
  const labelOf = (el) => {
    if (!el || el.nodeType !== 1) return '';
    const v = (el.tagName === 'INPUT' && /^(button|submit|image)$/i.test(el.type || '')) ? el.value : '';
    return [el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('alt'), v, el.innerText || el.textContent]
      .filter(Boolean).join(' ').slice(0, 300);
  };
  const targetText = (ev) => {
    const path = ev.composedPath ? ev.composedPath() : [ev.target];
    for (const n of path) {
      if (n && n.nodeType === 1 && n.matches && n.matches(INTERACTIVE)) {
        const s = labelOf(n);
        if (s.length <= 120) return s;   // 큰 컨테이너는 오탐 방지 위해 제외
        break;
      }
    }
    const s = labelOf(ev.target);
    return s.length <= 60 ? s : '';
  };
  const blockedHost = (href) => {
    try {
      const h = new URL(href, location.href).hostname.toLowerCase();
      return CFG.blockedHosts.some((b) => b.startsWith('*.') ? (h === b.slice(2) || h.endsWith(b.slice(1))) : (h === b || h.endsWith('.' + b)));
    } catch (e) { return false; }
  };
  const record = (kind, why, text) => {
    const r = { kind, why, text: String(text || '').slice(0, 80), at: new Date().toISOString() };
    (window.__taxautoGuardBlocked = window.__taxautoGuardBlocked || []).push(r);
    try { console.warn('[taxauto-guard] 차단 ' + JSON.stringify(r)); } catch (e) {}
  };
  const stop = (ev, why, text) => { ev.preventDefault(); ev.stopImmediatePropagation(); ev.stopPropagation(); record(ev.type, why, text); };
  const onPointer = (ev) => {
    const text = targetText(ev);
    const hit = forbidden(text);
    if (hit) return stop(ev, '금지어:' + hit, text);
    const a = ev.target && ev.target.closest && ev.target.closest('a[href]');
    if (a && blockedHost(a.getAttribute('href'))) return stop(ev, '차단도메인', a.getAttribute('href'));
  };
  for (const t of ['click', 'dblclick', 'auxclick', 'mousedown', 'mouseup', 'pointerdown', 'pointerup']) {
    window.addEventListener(t, onPointer, true);
  }
  window.addEventListener('submit', (ev) => {
    const s = ev.submitter ? labelOf(ev.submitter) : '';
    const hit = forbidden(s) || forbidden(ev.target && ev.target.getAttribute && (ev.target.getAttribute('action') || ''));
    if (hit) stop(ev, '금지어:' + hit, s);
  }, true);
  const _open = window.open;
  window.open = function (url, ...rest) {
    if (url && blockedHost(String(url))) { record('window.open', '차단도메인', String(url)); return null; }
    return _open.call(this, url, ...rest);
  };
})();
"""


# ---------------------------------------------------------------------------
# 훅 진입점
# ---------------------------------------------------------------------------

_MUTATING_HINT = ("click", "type", "fill", "select", "press", "navigate", "upload", "tabs", "dialog", "drag")


def _log_decision(guard: Guard, tool_name: str, tool_input: dict, d: Decision) -> None:
    """차단 + 화면을 바꾸는 동작은 data/_wehago_guard/guard_log.jsonl 에 남긴다(감사 추적). 실패해도 무시."""
    try:
        if d.allowed and not any(h in tool_name for h in _MUTATING_HINT):
            return
        from ..redact import redact  # 입력값 마스킹

        rec = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tool": tool_name,
            "allowed": d.allowed,
            "rule": d.rule,
            "reason": redact(d.reason),
            "input": redact(json.dumps(tool_input, ensure_ascii=False))[:400],
        }
        p = guard.base_dir / "data" / "_wehago_guard" / "guard_log.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def hook_main(stdin: Any = None, stderr: Any = None) -> int:
    """Claude Code PreToolUse 훅. 반환값 = 종료코드(0 허용, 2 차단·사유는 stderr → 에이전트에게 전달)."""
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    try:
        raw = stdin.read()
        data = json.loads(raw or "{}")
        tool_name = str(data.get("tool_name") or "")
        tool_input = data.get("tool_input") or {}
    except Exception as e:  # 입력을 못 읽으면 안전하게 차단
        print(f"[wehago-guard] 훅 입력 해석 실패 → 차단: {type(e).__name__}", file=stderr)
        return 2
    try:
        g = Guard.load()
        d = g.check_tool_call(tool_name, tool_input)
    except Exception as e:  # 가드 자체 오류: 브라우저·Bash 도구면 차단(fail-closed)
        if tool_name.startswith("mcp__") or tool_name == "Bash":
            print(f"[wehago-guard] 가드 오류로 차단: {type(e).__name__}: {e}", file=stderr)
            return 2
        return 0
    _log_decision(g, tool_name, tool_input if isinstance(tool_input, dict) else {}, d)
    if d.allowed:
        return 0
    print(f"[wehago-guard] 차단({d.rule}): {d.reason}\n"
          f"→ 이 동작은 하지 말 것. 필요하면 스크린샷과 함께 검토항목을 남기고 다음 단계/거래처로 넘어갈 것.", file=stderr)
    return 2


def write_init_script(path: Path | str | None = None, guard: Guard | None = None) -> Path:
    p = Path(path) if path else DEFAULT_INIT_SCRIPT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text((guard or Guard.load()).render_init_script(), encoding="utf-8")
    return p


def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m taxauto.wehago.guard", description="위하고 조작 금지행동 판정")
    ap.add_argument("--config", help="guard.yaml 경로")
    ap.add_argument("--write-init-script", nargs="?", const="", metavar="PATH", help="페이지 주입 스크립트 생성")
    ap.add_argument("--check-text")
    ap.add_argument("--check-url")
    ap.add_argument("--check-upload")
    ap.add_argument("--hook", action="store_true", help="stdin 훅 JSON 검사(훅과 동일 동작)")
    a = ap.parse_args(list(argv) if argv is not None else None)
    if a.hook:
        return hook_main()
    g = Guard.load(a.config)
    if a.write_init_script is not None:
        p = write_init_script(a.write_init_script or None, g)
        print(str(p))
        return 0
    results = {}
    if a.check_text is not None:
        results["text"] = g.check_text(a.check_text).to_dict()
    if a.check_url is not None:
        results["url"] = g.check_url(a.check_url).to_dict()
    if a.check_upload is not None:
        results["upload"] = g.check_upload_path(a.check_upload).to_dict()
    print(json.dumps(results or {"config": g.source}, ensure_ascii=False, indent=2))
    return 0 if all(r["allowed"] for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

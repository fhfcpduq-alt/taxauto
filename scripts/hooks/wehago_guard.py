#!/usr/bin/env python
"""Claude Code PreToolUse 훅: 위하고 조작 금지행동 차단.

.claude/settings.json 에 등록돼 있다. stdin 으로 훅 JSON({tool_name, tool_input, ...})을 받아
금지면 종료코드 2 + stderr 사유(→ 에이전트에게 전달되어 그 도구 호출이 취소됨), 허용이면 0.

규칙: config/wehago/guard.yaml  (판정 로직: src/taxauto/wehago/guard.py)
가드 모듈을 못 불러오면 브라우저·Bash 도구는 차단한다(fail-closed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _fallback(raw: str, err: Exception) -> int:
    try:
        name = str(json.loads(raw or "{}").get("tool_name") or "")
    except Exception:
        name = ""
    if name.startswith("mcp__") or name == "Bash" or not name:
        print(f"[wehago-guard] 가드 모듈 로딩 실패로 차단: {type(err).__name__}: {err}", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    raw = sys.stdin.read()
    try:
        import io

        from taxauto.wehago.guard import hook_main
    except Exception as e:  # noqa: BLE001
        return _fallback(raw, e)
    return hook_main(io.StringIO(raw), sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

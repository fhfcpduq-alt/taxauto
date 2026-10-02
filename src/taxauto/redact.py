"""민감정보 마스킹 (로그·state.json·리포트·LLM 프롬프트 공용).

주민등록번호, 카드번호 전체, 계좌번호처럼 보이는 긴 숫자열을 가린다.
사업자번호(10자리)는 업무상 필요하므로 별도 함수 mask_biz_no 로만 부분 마스킹.
"""

from __future__ import annotations

import re

# 주민등록번호/외국인등록번호: 6자리-7자리
_RRN = re.compile(r"(?<!\d)(\d{6})[- ]?([1-8])\d{6}(?!\d)")
# 카드번호: 4-4-4-4 (구분자 선택)
_CARD = re.compile(r"(?<!\d)(\d{4})[- ]?(\d{4}|\*{4})[- ]?(\d{4}|\*{4})[- ]?(\d{4})(?!\d)")
# 그 외 12자리 이상 연속 숫자(계좌번호 등)
_LONG = re.compile(r"(?<!\d)(\d{3})\d{6,}(\d{2})(?!\d)")
# 키/비밀번호 형태
_SECRET = re.compile(r"(?i)\b(sk-[a-z0-9_\-]{6,}|(?:api[_-]?key|password|passwd|pwd|secret)\s*[=:]\s*\S+)")


def redact(text: str | None) -> str:
    if not text:
        return ""
    s = str(text)
    s = _SECRET.sub("[비밀값 가림]", s)
    s = _RRN.sub(lambda m: f"{m.group(1)}-{m.group(2)}******", s)
    s = _CARD.sub(lambda m: f"{m.group(1)}-****-****-{m.group(4)}", s)
    s = _LONG.sub(lambda m: f"{m.group(1)}******{m.group(2)}", s)
    return s


def mask_biz_no(biz_no: str | None) -> str:
    """1234567890 → 123-45-67***"""
    d = "".join(ch for ch in str(biz_no or "") if ch.isdigit())
    if len(d) != 10:
        return "***" if d else ""
    return f"{d[:3]}-{d[3:5]}-{d[5:7]}***"


def fmt_biz_no(biz_no: str | None) -> str:
    """1234567890 → 123-45-67890 (내부 리포트용, 외부 전송 금지)"""
    d = "".join(ch for ch in str(biz_no or "") if ch.isdigit())
    return f"{d[:3]}-{d[3:5]}-{d[5:]}" if len(d) == 10 else d

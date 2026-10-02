"""법 파라미터·금액 계산 보조 함수 (compute·validate 공용)."""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from typing import Any

from ..law import Law, LawParamMissing


def to_fraction(v: Any) -> Fraction:
    """0.013 / "0.013" / "9/109" → Fraction. float 오차를 피하려고 문자열 경유."""
    if isinstance(v, Fraction):
        return v
    return Fraction(str(v).strip())


def mul_floor(amount: int, rate: Any) -> int:
    """금액 × 율, 원 미만 절사(0 방향)."""
    return int(Fraction(int(amount)) * to_fraction(rate))


def law_try(law: Law, key: str, on: date) -> tuple[Any, str | None]:
    """(값, 오류메시지). 키가 없거나 적용기간 밖이면 (None, 메시지)."""
    try:
        return law.get(key, on=on), None
    except LawParamMissing as e:
        return None, str(e)


def policy_get(policy: dict, path: str, default: Any = None) -> Any:
    cur: Any = policy or {}
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur

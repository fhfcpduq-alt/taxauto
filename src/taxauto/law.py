"""세법 파라미터 로더.

세율·한도·기한처럼 바뀌는 값은 코드에 박지 않고 config/law/*.yaml 에 '데이터'로 둔다.
각 값은 적용기간(from/to)과 근거(source), 검증여부(verified)를 가진다.

  card_issue_credit.rate:
    - value: 0.013
      from: 2024-01-01
      to: 2026-12-31
      source: "부가가치세법 제46조 제1항"
      verified: true
      checked_on: 2026-10-02

세법이 바뀌면 YAML에 새 버전 한 줄만 추가하면 된다(코드 수정 없음).
verified: false 인 값을 쓰면 계산 노트에 '미검증 파라미터 사용' 경고가 남는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@dataclass
class LawValue:
    key: str
    value: Any
    source: str
    verified: bool
    valid_from: date | None
    valid_to: date | None


class LawParamMissing(KeyError):
    pass


class Law:
    def __init__(self, table: dict[str, list[dict]]):
        self._table = table
        self.used_unverified: set[str] = set()

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "Law":
        d = (config_dir or CONFIG_DIR) / "law"
        table: dict[str, list[dict]] = {}
        for f in sorted(d.glob("*.yaml")):
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            for k, versions in data.items():
                if k.startswith("_"):
                    continue
                if not isinstance(versions, list):
                    versions = [versions]
                table.setdefault(k, []).extend(versions)
        return cls(table)

    def lookup(self, key: str, on: date) -> LawValue:
        versions = self._table.get(key)
        if not versions:
            raise LawParamMissing(key)
        for v in versions:
            f = _d(v.get("from"))
            t = _d(v.get("to"))
            if (f is None or f <= on) and (t is None or on <= t):
                lv = LawValue(key, v.get("value"), v.get("source", ""), bool(v.get("verified", False)), f, t)
                if not lv.verified:
                    self.used_unverified.add(key)
                return lv
        raise LawParamMissing(f"{key} @ {on.isoformat()} (적용기간에 해당하는 값 없음 - 일몰/개정 확인 필요)")

    def get(self, key: str, on: date, default: Any = ...) -> Any:
        try:
            return self.lookup(key, on).value
        except LawParamMissing:
            if default is ...:
                raise
            return default


def _d(v: Any) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def load_policy(config_dir: Path | None = None) -> dict:
    """사무실 정책값(법이 아닌 내부 기준: 변동률 경고 기준, 고정자산 검토 금액 등)."""
    p = (config_dir or CONFIG_DIR) / "policy.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}

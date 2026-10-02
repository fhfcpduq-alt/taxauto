"""거래처 명부와 거래처·회차별 설정 로더.

  clients/clients.yaml                      거래처 명부 (예시: examples/clients.example.yaml)
  clients/{client_id}/filings/{period}.yaml 회차별 설정·수동조정 (예정고지세액, 수동 매출 등)
  clients/{client_id}/memory.yaml           공제판정 학습 메모리 (classify 섹터가 관리)

clients/ 는 .gitignore 대상(실데이터). 커밋 금지.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import Client, Line


@dataclass
class Adjustment:
    line: Line
    amount: int = 0
    tax: int = 0
    note: str = ""


@dataclass
class FilingSettings:
    filed_preliminary: bool | None = None
    preliminary_notice_tax: int = 0
    preliminary_unrefunded: int = 0
    skip: bool = False                     # 이번 회차 자동처리 제외(직접 처리 거래처 등)
    adjustments: list[Adjustment] = field(default_factory=list)
    previous_period_sales: int | None = None   # 전기 과세표준(변동률 검증용, 없으면 workspace에서 찾음)


def load_clients(clients_dir: Path) -> list[Client]:
    p = clients_dir / "clients.yaml"
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [Client.from_dict(c) for c in data.get("clients") or []]


def load_filing_settings(clients_dir: Path, client_id: str, period_code: str) -> FilingSettings:
    p = clients_dir / client_id / "filings" / f"{period_code}.yaml"
    if not p.exists():
        return FilingSettings()
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    adj = [
        Adjustment(line=Line[a["line"]], amount=int(a.get("amount", 0)), tax=int(a.get("tax", 0)), note=a.get("note", ""))
        for a in d.get("adjustments") or []
    ]
    return FilingSettings(
        filed_preliminary=d.get("filed_preliminary"),
        preliminary_notice_tax=int(d.get("preliminary_notice_tax") or 0),
        preliminary_unrefunded=int(d.get("preliminary_unrefunded") or 0),
        skip=bool(d.get("skip", False)),
        adjustments=adj,
        previous_period_sales=d.get("previous_period_sales"),
    )

"""거래처별 공제판정 학습 메모리 — clients/{client_id}/memory.yaml.

사람(또는 승인된 AI)의 결정을 '가맹점 사업자번호 또는 정규화된 가맹점명 → 결과'로 저장하고,
다음 실행에서 규칙보다 먼저 적용한다(decided_by=memory).

  version: 1
  entries:
    - key: "biz:1234567890"        # 또는 "name:스타벅스강남점", scope=once 면 "tx:<거래id>"
      scope: counterparty           # counterparty | once
      doc_type: 신용카드             # 빈 값이면 증빙 종류 무관
      classification: {category: 불공제, non_deductible_reason: 접대비및이와유사한비용, note: ...}
      by: human
      decided_on: 2026-10-02
      example: {counterparty_name: ..., item: ..., supply_amount: ...}
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ..models import Classification, DecidedBy, Transaction

MEMORY_FILE = "memory.yaml"

_CORP_TOKENS = re.compile(r"(\(주\)|㈜|\(유\)|\(사\)|\(재\)|주식회사|유한회사|유한책임회사|합자회사|합명회사)")


def normalize_name(name: str) -> str:
    """가맹점명 정규화: 법인격 표기·공백·특수문자 제거, 소문자."""
    s = _CORP_TOKENS.sub("", name or "")
    s = re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)
    return s.lower()


def keys_for(txn: Transaction) -> list[str]:
    """조회 우선순위: 거래 단위 → 사업자번호 → 가맹점명."""
    out = [f"tx:{txn.id}"]
    if txn.counterparty_biz_no:
        out.append(f"biz:{txn.counterparty_biz_no}")
    n = normalize_name(txn.counterparty_name)
    if n:
        out.append(f"name:{n}")
    return out


def _path(client_dir: Path) -> Path:
    return Path(client_dir) / MEMORY_FILE


def load_memory(client_dir: Path) -> list[dict]:
    p = _path(client_dir)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return list(data.get("entries") or [])


def save_memory(client_dir: Path, entries: list[dict]) -> None:
    p = _path(client_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump({"version": 1, "entries": entries}, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    tmp.replace(p)


def _cls_to_dict(c: Classification) -> dict[str, Any]:
    d: dict[str, Any] = {"category": c.category.value}
    if c.non_deductible_reason:
        d["non_deductible_reason"] = c.non_deductible_reason.value
    if c.exclusion_reason:
        d["exclusion_reason"] = c.exclusion_reason.value
    if c.note:
        d["note"] = c.note
    return d


def add_decision(
    client_dir: Path,
    txn: Transaction,
    classification: Classification,
    scope: str = "counterparty",
    by: str = "human",
    today: date | None = None,
) -> dict:
    """결정 1건을 메모리에 저장(같은 키·증빙종류는 덮어씀). 저장된 entry 반환.

    scope='counterparty': 사업자번호(없으면 정규화 가맹점명) 기준으로 이후 모든 거래에 적용
    scope='once'        : 이 거래(id)에만 적용
    """
    if scope not in ("counterparty", "once"):
        raise ValueError("scope 는 'counterparty' 또는 'once'")
    if scope == "once":
        key = f"tx:{txn.id}"
    elif txn.counterparty_biz_no:
        key = f"biz:{txn.counterparty_biz_no}"
    else:
        n = normalize_name(txn.counterparty_name)
        if not n:
            raise ValueError("가맹점 사업자번호·이름이 없어 거래처 단위로 저장할 수 없습니다(scope='once' 사용)")
        key = f"name:{n}"
    doc_type = "" if scope == "once" else txn.doc_type.value
    entry = {
        "key": key,
        "scope": scope,
        "doc_type": doc_type,
        "classification": _cls_to_dict(classification),
        "by": by,
        "decided_on": (today or date.today()).isoformat(),
        "example": {
            "counterparty_name": txn.counterparty_name,
            "item": txn.item,
            "supply_amount": txn.supply_amount,
        },
    }
    entries = [e for e in load_memory(client_dir) if not (e.get("key") == key and e.get("doc_type", "") == doc_type)]
    entries.append(entry)
    save_memory(client_dir, entries)
    return entry


class Memory:
    """조회용 인덱스."""

    def __init__(self, entries: list[dict]):
        self._idx: dict[tuple[str, str], dict] = {}
        for e in entries:
            self._idx[(str(e.get("key", "")), str(e.get("doc_type") or ""))] = e

    @classmethod
    def load(cls, client_dir: Path) -> "Memory":
        return cls(load_memory(client_dir))

    def __len__(self) -> int:
        return len(self._idx)

    def lookup(self, txn: Transaction) -> Classification | None:
        for key in keys_for(txn):
            e = self._idx.get((key, txn.doc_type.value)) or self._idx.get((key, ""))
            if e:
                c = Classification.from_dict(dict(e.get("classification") or {}))
                by = e.get("by", "human")
                note = c.note
                return replace(
                    c,
                    decided_by=DecidedBy.MEMORY,
                    rule_id=f"memory:{key}",
                    confidence=1.0,
                    needs_review=False,
                    note=f"과거 결정 적용({by}, {e.get('decided_on', '')})" + (f" - {note}" if note else ""),
                )
        return None

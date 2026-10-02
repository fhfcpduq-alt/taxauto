"""수집 커넥터 계약 + raw/ 매니페스트.

커넥터 = '어딘가에서 자료를 가져와 dest_dir(raw/)에 파일로 남기는 것'.
  - FolderConnector     : inbox/{period}/{client_id}/ 에 사람이 떨어뜨린 파일 (1단계 기본)
  - BulkSplitConnector  : inbox/{period}/_bulk/ 의 수임처 일괄 파일을 사업자번호로 분리
  - (향후) 브라우저 레시피·API 커넥터도 같은 프로토콜을 따른다.

raw/_manifest.json 에 파일별 sha256 을 기록해 변경 없는 파일은 다시 복사하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import Client, Filing

MANIFEST_NAME = "_manifest.json"


@runtime_checkable
class SourceConnector(Protocol):
    name: str

    def collect(self, client: Client, filing: Filing, dest_dir: Path) -> list[Path]:
        """자료를 dest_dir 에 저장하고, 이번에 확보된(신규·변경·동일) 파일 경로 목록을 돌려준다."""
        ...


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Manifest:
    """raw/_manifest.json  {"files": {raw파일명: {sha256, source, connector, collected_at, ...}}}"""

    def __init__(self, raw_dir: Path, data: dict | None = None):
        self.raw_dir = raw_dir
        self.data = data or {"files": {}}
        self.data.setdefault("files", {})

    @classmethod
    def load(cls, raw_dir: Path) -> "Manifest":
        p = raw_dir / MANIFEST_NAME
        if p.exists():
            try:
                return cls(raw_dir, json.loads(p.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass  # 깨진 매니페스트 → 새로 작성(전부 다시 복사)
        return cls(raw_dir)

    @property
    def files(self) -> dict[str, dict]:
        return self.data["files"]

    def save(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        p = self.raw_dir / MANIFEST_NAME
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    def unchanged(self, name: str, sha: str) -> bool:
        e = self.files.get(name)
        return bool(e and e.get("sha256") == sha and (self.raw_dir / name).exists())

    def record(self, name: str, sha: str, connector: str, **extra) -> None:
        self.files[name] = {
            "sha256": sha,
            "connector": connector,
            "collected_at": datetime.now().isoformat(timespec="seconds"),
            **extra,
        }


def copy_if_changed(src: Path, dest_dir: Path, name: str, manifest: Manifest, connector: str,
                    dry_run: bool = False, **extra) -> str:
    """변경 시에만 복사. 반환: 'new' | 'updated' | 'unchanged'."""
    sha = sha256_file(src)
    if manifest.unchanged(name, sha):
        return "unchanged"
    status = "updated" if name in manifest.files else "new"
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_dir / name)
        manifest.record(name, sha, connector, **extra)
    return status


def is_junk_file(p: Path) -> bool:
    """엑셀 임시파일·숨김파일·OS 부산물."""
    n = p.name
    return n.startswith(("~$", ".", "_")) or n in ("Thumbs.db", "desktop.ini") or n.endswith((".tmp", ".crdownload", ".part"))

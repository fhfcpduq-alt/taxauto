"""collect 단계: inbox → data/{period}/{client_id}/raw/

  inbox/{period}/{client_id}/*      거래처별로 내려받아 넣은 파일 → 그대로 복사(sha256 기록)
  inbox/{period}/_bulk/*            홈택스 세무대리인 '수임처 일괄 다운로드'처럼 여러 거래처가 섞인 파일
                                    → 거래처 사업자번호(Client.biz_no) 행만 골라 raw/bulk__{원본}.xlsx 로 저장

변경 없는 파일은 raw/_manifest.json 의 sha256 으로 판단해 건너뛴다.
inbox 에서 사라진 파일의 사본은 raw/_stale/ 로 옮겨 normalize 대상에서 빠지게 한다(삭제하지 않음).
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

from ..context import RunContext, StageResult
from ..models import Client, Filing, normalize_biz_no
from . import excel
from .base import Manifest, SourceConnector, copy_if_changed, is_junk_file, sha256_bytes, sha256_file

BULK_DIR = "_bulk"
BULK_PREFIX = "bulk__"
STALE_DIR = "_stale"

# 일괄 파일에서 '거래처 본인' 사업자번호가 들어갈 수 있는 필드(우선순위)
_BULK_OWN_FIELDS = ("client_biz_no",)
_BULK_PARTY_FIELDS = ("supplier_biz_no", "buyer_biz_no")


class FolderConnector:
    """inbox/{period}/{client_id}/ 의 파일을 raw/ 로 복사."""

    name = "folder"

    def __init__(self, period_inbox: Path, dry_run: bool = False):
        self.period_inbox = Path(period_inbox)
        self.dry_run = dry_run
        self.stats: dict[str, int] = {"new": 0, "updated": 0, "unchanged": 0}

    def source_dir(self, client: Client) -> Path:
        return self.period_inbox / client.id

    def collect(self, client: Client, filing: Filing, dest_dir: Path) -> list[Path]:
        src = self.source_dir(client)
        if not src.is_dir():
            return []
        manifest = Manifest.load(dest_dir)
        out: list[Path] = []
        for p in sorted(src.rglob("*")):
            if not p.is_file() or is_junk_file(p) or any(part.startswith((".", "_")) for part in p.relative_to(src).parts[:-1]):
                continue
            name = "__".join(p.relative_to(src).parts)   # 하위폴더는 이름으로 평탄화
            st = copy_if_changed(p, dest_dir, name, manifest, self.name, self.dry_run,
                                 source=_rel(p, self.period_inbox.parent), size=p.stat().st_size)
            self.stats[st] += 1
            out.append(dest_dir / name)
        if not self.dry_run:
            manifest.save()
        return out


class BulkSplitConnector:
    """inbox/{period}/_bulk/ 의 수임처 일괄 파일에서 이 거래처 행만 분리."""

    name = "bulk"

    def __init__(self, period_inbox: Path, config_dir: Path | None = None, dry_run: bool = False):
        self.period_inbox = Path(period_inbox)
        self.config_dir = config_dir
        self.dry_run = dry_run
        self.stats: dict[str, int] = {"new": 0, "updated": 0, "unchanged": 0, "rows": 0}
        self.issues: list[str] = []

    @property
    def bulk_dir(self) -> Path:
        return self.period_inbox / BULK_DIR

    def bulk_files(self) -> list[Path]:
        if not self.bulk_dir.is_dir():
            return []
        return sorted(p for p in self.bulk_dir.iterdir() if p.is_file() and not is_junk_file(p))

    def collect(self, client: Client, filing: Filing, dest_dir: Path) -> list[Path]:
        biz = normalize_biz_no(client.biz_no)
        files = self.bulk_files()
        if not files or not biz:
            return []
        manifest = Manifest.load(dest_dir)
        out: list[Path] = []
        for src in files:
            name = f"{BULK_PREFIX}{src.stem}.xlsx"
            src_sha = sha256_file(src)
            prev = manifest.files.get(name)
            if prev and prev.get("source_sha256") == src_sha and (dest_dir / name).exists():
                self.stats["unchanged"] += 1
                out.append(dest_dir / name)
                continue
            data, n_rows, note = split_bulk_file(src, biz, self.config_dir)
            if note:
                self.issues.append(f"{src.name}: {note}")
            if data is None or n_rows == 0:
                continue
            self.stats["rows"] += n_rows
            self.stats["updated" if prev else "new"] += 1
            if not self.dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)
                (dest_dir / name).write_bytes(data)
                manifest.record(name, sha256_bytes(data), self.name, source=_rel(src, self.period_inbox.parent),
                                source_sha256=src_sha, rows=n_rows)
            out.append(dest_dir / name)
        if not self.dry_run:
            manifest.save()
        return out


def split_bulk_file(path: Path, biz_no: str, config_dir: Path | None = None) -> tuple[bytes | None, int, str]:
    """일괄 파일 → 이 사업자번호 행만 남긴 xlsx(bytes), 행 수, 메모.

    판별 순서: (1) columns.yaml 로 종류 인식 → 수임처 사업자번호 열, 없으면 공급자/공급받는자 열
               (2) 인식 실패 → 헤더에 '사업자'·'등록번호'가 들어간 열, 그래도 없으면 행 전체에서 사업자번호 검색
    제목행·헤더행은 그대로 보존(normalize 에서 종류 판별에 씀).
    """
    from .normalize import detect_kind, load_columns  # 순환 import 회피

    biz = normalize_biz_no(biz_no)
    sheets, issues = excel.read_workbook(path)
    if issues and not sheets:
        return None, 0, "; ".join(i.message for i in issues)
    cfg = load_columns(config_dir)
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    total = 0
    notes: list[str] = []
    for sh in sheets:
        if not sh.rows:
            continue
        m = detect_kind(sh, path.name, cfg)
        if m is not None:
            hdr_idx = m.header.row_index
            cols = [m.header.columns[f] for f in _BULK_OWN_FIELDS if f in m.header.columns] or [
                m.header.columns[f] for f in _BULK_PARTY_FIELDS if f in m.header.columns
            ]
        else:
            hdr_idx, cols = _guess_biz_columns(sh.rows)
        if hdr_idx is None:
            notes.append(f"시트 '{sh.name}' 헤더 인식 실패 - 행 전체 검색")
            hdr_idx = -1
        keep = [r for r in sh.rows[hdr_idx + 1:] if _row_matches(r, biz, cols)]
        if not keep:
            continue
        ws = wb.create_sheet(_safe_title(sh.name))
        for r in sh.rows[: hdr_idx + 1]:
            ws.append([_cell(v) for v in r])
        for r in keep:
            ws.append([_cell(v) for v in r])
        total += len(keep)
    if total == 0:
        return None, 0, "; ".join(notes)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), total, "; ".join(notes)


def _guess_biz_columns(rows: list[list[Any]]) -> tuple[int | None, list[int]]:
    best: tuple[int, int, list[int]] | None = None
    for i, r in enumerate(rows[: excel.HEADER_SCAN_ROWS]):
        hs = [excel.norm_header(c) if isinstance(c, str) else "" for c in r]
        cols = [c for c, h in enumerate(hs) if ("사업자" in h or "등록번호" in h)]
        own = [c for c in cols if "수임" in hs[c]]
        cols = own or cols
        if cols and (best is None or len(cols) > best[0]):
            best = (len(cols), i, cols)
    return (best[1], best[2]) if best else (None, [])


def _row_matches(row: list[Any], biz: str, cols: list[int]) -> bool:
    cells = [row[c] for c in cols if c < len(row)] if cols else row
    return any(v is not None and normalize_biz_no(v) == biz for v in cells)


def _cell(v: Any) -> Any:
    if isinstance(v, str) and v.startswith("="):
        return "'" + v  # 수식 주입 방지
    return v


def _safe_title(name: str) -> str:
    for ch in "[]:*?/\\":
        name = name.replace(ch, "_")
    return (name or "Sheet")[:31]


def _rel(p: Path, base: Path) -> str:
    try:
        return str(p.relative_to(base))
    except ValueError:
        return str(p)


def move_stale(raw_dir: Path, keep: set[str], dry_run: bool = False) -> list[str]:
    """매니페스트에는 있으나 이번 수집에서 확보되지 않은 파일 → raw/_stale/ 로 이동."""
    manifest = Manifest.load(raw_dir)
    stale = [n for n in list(manifest.files) if n not in keep]
    if dry_run or not stale:
        return stale
    sdir = raw_dir / STALE_DIR
    for n in stale:
        p = raw_dir / n
        if p.exists():
            sdir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(sdir / n))
        manifest.files.pop(n, None)
    manifest.save()
    return stale


def default_connectors(ctx: RunContext) -> list[SourceConnector]:
    period_inbox = ctx.inbox_dir.parent   # inbox/{period}
    return [
        FolderConnector(period_inbox, dry_run=ctx.dry_run),
        BulkSplitConnector(period_inbox, ctx.config_dir, dry_run=ctx.dry_run),
    ]


def run(ctx: RunContext, connectors: list[SourceConnector] | None = None) -> StageResult:
    raw_dir = ctx.workspace.raw_dir
    connectors = connectors if connectors is not None else default_connectors(ctx)
    got: list[Path] = []
    notes: list[str] = []
    counts: dict[str, int] = {}
    for c in connectors:
        try:
            files = c.collect(ctx.client, ctx.filing, raw_dir)
        except Exception as e:  # 커넥터 하나 실패해도 나머지는 진행
            notes.append(f"{c.name} 실패: {type(e).__name__}: {e}")
            counts[f"{c.name}_error"] = 1
            continue
        got.extend(files)
        for k, v in (getattr(c, "stats", None) or {}).items():
            counts[f"{c.name}_{k}"] = v
        notes.extend(getattr(c, "issues", []) or [])
    ok = not any(k.endswith("_error") for k in counts)
    # 커넥터 실패 시에는 사본을 옮기지 않는다(일시 장애로 정상 파일이 빠지는 것 방지)
    stale = move_stale(raw_dir, {p.name for p in got}, ctx.dry_run) if ok else []
    counts["files"] = len(got)
    counts["stale"] = len(stale)
    if stale:
        notes.append(f"inbox 에서 사라진 파일 {len(stale)}개 → raw/{STALE_DIR}/ 로 이동: {', '.join(stale[:5])}")
    msg = f"파일 {len(got)}개 확보" if got else f"수집 파일 없음 ({ctx.inbox_dir} 확인)"
    if notes:
        msg += " | " + " | ".join(notes)
    ctx.log.info("[collect] %s %s", ctx.client.id, msg)
    return StageResult(ok=ok, message=msg, counts=counts)

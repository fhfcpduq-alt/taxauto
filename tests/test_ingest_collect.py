"""collect.py: 폴더 커넥터·매니페스트·stale 이동·수임처 일괄파일 분리."""

from __future__ import annotations

import json

from fixtures.make_fixtures import CP, build_bulk, build_inbox, make_ctx
from taxauto.ingest import collect, normalize
from taxauto.ingest.base import MANIFEST_NAME, SourceConnector
from taxauto.models import Source


def _manifest(ctx):
    return json.loads((ctx.workspace.raw_dir / MANIFEST_NAME).read_text(encoding="utf-8"))["files"]


def test_folder_copy_manifest_and_skip(tmp_path):
    files = build_inbox(tmp_path / "inbox", "2026-2P", clients=["C001"])["C001"]
    ctx = make_ctx(tmp_path, "C001")
    (ctx.inbox_dir / "~$임시.xlsx").write_bytes(b"x")       # 엑셀 임시파일은 무시
    r = collect.run(ctx)
    assert r.ok and r.counts["files"] == len(files) and r.counts["folder_new"] == len(files)
    m = _manifest(ctx)
    assert set(m) == {p.name for p in files}
    assert all(len(e["sha256"]) == 64 and e["connector"] == "folder" for e in m.values())

    r2 = collect.run(ctx)
    assert r2.counts["folder_unchanged"] == len(files) and r2.counts["folder_new"] == 0

    # 내용 변경 → updated
    target = files[0]
    target.write_bytes(target.read_bytes() + b"")  # 동일 내용 → 그대로
    src = ctx.inbox_dir / "신용카드매출자료.xlsx"
    import openpyxl
    wb = openpyxl.load_workbook(src)
    wb.active.append(["2026-09", 1, "1,100", "1,100", "0", "0"])
    wb.save(src)
    r3 = collect.run(ctx)
    assert r3.counts["folder_updated"] == 1


def test_stale_file_moved(tmp_path):
    build_inbox(tmp_path / "inbox", "2026-2P", clients=["C001"])
    ctx = make_ctx(tmp_path, "C001")
    collect.run(ctx)
    (ctx.inbox_dir / "판매대행_매출자료.xlsx").unlink()
    r = collect.run(ctx)
    assert r.counts["stale"] == 1
    assert (ctx.workspace.raw_dir / "_stale" / "판매대행_매출자료.xlsx").exists()
    assert not (ctx.workspace.raw_dir / "판매대행_매출자료.xlsx").exists()
    assert "판매대행_매출자료.xlsx" not in _manifest(ctx)
    normalize.run(ctx)
    assert not any(t.source == Source.PG_SALES for t in ctx.workspace.load_transactions())


def test_no_inbox_is_ok(tmp_path):
    ctx = make_ctx(tmp_path, "C002")
    r = collect.run(ctx)
    assert r.ok and r.counts["files"] == 0 and "없음" in r.message


def test_dry_run_writes_nothing(tmp_path):
    build_inbox(tmp_path / "inbox", "2026-2P", clients=["C002"])
    ctx = make_ctx(tmp_path, "C002", dry_run=True)
    r = collect.run(ctx)
    assert r.counts["files"] > 0
    assert not ctx.workspace.raw_dir.exists()


def test_bulk_split_and_dedupe(tmp_path):
    build_inbox(tmp_path / "inbox", "2026-2P")
    build_bulk(tmp_path / "inbox", "2026-2P")
    for cid in ("C001", "C002"):
        ctx = make_ctx(tmp_path, cid)
        r = collect.run(ctx)
        assert r.ok and r.counts["bulk_new"] == 1
        bulk = ctx.workspace.raw_dir / "bulk__수임처_전자세금계산서_매입.xlsx"
        assert bulk.exists()
        res = normalize.parse_file(bulk, ctx.client, ctx.filing)
        assert res.kinds == ["etax_purchase"]
        assert res.transactions and all(t.direction.value == "매입" for t in res.transactions)
        assert CP["타거래처"][0] not in {t.counterparty_biz_no for t in res.transactions}
        n_bulk = len(res.transactions)
        # 개별 파일과 같은 승인번호 → 중복 제거
        normalize.run(ctx)
        issues = ctx.workspace.load_parse_issues()
        assert any(f"중복 거래 {n_bulk}건" in i["message"] for i in issues)
        # 원본 일괄 파일 변경 없으면 다시 분리하지 않음
        r2 = collect.run(ctx)
        assert r2.counts["bulk_unchanged"] == 1


def test_split_bulk_without_known_header(tmp_path):
    import openpyxl

    p = tmp_path / "bulk.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["알 수 없는 양식"])
    ws.append(["수임처 사업자번호", "값"])
    ws.append(["123-45-67890", 1])
    ws.append(["220-81-23456", 2])
    ws.append(["123-45-67890", 3])
    wb.save(p)
    data, n, _ = collect.split_bulk_file(p, "1234567890")
    assert n == 2 and data


def test_connectors_follow_protocol(tmp_path):
    ctx = make_ctx(tmp_path, "C001")
    for c in collect.default_connectors(ctx):
        assert isinstance(c, SourceConnector)


def test_failing_connector_does_not_stop_others(tmp_path):
    build_inbox(tmp_path / "inbox", "2026-2P", clients=["C001"])
    ctx = make_ctx(tmp_path, "C001")
    collect.run(ctx)

    class Broken:
        name = "broken"

        def collect(self, client, filing, dest_dir):
            raise RuntimeError("network down")

    r = collect.run(ctx, connectors=[Broken()])
    assert not r.ok and "broken 실패" in r.message
    assert r.counts["stale"] == 0  # 실패 시 기존 사본을 옮기지 않음
    assert (ctx.workspace.raw_dir / "판매대행_매출자료.xlsx").exists()

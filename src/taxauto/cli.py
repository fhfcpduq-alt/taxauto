"""taxauto 명령줄.

  taxauto init
  taxauto run --period 2026-2P [--client C001 ...] [--stages ...] [--from-stage classify] [--only-failed]
  taxauto night                      # 오늘 날짜로 회차 결정 → 전체 실행 → 브리핑. 종료코드 0/1
  taxauto status [--period]
  taxauto review list [--period] [--client] [--open]
  taxauto review resolve <id> --status 해결|확인후유지 --note "..." [--remember]
  taxauto doctor

기본 작업 폴더: --home > 환경변수 TAXAUTO_HOME > 현재 폴더.
전자신고 제출 명령은 없다(사람이 승인·제출).
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import unicodedata
from datetime import date
from pathlib import Path

from . import __version__
from .models import ReviewStatus, Severity, TaxPeriod
from .period import current_period
from .pipeline import (
    STAGE_ORDER,
    LockBusy,
    Paths,
    build_summary,
    run_period,
)

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
API_KEY_ENVS = ["ANTHROPIC_API_KEY", "DATA_GO_KR_API_KEY"]
STATUS_KO = {"ok": "완료", "failed": "실패", "not_implemented": "미구현", "skipped": "제외", "pending": "대기"}


# ---------------------------------------------------------------------------
# 출력 도우미
# ---------------------------------------------------------------------------


def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def table(headers: list[str], rows: list[list], right: set[int] | None = None) -> str:
    right = right or set()
    cells = [[str(h) for h in headers]] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(_w(r[i]) for r in cells) for i in range(len(headers))]

    def fmt(r: list[str]) -> str:
        out = []
        for i, c in enumerate(r):
            pad = " " * (widths[i] - _w(c))
            out.append(pad + c if i in right else c + pad)
        return "  ".join(out).rstrip()

    sep = "  ".join("-" * w for w in widths)
    return "\n".join([fmt(cells[0]), sep] + [fmt(r) for r in cells[1:]])


def _money(v) -> str:
    if v is None:
        return "-"
    return f"{v:,}" if v >= 0 else f"환급 {-v:,}"


def _dday(v) -> str:
    if v is None:
        return "-"
    return "D-day" if v == 0 else (f"D-{v}" if v > 0 else f"D+{-v}")


def _setup_logging(paths: Paths | None, verbose: bool, logfile: Path | None = None) -> logging.Logger:
    lg = logging.getLogger("taxauto")
    lg.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(lg.handlers):
        if getattr(h, "_taxauto", False):
            lg.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG if verbose else logging.WARNING)
    sh._taxauto = True  # type: ignore[attr-defined]
    lg.addHandler(sh)
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG if verbose else logging.INFO)
        fh._taxauto = True  # type: ignore[attr-defined]
        lg.addHandler(fh)
    return lg


def _period_arg(v: str | None) -> str:
    return TaxPeriod.parse(v).code if v else current_period(date.today()).code


def _print_run_result(summary: dict) -> None:
    rows = summary.get("clients") or []
    ran = set((summary.get("last_run") or {}).get("ran") or [])
    print(f"{summary['period_label']} — 대상 {len(rows)}곳, 이번 실행 {len(ran)}곳")
    print(_status_table(summary))
    t = summary["totals"]
    print(f"\n완료 {t['ok']} · 실패 {t['failed']} · 미구현 {t['not_implemented']} · 제외 {t['skipped']} · "
          f"신고가능 {t['ready_to_file']} · 차단 {t['blocker_open']}건 · 경고 {t['warn_open']}건")


def _status_table(summary: dict) -> str:
    rows = []
    for r in summary.get("clients") or []:
        wh = r.get("wehago_status") or {}
        st = STATUS_KO.get(r["status"], r["status"])
        if r.get("failed_stage"):
            st += f"({r['failed_stage']})"
        rows.append([r["client_id"], r["name"], st, _money(r.get("final_tax")), r["blocker_open"], r["warn_open"],
                     r.get("due_date") or "-", _dday(r.get("d_day")),
                     f"{wh.get('step')}:{wh.get('status')}" if wh else "-"])
    if not rows:
        return "(대상 거래처 없음)"
    return table(["코드", "거래처", "상태", "납부(환급)", "차단", "경고", "기한", "D-day", "위하고"], rows, right={3, 4, 5})


def _failed_any(summary: dict) -> bool:
    t = summary.get("totals") or {}
    return bool(t.get("failed") or t.get("not_implemented"))


# ---------------------------------------------------------------------------
# 명령
# ---------------------------------------------------------------------------


def cmd_init(args, paths: Paths) -> int:
    for d in (paths.clients_dir, paths.inbox_root, paths.data_dir, paths.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
        print(f"폴더 준비: {d}")
    roster = paths.clients_dir / "clients.yaml"
    ex = EXAMPLES_DIR / "clients.example.yaml"
    if not roster.exists() and ex.exists():
        shutil.copy2(ex, roster)
        print(f"예시 명부 복사: {roster}  ← 실제 거래처로 바꿔 쓰세요")
    for f in sorted((EXAMPLES_DIR / "filings").glob("*.yaml")) if (EXAMPLES_DIR / "filings").exists() else []:
        # 파일명: {period}.{client_id}.yaml → clients/{client_id}/filings/{period}.yaml
        parts = f.name.split(".")
        if len(parts) == 3:
            dest = paths.clients_dir / parts[1] / "filings" / f"{parts[0]}.yaml"
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                print(f"예시 회차설정 복사: {dest}")
    period = current_period(date.today()).code
    from .registry import load_clients

    for c in load_clients(paths.clients_dir):
        (paths.inbox_root / period / c.id).mkdir(parents=True, exist_ok=True)
    print(f"inbox/{period}/{{거래처코드}}/ 폴더를 만들었습니다. 다음: taxauto doctor")
    return 0


def cmd_run(args, paths: Paths) -> int:
    lg = _setup_logging(paths, args.verbose)
    try:
        summary = run_period(_period_arg(args.period), client_ids=args.client, stages=args.stages,
                             only_failed=args.only_failed, from_stage=args.from_stage, base_dir=paths.base,
                             logger=lg, command="run")
    except LockBusy as e:
        print(f"[중단] {e}", file=sys.stderr)
        return 1
    _print_run_result(summary)
    return 1 if _failed_any(summary) else 0


def cmd_night(args, paths: Paths) -> int:
    today = date.fromisoformat(args.date) if args.date else date.today()
    period = TaxPeriod.parse(args.period).code if args.period else current_period(today).code
    logfile = paths.logs_dir / f"{today.isoformat()}.log"
    lg = _setup_logging(paths, args.verbose, logfile)
    lg.info("야간 실행 시작: %s (기준일 %s)", period, today)
    try:
        summary = run_period(period, base_dir=paths.base, today=today, logger=lg, command="night")
    except LockBusy as e:
        lg.error("중단: %s", e)
        print(f"[중단] {e}", file=sys.stderr)
        return 1
    except Exception:
        lg.exception("야간 실행 오류")
        return 1
    t = summary["totals"]
    lg.info("야간 실행 끝: 대상 %s · 완료 %s · 실패 %s · 미구현 %s · 차단 %s건",
            t["clients"], t["ok"], t["failed"], t["not_implemented"], t["blocker_open"])
    _print_run_result(summary)
    print(f"\n브리핑: {paths.period_dir(period) / '_briefing.md'}")
    print(f"대시보드: {paths.period_dir(period) / '_dashboard.html'}")
    print(f"로그: {logfile}")
    return 1 if _failed_any(summary) else 0


def cmd_status(args, paths: Paths) -> int:
    period = _period_arg(args.period)
    s = build_summary(period, paths=paths)
    print(f"{s['period_label']} · 기한 {s.get('due_date') or '-'} ({_dday(s.get('d_day'))})")
    print(_status_table(s))
    if s.get("not_required"):
        print(f"\n이번 회차 신고 대상 아님: {', '.join(s['not_required'])}")
    return 0


def cmd_review_list(args, paths: Paths) -> int:
    from .ops import client_ids_in_period
    from .pipeline import sort_review

    period = _period_arg(args.period)
    cids = args.client or client_ids_in_period(paths, period)
    rows = []
    for cid in cids:
        items = [i.to_dict() for i in paths.workspace(period, cid).load_review()]
        for it in sort_review(items):
            if args.open and it.get("status") != ReviewStatus.OPEN.value:
                continue
            rows.append([it["id"], cid, it["severity"], it["status"], (it.get("title") or "")[:50],
                         f"{int(it.get('tax_impact') or 0):,}" if it.get("tax_impact") else "", len(it.get("tx_ids") or [])])
    sev = {Severity.BLOCKER.value: 0, Severity.WARN.value: 1, Severity.INFO.value: 2}
    rows.sort(key=lambda r: (r[3] != ReviewStatus.OPEN.value, sev.get(r[2], 9)))
    if not rows:
        print("검토항목 없음.")
        return 0
    print(table(["ID", "거래처", "구분", "상태", "내용", "세액영향", "거래"], rows, right={5, 6}))
    return 0


def cmd_review_resolve(args, paths: Paths) -> int:
    from .ops import OpError, resolve_review_item

    try:
        r = resolve_review_item(paths, _period_arg(args.period), args.id, args.status, args.note,
                                client_id=args.client, resolved_by=args.by, remember=args.remember)
    except OpError as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1
    it = r["item"]
    print(f"{r['client_id']} {it['id']} → {it['status']} ({it['resolved_by']})")
    if args.remember:
        print(f"거래처 메모리에 {r['remembered']}건 저장 — 다음 실행부터 같은 거래처는 자동 처리됩니다.")
    return 0


def cmd_doctor(args, paths: Paths) -> int:
    import yaml

    from .law import load_policy
    from .registry import load_clients

    today = date.today()
    errs = warns = 0

    def ok(msg):
        print(f"[OK]   {msg}")

    def warn(msg):
        nonlocal warns
        warns += 1
        print(f"[경고] {msg}")

    def err(msg):
        nonlocal errs
        errs += 1
        print(f"[오류] {msg}")

    print(f"taxauto {__version__} · Python {sys.version.split()[0]} · 작업폴더 {paths.base}")
    print(f"설정 폴더 {paths.config_dir}")

    # 1) 명부
    roster = paths.clients_dir / "clients.yaml"
    clients = []
    if not roster.exists():
        err(f"거래처 명부 없음: {roster} (taxauto init)")
    else:
        try:
            clients = load_clients(paths.clients_dir)
            ids = [c.id for c in clients]
            dup = sorted({x for x in ids if ids.count(x) > 1})
            bad_biz = [c.id for c in clients if len(c.biz_no) != 10]
            if dup:
                err(f"거래처 코드 중복: {', '.join(dup)}")
            if bad_biz:
                warn(f"사업자번호 10자리 아님: {', '.join(bad_biz)}")
            ok(f"거래처 명부 {len(clients)}곳 (활성 {sum(c.active for c in clients)})")
        except Exception as e:
            err(f"거래처 명부 파싱 실패: {type(e).__name__}: {e}")

    # 2) 정책·법 파라미터
    try:
        policy = load_policy(paths.config_dir)
        ok("policy.yaml 읽기")
    except Exception as e:
        policy = {}
        err(f"policy.yaml 파싱 실패: {e}")
    try:
        unverified, total = _unverified_law(paths.config_dir, today)
        if unverified:
            warn(f"오늘 적용되는 세법 파라미터 {total}개 중 미검증 {len(unverified)}개 (config/law, verified: false)")
        else:
            ok(f"세법 파라미터 {total}개 모두 검증됨")
    except Exception as e:
        err(f"config/law 파싱 실패: {e}")

    # 3) 공휴일
    try:
        hp = paths.config_dir / "holidays.yaml"
        data = (yaml.safe_load(hp.read_text(encoding="utf-8")) or {}).get("holidays") or {} if hp.exists() else {}
        years = {today.year} | ({today.year + 1} if today.month >= 11 else set())
        for y in sorted(years):
            items = data.get(y) if y in data else data.get(str(y))
            if items is None:
                err(f"공휴일 목록에 {y}년 없음 (config/holidays.yaml) — 신고기한이 틀릴 수 있음")
            elif not items:
                warn(f"공휴일 목록 {y}년이 비어 있음 — 기한이 공휴일과 겹치면 하루 늦게 잡히지 않음")
            else:
                ok(f"공휴일 {y}년 {len(items)}일")
    except Exception as e:
        err(f"holidays.yaml 파싱 실패: {e}")

    # 4) inbox
    period = current_period(today).code
    pin = paths.inbox_root / period
    if not paths.inbox_root.exists():
        err(f"inbox 폴더 없음: {paths.inbox_root} (taxauto init)")
    elif not pin.exists():
        warn(f"이번 회차 inbox 폴더 없음: {pin}")
    else:
        missing = [c.id for c in clients if c.active and not (pin / c.id).exists()]
        if missing:
            warn(f"inbox/{period}/ 에 거래처 폴더 없음 {len(missing)}곳: {', '.join(missing[:10])}")
        else:
            ok(f"inbox/{period}/ 준비")

    # 5) data, lock
    lock = paths.data_dir / ".lock"
    if lock.exists():
        warn(f"실행 lock 이 있음: {lock} (실행 중이 아니면 {int((policy.get('run') or {}).get('lock_stale_hours') or 6)}시간 뒤 자동 해제)")
    try:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        probe = paths.data_dir / ".doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        ok(f"data 폴더 쓰기 가능: {paths.data_dir}")
    except OSError as e:
        err(f"data 폴더 쓰기 불가: {e}")

    # 6) API 키 (값은 절대 출력하지 않음)
    for k in API_KEY_ENVS:
        if os.environ.get(k):
            ok(f"환경변수 {k}: 설정됨")
        else:
            needed = (k == "ANTHROPIC_API_KEY" and (policy.get("llm") or {}).get("enabled")) or (
                k == "DATA_GO_KR_API_KEY" and (policy.get("nts_status") or {}).get("enabled"))
            (warn if needed else ok)(f"환경변수 {k}: 없음" + (" — 정책에서 기능이 켜져 있음" if needed else " (해당 기능 꺼짐)"))

    # 7) 선택 패키지
    try:
        import mcp  # noqa: F401

        ok("mcp 패키지 설치됨 (에이전트 도구 서버 사용 가능)")
    except ImportError:
        warn("mcp 패키지 없음 — pip install -e .[agent]")

    print(f"\n결과: 오류 {errs} · 경고 {warns}")
    return 1 if errs else 0


def _unverified_law(config_dir: Path, today: date) -> tuple[list[str], int]:
    from .law import Law, LawParamMissing

    law = Law.load(config_dir)
    keys = list(law._table.keys())  # 읽기 전용 점검
    unverified = []
    total = 0
    for k in keys:
        try:
            lv = law.lookup(k, today)
        except LawParamMissing:
            continue
        total += 1
        if not lv.verified:
            unverified.append(k)
    return unverified, total


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="taxauto", description="부가가치세 신고 야간 자동화 (제출은 사람이)")
    p.add_argument("--home", help="작업 폴더(기본: TAXAUTO_HOME 또는 현재 폴더)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"taxauto {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="clients/ inbox/ data/ logs/ 만들기 + 예시 복사")

    r = sub.add_parser("run", help="회차 실행")
    r.add_argument("--period", required=True, help="예: 2026-2P, 2026-2F")
    r.add_argument("--client", nargs="+", help="거래처 코드(여러 개 가능)")
    r.add_argument("--stages", nargs="+", choices=STAGE_ORDER)
    r.add_argument("--from-stage", choices=STAGE_ORDER)
    r.add_argument("--only-failed", action="store_true", help="지난 실행이 완료(ok)가 아닌 거래처만")

    n = sub.add_parser("night", help="야간 일괄 실행(오늘 날짜로 회차 결정)")
    n.add_argument("--period", help="회차 강제 지정")
    n.add_argument("--date", help="기준일(YYYY-MM-DD, 점검용)")

    s = sub.add_parser("status", help="회차 현황표")
    s.add_argument("--period")

    rv = sub.add_parser("review", help="검토항목")
    rsub = rv.add_subparsers(dest="review_cmd", required=True)
    rl = rsub.add_parser("list")
    rl.add_argument("--period")
    rl.add_argument("--client", nargs="+")
    rl.add_argument("--open", action="store_true", help="미해결만")
    rr = rsub.add_parser("resolve")
    rr.add_argument("id")
    rr.add_argument("--status", required=True, choices=[ReviewStatus.RESOLVED.value, ReviewStatus.ACCEPTED.value, ReviewStatus.OPEN.value])
    rr.add_argument("--note", required=True)
    rr.add_argument("--remember", action="store_true", help="이 결정을 거래처 메모리에 저장(다음부터 자동)")
    rr.add_argument("--period")
    rr.add_argument("--client")
    rr.add_argument("--by", help="처리자(기본: TAXAUTO_USER / 로그인 사용자)")

    sub.add_parser("doctor", help="설정 점검")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # Windows 콘솔/리다이렉트에서 한글 깨짐·인코딩 오류 방지
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    paths = Paths.from_base(args.home)
    handlers = {"init": cmd_init, "run": cmd_run, "night": cmd_night, "status": cmd_status, "doctor": cmd_doctor}
    if args.cmd == "review":
        fn = cmd_review_list if args.review_cmd == "list" else cmd_review_resolve
    else:
        fn = handlers[args.cmd]
    try:
        return fn(args, paths)
    except ValueError as e:  # 잘못된 회차 코드 등
        print(f"[오류] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

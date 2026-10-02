"""파이프라인 실행기 (오케스트레이션).

  collect → normalize → enrich → classify → compute → validate → report

- 단계 모듈은 '모듈명으로 지연 import' 한다. 아직 없는 단계는 not_implemented 로 기록하고
  그 거래처는 거기서 멈춘다(다른 거래처는 계속).
- 한 단계가 예외/ok=False 면 그 거래처만 중단. 다른 거래처는 계속.
- 단계별 시작/종료시각·결과·예외 traceback(마스킹) → data/{period}/{client_id}/state.json
- 실행 후 data/{period}/_summary.json (스키마: docs/RUNBOOK.md 부록) 작성
- 동시 실행 방지: data/.lock (오래된 lock 은 자동 해제)

전자신고 '제출'은 여기서 하지 않는다(사람 승인 영역).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import socket
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from .context import RunContext, StageResult
from .law import CONFIG_DIR, Law, load_policy
from .models import Client, Filing, ReviewStatus, Severity, TaxPeriod
from .period import build_filing, filing_required, load_holidays
from .redact import mask_biz_no, redact
from .registry import FilingSettings, load_clients, load_filing_settings
from .workspace import Workspace, _dump, _load

log = logging.getLogger("taxauto")

STAGE_ORDER: list[str] = ["collect", "normalize", "enrich", "classify", "compute", "validate", "report"]

STAGE_MODULES: dict[str, str] = {
    "collect": "taxauto.ingest.collect",
    "normalize": "taxauto.ingest.normalize",
    "enrich": "taxauto.ingest.enrich",
    "classify": "taxauto.classify.stage",
    "compute": "taxauto.compute.stage",
    "validate": "taxauto.validate.stage",
    "report": "taxauto.report.stage",
}

# 단계 상태값 (state.json / _summary.json 공통, 외부 계약)
ST_OK = "ok"
ST_SKIPPED = "skipped"            # 단계가 스스로 건너뜀(자료 없음 등) / 거래처 자동처리 제외
ST_FAILED = "failed"
ST_NOT_IMPLEMENTED = "not_implemented"
ST_PENDING = "pending"            # 아직 실행 안 됨(앞 단계 실패 등)

SUMMARY_SCHEMA = "taxauto.summary/v1"
STATE_SCHEMA = "taxauto.state/v1"

StageFn = Callable[[RunContext], StageResult]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------


def default_base_dir() -> Path:
    return Path(os.environ.get("TAXAUTO_HOME") or Path.cwd()).resolve()


@dataclass
class Paths:
    base: Path
    config_dir: Path
    clients_dir: Path
    data_dir: Path
    inbox_root: Path
    logs_dir: Path

    @classmethod
    def from_base(cls, base_dir: Path | str | None = None) -> "Paths":
        base = Path(base_dir).resolve() if base_dir else default_base_dir()
        # 사무실 PC 에서 config 를 따로 둘 수 있게: base/config/law 가 있으면 그걸 쓴다
        cfg = base / "config" if (base / "config" / "law").is_dir() else CONFIG_DIR
        run = (load_policy(cfg).get("run") or {}) if (cfg / "policy.yaml").exists() else {}
        return cls(
            base=base,
            config_dir=cfg,
            clients_dir=base / "clients",
            data_dir=base / str(run.get("workspace") or "data"),
            inbox_root=base / str(run.get("inbox") or "inbox"),
            logs_dir=base / "logs",
        )

    def period_dir(self, period_code: str) -> Path:
        return self.data_dir / period_code

    def workspace(self, period_code: str, client_id: str) -> Workspace:
        return Workspace(self.period_dir(period_code) / client_id)


# ---------------------------------------------------------------------------
# 단계 해석 (지연 import)
# ---------------------------------------------------------------------------


class StageNotImplemented(Exception):
    pass


def resolve_stage(name: str, overrides: dict[str, StageFn] | None = None) -> StageFn:
    """단계 이름 → run 함수. 모듈이 없으면 StageNotImplemented.

    모듈 '안'에서 다른 import 가 실패한 경우는 구현 결함이므로 그대로 예외(→ failed).
    """
    if overrides and name in overrides:
        return overrides[name]
    if name not in STAGE_MODULES:
        raise ValueError(f"알 수 없는 단계: {name}")
    modname = STAGE_MODULES[name]
    try:
        mod = importlib.import_module(modname)
    except ModuleNotFoundError as e:
        missing = e.name or ""
        if missing and (modname == missing or modname.startswith(missing + ".")):
            raise StageNotImplemented(f"{modname} 모듈 없음") from None
        raise
    fn = getattr(mod, "run", None)
    if not callable(fn):
        raise StageNotImplemented(f"{modname}.run 없음")
    return fn


def normalize_stages(stages: Iterable[str] | None, from_stage: str | None = None) -> list[str]:
    names = list(stages) if stages else list(STAGE_ORDER)
    for s in names + ([from_stage] if from_stage else []):
        if s not in STAGE_ORDER:
            raise ValueError(f"알 수 없는 단계: {s} (가능: {', '.join(STAGE_ORDER)})")
    names = sorted(set(names), key=STAGE_ORDER.index)
    if from_stage:
        names = [s for s in names if STAGE_ORDER.index(s) >= STAGE_ORDER.index(from_stage)]
    return names


# ---------------------------------------------------------------------------
# 동시 실행 방지 lock
# ---------------------------------------------------------------------------


class LockBusy(RuntimeError):
    def __init__(self, info: dict):
        self.info = info
        super().__init__(f"다른 실행이 진행 중입니다: {info.get('command', '')} (시작 {info.get('started_at', '?')}, pid {info.get('pid', '?')})")


def _pid_alive(pid: int) -> bool | None:
    """POSIX 에서만 확인. Windows 의 os.kill(pid, 0) 은 프로세스를 종료시키므로 쓰지 않는다."""
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


class RunLock:
    """data/.lock. with 문으로 사용. stale_after 가 지나거나(같은 PC에서) 프로세스가 죽었으면 자동 해제."""

    def __init__(self, data_dir: Path, command: str = "", stale_after: timedelta = timedelta(hours=6)):
        self.path = Path(data_dir) / ".lock"
        self.command = command
        self.stale_after = stale_after
        self.token = f"{socket.gethostname()}:{os.getpid()}:{time.time_ns()}"
        self.acquired = False

    def read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def is_stale(self, info: dict) -> bool:
        try:
            started = datetime.fromisoformat(info["started_at"])
            age = datetime.now().astimezone() - started
        except (KeyError, ValueError, TypeError):
            try:
                age = timedelta(seconds=time.time() - self.path.stat().st_mtime)
            except OSError:
                return True
        if age > self.stale_after:
            return True
        if info.get("host") == socket.gethostname() and isinstance(info.get("pid"), int):
            if _pid_alive(info["pid"]) is False:
                return True
        return False

    def acquire(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                info = self.read()
                if self.is_stale(info):
                    log.warning("오래된 lock 자동 해제: %s", info)
                    self.path.unlink(missing_ok=True)
                    continue
                raise LockBusy(info)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    {"pid": os.getpid(), "host": socket.gethostname(), "started_at": now_iso(),
                     "command": self.command, "token": self.token},
                    f, ensure_ascii=False,
                )
            self.acquired = True
            return self
        raise LockBusy(self.read())

    def release(self) -> None:
        if self.acquired and self.read().get("token") == self.token:
            self.path.unlink(missing_ok=True)
        self.acquired = False

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()


def lock_for(paths: Paths, command: str) -> RunLock:
    hours = float(((load_policy(paths.config_dir).get("run") or {}).get("lock_stale_hours")) or 6)
    return RunLock(paths.data_dir, command=command, stale_after=timedelta(hours=hours))


# ---------------------------------------------------------------------------
# 컨텍스트
# ---------------------------------------------------------------------------


def make_filing(client: Client, period: TaxPeriod, settings: FilingSettings, law: Law, holidays: set[date]) -> Filing:
    return build_filing(
        client, period, law, holidays,
        filed_preliminary=settings.filed_preliminary,
        preliminary_notice_tax=int(settings.preliminary_notice_tax or 0),
        preliminary_unrefunded=int(settings.preliminary_unrefunded or 0),
    )


def make_context(
    client: Client,
    period: TaxPeriod | str,
    base_dir: Path | str | None = None,
    *,
    settings: FilingSettings | None = None,
    law: Law | None = None,
    policy: dict | None = None,
    holidays: set[date] | None = None,
    today: date | None = None,
    logger: logging.Logger | None = None,
    dry_run: bool = False,
    paths: Paths | None = None,
) -> RunContext:
    paths = paths or Paths.from_base(base_dir)
    tp = TaxPeriod.parse(period) if isinstance(period, str) else period
    law = law or Law.load(paths.config_dir)
    policy = policy if policy is not None else load_policy(paths.config_dir)
    holidays = holidays if holidays is not None else load_holidays(paths.config_dir)
    settings = settings or load_filing_settings(paths.clients_dir, client.id, tp.code)
    filing = make_filing(client, tp, settings, law, holidays)
    return RunContext(
        client=client,
        filing=filing,
        workspace=paths.workspace(tp.code, client.id),
        law=law,
        policy=policy,
        inbox_dir=paths.inbox_root / tp.code / client.id,
        config_dir=paths.config_dir,
        clients_dir=paths.clients_dir,
        today=today or date.today(),
        log=logger or log,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# 거래처 1건 실행
# ---------------------------------------------------------------------------


def _new_state(ws: Workspace, client: Client, period_code: str) -> dict:
    st = ws.load_state() or {}
    st.setdefault("stages", {})
    st["schema"] = STATE_SCHEMA
    st["client_id"] = client.id
    st["client_name"] = client.name
    st["period"] = period_code
    return st


def run_client(
    ctx: RunContext,
    stages: Iterable[str] | None = None,
    from_stage: str | None = None,
    stage_fns: dict[str, StageFn] | None = None,
) -> dict:
    """한 거래처의 단계들을 순서대로 실행. 반환: state dict (state.json 과 동일)."""
    plan = normalize_stages(stages, from_stage)
    ws = ctx.workspace
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.save_json("filing.json", ctx.filing.to_dict())
    state = _new_state(ws, ctx.client, ctx.period_code)
    run_info = {"started_at": now_iso(), "finished_at": None, "stages": plan, "from_stage": from_stage}
    state["last_run"] = run_info
    overall, failed_stage, error = ST_OK, None, None

    for i, name in enumerate(plan):
        rec: dict[str, Any] = {"status": ST_PENDING, "started_at": now_iso(), "finished_at": None,
                               "duration_sec": 0.0, "message": "", "counts": {}, "error": None, "traceback": None}
        t0 = time.perf_counter()
        try:
            fn = resolve_stage(name, stage_fns)
            res = fn(ctx)
            if not isinstance(res, StageResult):
                raise TypeError(f"{name}.run() 이 StageResult 가 아닌 {type(res).__name__} 을 반환")
            rec["message"] = redact(res.message)
            rec["counts"] = dict(res.counts or {})
            if not res.ok:
                rec["status"] = ST_FAILED
                rec["error"] = redact(res.message) or "단계가 ok=False 반환"
            else:
                rec["status"] = ST_SKIPPED if res.skipped else ST_OK
        except StageNotImplemented as e:
            rec["status"] = ST_NOT_IMPLEMENTED
            rec["message"] = str(e)
        except Exception as e:  # 단계 결함은 기록하고 이 거래처만 중단
            rec["status"] = ST_FAILED
            rec["error"] = redact(f"{type(e).__name__}: {e}")
            rec["traceback"] = redact(traceback.format_exc())
        rec["finished_at"] = now_iso()
        rec["duration_sec"] = round(time.perf_counter() - t0, 3)
        state["stages"][name] = rec
        ctx.log.info("[%s %s] %s → %s %s", ctx.period_code, ctx.client.id, name, rec["status"], rec["error"] or rec["message"])

        if rec["status"] in (ST_FAILED, ST_NOT_IMPLEMENTED):
            overall, failed_stage = rec["status"], name
            error = rec["error"] or rec["message"]
            for rest in plan[i + 1:]:
                state["stages"][rest] = {"status": ST_PENDING, "message": f"{name} 단계 {rec['status']} 로 실행 안 함",
                                         "started_at": None, "finished_at": None, "duration_sec": 0.0,
                                         "counts": {}, "error": None, "traceback": None}
            break
        ws.save_state(state)  # 단계마다 저장(중간에 PC가 꺼져도 어디까지 됐는지 남게)

    run_info["finished_at"] = now_iso()
    state["status"] = overall
    state["failed_stage"] = failed_stage
    state["error"] = error
    state["unverified_law_params"] = sorted(getattr(ctx.law, "used_unverified", set()) or [])
    ws.save_state(state)
    return state


def _mark_client(ws: Workspace, client: Client, period_code: str, status: str, message: str = "",
                 failed_stage: str | None = None) -> dict:
    st = _new_state(ws, client, period_code)
    st["status"] = status
    st["failed_stage"] = failed_stage
    st["error"] = message if status == ST_FAILED else None
    st["message"] = message
    st["last_run"] = {"started_at": now_iso(), "finished_at": now_iso(), "stages": [], "from_stage": None}
    ws.save_state(st)
    return st


# ---------------------------------------------------------------------------
# 회차 전체 실행
# ---------------------------------------------------------------------------


@dataclass
class Target:
    client: Client
    settings: FilingSettings | None
    settings_error: str | None = None


def period_targets(paths: Paths, period: TaxPeriod, client_ids: Iterable[str] | None = None) -> tuple[list[Target], list[str], list[str]]:
    """(신고 대상, 이번 회차 신고 대상 아님 id, 명부에 없는 id)"""
    clients = [c for c in load_clients(paths.clients_dir) if c.active]
    wanted = [str(x) for x in client_ids] if client_ids else None
    unknown: list[str] = []
    if wanted:
        known = {c.id for c in clients}
        unknown = [x for x in wanted if x not in known]
        clients = [c for c in clients if c.id in set(wanted)]
    targets: list[Target] = []
    not_required: list[str] = []
    for c in clients:
        try:
            s = load_filing_settings(paths.clients_dir, c.id, period.code)
        except Exception as e:
            targets.append(Target(c, None, redact(f"회차 설정 파일 오류: {type(e).__name__}: {e}")))
            continue
        if not filing_required(c, period, s.filed_preliminary):
            not_required.append(c.id)
            continue
        targets.append(Target(c, s))
    return targets, not_required, unknown


def run_period(
    period_code: str,
    client_ids: Iterable[str] | None = None,
    stages: Iterable[str] | None = None,
    only_failed: bool = False,
    from_stage: str | None = None,
    base_dir: Path | str | None = None,
    *,
    today: date | None = None,
    stage_fns: dict[str, StageFn] | None = None,
    logger: logging.Logger | None = None,
    dry_run: bool = False,
    command: str = "run",
) -> dict:
    """회차 전체(또는 일부 거래처) 실행 → _summary.json 반환. 다른 실행 중이면 LockBusy."""
    paths = Paths.from_base(base_dir)
    period = TaxPeriod.parse(period_code)
    plan = normalize_stages(stages, from_stage)
    today = today or date.today()
    lg = logger or log
    policy = load_policy(paths.config_dir)
    holidays = load_holidays(paths.config_dir)
    run_info: dict[str, Any] = {
        "command": command, "started_at": now_iso(), "finished_at": None, "stages": plan,
        "from_stage": from_stage, "only_failed": only_failed,
        "client_ids": list(client_ids) if client_ids else None, "ran": [],
    }
    with lock_for(paths, command):
        targets, not_required, unknown = period_targets(paths, period, client_ids)
        for x in unknown:
            lg.warning("명부에 없는 거래처: %s", x)
        for t in targets:
            c = t.client
            ws = paths.workspace(period.code, c.id)
            if t.settings is None:
                _mark_client(ws, c, period.code, ST_FAILED, t.settings_error or "", failed_stage="setup")
                run_info["ran"].append(c.id)
                lg.error("[%s %s] 설정 오류: %s", period.code, c.id, t.settings_error)
                continue
            if t.settings.skip:
                _mark_client(ws, c, period.code, ST_SKIPPED, "회차 설정 skip: 자동처리 제외(직접 처리)")
                continue
            if only_failed and (ws.load_state() or {}).get("status") == ST_OK:
                continue
            run_info["ran"].append(c.id)
            try:
                ctx = make_context(c, period, paths=paths, settings=t.settings, policy=policy,
                                   holidays=holidays, today=today, logger=lg, dry_run=dry_run)
            except Exception as e:  # 기한 파라미터 누락 등
                msg = redact(f"{type(e).__name__}: {e}")
                st = _mark_client(ws, c, period.code, ST_FAILED, msg, failed_stage="setup")
                st["traceback"] = redact(traceback.format_exc())
                ws.save_state(st)
                lg.error("[%s %s] 준비 실패: %s", period.code, c.id, msg)
                continue
            run_client(ctx, plan, None, stage_fns)
        run_info["finished_at"] = now_iso()
        summary = write_summary(period.code, paths=paths, today=today, run_info=run_info,
                                unknown_client_ids=unknown)
    return summary


# ---------------------------------------------------------------------------
# _summary.json
# ---------------------------------------------------------------------------

_SEV_ORDER = {Severity.BLOCKER.value: 0, Severity.WARN.value: 1, Severity.INFO.value: 2}


def final_tax_of(ret: dict | None) -> int | None:
    """return.json 의 FINAL(27) 라인 세액. +납부 / -환급. 없으면 None."""
    if not ret:
        return None
    v = ((ret.get("lines") or {}).get("FINAL") or {}).get("tax")
    return int(v) if v is not None else None


def review_counts(items: list[dict]) -> dict[str, int]:
    out = {"blocker_open": 0, "warn_open": 0, "info_open": 0, "resolved": 0}
    for it in items:
        if it.get("status", ReviewStatus.OPEN.value) != ReviewStatus.OPEN.value:
            out["resolved"] += 1
            continue
        key = {Severity.BLOCKER.value: "blocker_open", Severity.WARN.value: "warn_open"}.get(it.get("severity"), "info_open")
        out[key] += 1
    return out


def sort_review(items: list[dict]) -> list[dict]:
    """미해결 먼저, 차단→경고→참고, 세액영향 큰 순."""
    return sorted(
        items,
        key=lambda i: (
            i.get("status", ReviewStatus.OPEN.value) != ReviewStatus.OPEN.value,
            _SEV_ORDER.get(i.get("severity"), 9),
            -abs(int(i.get("tax_impact") or 0)),
        ),
    )


def wehago_status(ws: Workspace) -> dict | None:
    """wehago/agent_log.jsonl 마지막 줄 → {step, status, at, note}. 없으면 None."""
    p = ws.root / "wehago" / "agent_log.jsonl"
    if not p.exists():
        return None
    last = None
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line)
        except ValueError:
            continue
    if not last:
        return None
    return {"step": last.get("step"), "status": last.get("status"), "at": last.get("ts"),
            "note": redact(last.get("note"))[:200] or None}


def client_summary(paths: Paths, period: TaxPeriod, client: Client, settings: FilingSettings | None,
                   law: Law, holidays: set[date], today: date, settings_error: str | None = None) -> dict:
    ws = paths.workspace(period.code, client.id)
    state = ws.load_state() or {}
    status = state.get("status") or ST_PENDING
    if settings is not None and settings.skip:
        status = ST_SKIPPED
    ret = ws.load_json("return.json")
    items = ws.load_json("review.json", []) or []
    counts = review_counts(items)
    due: date | None = None
    try:
        due = make_filing(client, period, settings or FilingSettings(), law, holidays).due_date
    except Exception:
        fd = ws.load_json("filing.json")
        if fd and fd.get("due_date"):
            due = date.fromisoformat(fd["due_date"])
    ft = final_tax_of(ret)
    top = [
        {"id": i.get("id"), "severity": i.get("severity"), "code": i.get("code"),
         "title": redact(i.get("title")), "tax_impact": int(i.get("tax_impact") or 0)}
        for i in sort_review(items) if i.get("status", ReviewStatus.OPEN.value) == ReviewStatus.OPEN.value
    ][:3]
    report_dir = ws.report_dir
    return {
        "client_id": client.id,
        "name": client.name,
        "biz_no_masked": mask_biz_no(client.biz_no),
        "taxpayer_type": client.taxpayer_type.value,
        "status": status,
        "failed_stage": state.get("failed_stage"),
        "error": redact(state.get("error") or settings_error) or None,
        "stages": {k: (v or {}).get("status") for k, v in (state.get("stages") or {}).items()},
        "final_tax": ft,
        "payable": max(ft, 0) if ft is not None else None,
        "refund": max(-ft, 0) if ft is not None else None,
        "blocker_open": counts["blocker_open"],
        "warn_open": counts["warn_open"],
        "info_open": counts["info_open"],
        "resolved": counts["resolved"],
        "ready_to_file": bool(status == ST_OK and ft is not None and counts["blocker_open"] == 0),
        "due_date": due.isoformat() if due else None,
        "d_day": (due - today).days if due else None,
        "last_run_at": (state.get("last_run") or {}).get("finished_at"),
        "unverified_law_params": len(state.get("unverified_law_params") or []),
        "top_items": top,
        "wehago_status": wehago_status(ws),
        "review_html": str((report_dir / "review.html").relative_to(paths.period_dir(period.code))).replace("\\", "/")
        if (report_dir / "review.html").exists() else None,
    }


def build_summary(period_code: str, *, paths: Paths, today: date | None = None, run_info: dict | None = None,
                  unknown_client_ids: list[str] | None = None) -> dict:
    period = TaxPeriod.parse(period_code)
    today = today or date.today()
    law = Law.load(paths.config_dir)
    holidays = load_holidays(paths.config_dir)
    targets, not_required, _ = period_targets(paths, period)
    rows = [client_summary(paths, period, t.client, t.settings, law, holidays, today, t.settings_error) for t in targets]
    prev = _load(paths.period_dir(period.code) / "_summary.json", {}) or {}
    totals = {
        "clients": len(rows),
        "ok": sum(r["status"] == ST_OK for r in rows),
        "failed": sum(r["status"] == ST_FAILED for r in rows),
        "not_implemented": sum(r["status"] == ST_NOT_IMPLEMENTED for r in rows),
        "skipped": sum(r["status"] == ST_SKIPPED for r in rows),
        "pending": sum(r["status"] == ST_PENDING for r in rows),
        "ready_to_file": sum(r["ready_to_file"] for r in rows),
        "with_blockers": sum(r["blocker_open"] > 0 for r in rows),
        "blocker_open": sum(r["blocker_open"] for r in rows),
        "warn_open": sum(r["warn_open"] for r in rows),
        "payable_total": sum(r["payable"] or 0 for r in rows),
        "refund_total": sum(r["refund"] or 0 for r in rows),
    }
    dues = sorted({r["due_date"] for r in rows if r["due_date"]})
    return {
        "schema": SUMMARY_SCHEMA,
        "period": period.code,
        "period_label": period.label,
        "generated_at": now_iso(),
        "as_of": today.isoformat(),
        "due_date": dues[0] if dues else None,
        "d_day": (date.fromisoformat(dues[0]) - today).days if dues else None,
        "last_run": run_info if run_info is not None else prev.get("last_run"),
        "totals": totals,
        "clients": rows,
        "not_required": not_required,
        "unknown_client_ids": unknown_client_ids or [],
    }


def write_summary(period_code: str, *, paths: Paths | None = None, base_dir: Path | str | None = None,
                  today: date | None = None, run_info: dict | None = None, unknown_client_ids: list[str] | None = None,
                  office_reports: bool = True) -> dict:
    """_summary.json 작성 + (기본) _dashboard.html, _briefing.md 갱신."""
    paths = paths or Paths.from_base(base_dir)
    summary = build_summary(period_code, paths=paths, today=today, run_info=run_info, unknown_client_ids=unknown_client_ids)
    pdir = paths.period_dir(period_code)
    _dump(pdir / "_summary.json", summary)
    if office_reports:
        try:
            from .report.stage import build_office_reports

            top_n = int((load_policy(paths.config_dir).get("run") or {}).get("briefing_top_n") or 5)
            build_office_reports(pdir, top_n=top_n)
        except Exception:
            log.exception("사무실 리포트(_dashboard/_briefing) 작성 실패")
    return summary


def load_summary(period_code: str, *, paths: Paths | None = None, base_dir: Path | str | None = None) -> dict | None:
    paths = paths or Paths.from_base(base_dir)
    return _load(paths.period_dir(period_code) / "_summary.json", None)

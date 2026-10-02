"""회차 단위 사무실 리포트: _dashboard.html, _briefing.md.

입력은 data/{period}/_summary.json 하나(스키마: docs/RUNBOOK.md 부록).
수치는 요약 파일 값만 쓰고, 여기서 새로 계산하지 않는다(합계·건수 집계만).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .fmt import BASE_CSS, dday, esc, kdate, won

STATUS_LABEL = {
    "ok": "완료",
    "failed": "실패",
    "not_implemented": "미구현 단계",
    "skipped": "자동처리 제외",
    "pending": "대기",
}
STATUS_CHIP = {"ok": "green", "failed": "red", "not_implemented": "amber", "skipped": "gray", "pending": "gray"}


def _load_summary(period_dir: Path) -> dict:
    p = Path(period_dir) / "_summary.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _tax_text(r: dict) -> str:
    ft = r.get("final_tax")
    if ft is None:
        return "-"
    if ft < 0:
        return f"환급 {-ft:,}"
    return f"{ft:,}"


def _rerun_cmd(period: str, r: dict) -> str:
    stage = r.get("failed_stage")
    frm = f" --from-stage {stage}" if stage and stage != "setup" else ""
    return f"taxauto run --period {period} --client {r['client_id']}{frm}"


def build_office_reports(period_dir: Path, top_n: int = 5) -> dict[str, Path]:
    period_dir = Path(period_dir)
    s = _load_summary(period_dir)
    out = {
        "dashboard": period_dir / "_dashboard.html",
        "briefing": period_dir / "_briefing.md",
    }
    out["dashboard"].write_text(render_dashboard(s), encoding="utf-8")
    out["briefing"].write_text(render_briefing(s, top_n=top_n), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# 대시보드
# ---------------------------------------------------------------------------


def dashboard_order(rows: list[dict]) -> list[dict]:
    """차단 많은 순 → 경고 많은 순 → 기한 임박 순."""
    return sorted(rows, key=lambda r: (-(r.get("blocker_open") or 0), -(r.get("warn_open") or 0),
                                       r.get("d_day") if r.get("d_day") is not None else 9999, r.get("client_id", "")))


DASH_CSS = """
.page{max-width:1100px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0 4px}
.tile{border:1px solid var(--line);border-radius:6px;padding:10px 12px}
.tile .k{font-size:11px;color:var(--muted)}.tile .v{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.tile.red .v{color:var(--red)}.tile.green .v{color:var(--green)}
.wrap{overflow-x:auto;margin-top:14px}
td.n0{color:#b8bec6}
td.blk{color:var(--red);font-weight:700}td.wrn{color:var(--amber);font-weight:700}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.err{color:var(--red);font-size:12px}
"""


def render_dashboard(s: dict) -> str:
    rows = dashboard_order(s.get("clients") or [])
    t = s.get("totals") or {}
    period = s.get("period", "")
    trs = []
    for r in rows:
        st = r.get("status") or "pending"
        link = r.get("review_html")
        name = esc(r.get("name"))
        name_html = f"<a href='{esc(link)}'>{name}</a>" if link else name
        b, w = r.get("blocker_open") or 0, r.get("warn_open") or 0
        note = ""
        if st in ("failed", "not_implemented"):
            note = f"<div class='err'>{esc(r.get('failed_stage') or '')}: {esc((r.get('error') or '')[:120])}</div>"
        elif r.get("top_items"):
            note = f"<div class='muted'>{esc(r['top_items'][0].get('title'))}</div>"
        ready = " <span class='chip green'>신고가능</span>" if r.get("ready_to_file") else ""
        wh = r.get("wehago_status") or {}
        wh_txt = f"{esc(wh.get('step'))} · {esc(wh.get('status'))}" if wh else "<span class='muted'>-</span>"
        trs.append(
            f"<tr><td>{name_html} <span class='mono muted'>{esc(r.get('client_id'))}</span>{note}</td>"
            f"<td><span class='chip {STATUS_CHIP.get(st, 'gray')}'>{esc(STATUS_LABEL.get(st, st))}</span>"
            f"{ready}</td>"
            f"<td class='num'>{esc(_tax_text(r))}</td>"
            f"<td class='num {'blk' if b else 'n0'}'>{b}</td><td class='num {'wrn' if w else 'n0'}'>{w}</td>"
            f"<td class='num'>{esc(dday(r.get('d_day')))}</td><td>{wh_txt}</td></tr>"
        )
    body = "\n".join(trs) or "<tr><td colspan='7' class='muted'>대상 거래처 없음</td></tr>"
    gen = (s.get("generated_at") or "")[:16].replace("T", " ")
    tiles = [
        ("대상 거래처", t.get("clients", 0), ""),
        ("신고 가능(차단 0)", t.get("ready_to_file", 0), "green"),
        ("차단 있는 곳", t.get("with_blockers", 0), "red" if t.get("with_blockers") else ""),
        ("실행 실패", (t.get("failed", 0) or 0) + (t.get("not_implemented", 0) or 0), "red" if t.get("failed") else ""),
        ("납부세액 합계", f"{(t.get('payable_total') or 0):,}", ""),
        ("환급세액 합계", f"{(t.get('refund_total') or 0):,}", ""),
    ]
    tiles_html = "".join(f"<div class='tile {c}'><div class='k'>{esc(k)}</div><div class='v'>{esc(v)}</div></div>" for k, v, c in tiles)
    due = s.get("due_date")
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(s.get('period_label', period))} 부가세 현황</title>
<style>{BASE_CSS}{DASH_CSS}</style></head>
<body><div class="page">
<h1>{esc(s.get('period_label', period))} 부가세 현황</h1>
<div class="muted">기한 {esc(kdate(due, with_year=True) if due else '-')} · {esc(dday(s.get('d_day')))} · 기준 {esc(gen)}</div>
<div class="tiles">{tiles_html}</div>
<div class="wrap"><table>
<thead><tr><th>거래처</th><th>상태</th><th class="num">납부(환급)세액</th><th class="num">차단</th><th class="num">경고</th><th class="num">D-day</th><th>위하고 작업</th></tr></thead>
<tbody>{body}</tbody></table></div>
</div></body></html>
"""


# ---------------------------------------------------------------------------
# 아침 브리핑
# ---------------------------------------------------------------------------


def _attention_score(r: dict) -> tuple:
    d = r.get("d_day")
    d = 9999 if d is None else d
    return (-(r.get("blocker_open") or 0), d, -(r.get("warn_open") or 0), r.get("client_id", ""))


def render_briefing(s: dict, top_n: int = 5) -> str:
    rows = s.get("clients") or []
    t = s.get("totals") or {}
    period = s.get("period", "")
    label = s.get("period_label", period)
    last = s.get("last_run") or {}
    as_of = s.get("as_of") or datetime.now().date().isoformat()
    L: list[str] = [f"# {label} 부가세 아침 브리핑 ({as_of})", ""]
    due = s.get("due_date")
    L.append(f"기한 {kdate(due, with_year=True) if due else '-'} ({dday(s.get('d_day'))})"
             + (f" · 마지막 실행 {str(last.get('finished_at') or '')[:16].replace('T', ' ')}" if last else ""))
    L.append("")

    # 1) 밤사이 처리 결과
    L.append("## 밤사이 처리 결과")
    ran = last.get("ran") if isinstance(last.get("ran"), list) else None
    L.append(
        f"- 대상 {t.get('clients', 0)}곳"
        + (f" (이번 실행 {len(ran)}곳)" if ran is not None else "")
        + f": 완료 {t.get('ok', 0)} · 실패 {t.get('failed', 0)} · 미구현 단계 {t.get('not_implemented', 0)}"
        + f" · 자동처리 제외 {t.get('skipped', 0)} · 대기 {t.get('pending', 0)}"
    )
    L.append(f"- 바로 신고 가능(차단 0): {t.get('ready_to_file', 0)}곳")
    L.append(f"- 미해결 검토: 차단 {t.get('blocker_open', 0)}건({t.get('with_blockers', 0)}곳) · 경고 {t.get('warn_open', 0)}건")
    L.append(f"- 납부세액 합계 {won(t.get('payable_total') or 0)} · 환급세액 합계 {won(t.get('refund_total') or 0)}")
    if s.get("not_required"):
        L.append(f"- 이번 회차 신고 대상 아님: {len(s['not_required'])}곳")
    if s.get("unknown_client_ids"):
        L.append(f"- 명부에 없는 거래처 지정: {', '.join(s['unknown_client_ids'])}")
    L.append("")

    # 2) 사람이 볼 것 TOP N
    attention = [r for r in rows if (r.get("blocker_open") or 0) or (r.get("warn_open") or 0)]
    attention.sort(key=_attention_score)
    L.append(f"## 사람이 볼 것 TOP {top_n}")
    if not attention:
        L.append("- 미해결 검토항목이 있는 거래처 없음.")
    for i, r in enumerate(attention[:top_n], 1):
        L.append(
            f"{i}. **{r.get('name')}({r.get('client_id')})** — 차단 {r.get('blocker_open', 0)} · 경고 {r.get('warn_open', 0)}"
            f" · {dday(r.get('d_day'))} · 세액 {_tax_text(r)}"
            + (f" · `{r['review_html']}`" if r.get("review_html") else "")
        )
        for it in (r.get("top_items") or [])[:3]:
            imp = f" (세액영향 {won(it.get('tax_impact'), sign=True)})" if it.get("tax_impact") else ""
            L.append(f"   - [{it.get('severity')}] {it.get('title')}{imp} `{it.get('id')}`")
    if len(attention) > top_n:
        L.append(f"- 그 외 {len(attention) - top_n}곳은 대시보드(_dashboard.html) 참고.")
    L.append("")

    # 3) 실패한 거래처
    failed = [r for r in rows if r.get("status") in ("failed", "not_implemented")]
    L.append("## 실패한 거래처")
    if not failed:
        L.append("- 없음.")
    for r in failed:
        kind = "실패" if r["status"] == "failed" else "미구현 단계"
        L.append(f"- **{r.get('name')}({r.get('client_id')})** — {r.get('failed_stage') or '-'} 단계 {kind}: {(r.get('error') or '').strip()[:200]}")
        L.append(f"  - 재실행: `{_rerun_cmd(period, r)}`")
    L.append("")

    # 4) 조언 (결과 파일 값에서만)
    L.append("## 조언")
    for line in _advice(rows, t, period)[:2] or ["특이사항 없음. 경고 항목만 순서대로 확인하면 됩니다."]:
        L.append(f"- {line}")
    L.append("")
    L.append("> 전자신고 제출은 사람이 승인 후 진행합니다. 이 브리핑은 엔진 결과 파일(_summary.json)만으로 작성됐습니다.")
    return "\n".join(L) + "\n"


def _advice(rows: list[dict], t: dict, period: str) -> list[str]:
    out: list[str] = []
    urgent = [r for r in rows if (r.get("blocker_open") or 0) and r.get("d_day") is not None and r["d_day"] <= 7]
    if urgent:
        names = ", ".join(r.get("name", "") for r in urgent[:3])
        out.append(f"기한 7일 이내인데 차단이 남은 곳 {len(urgent)}곳({names}) — 오늘 이 거래처부터 처리.")
    data_fail = [r for r in rows if r.get("status") == "failed" and r.get("failed_stage") in ("collect", "normalize")]
    if data_fail:
        out.append(f"자료 단계(collect/normalize) 실패 {len(data_fail)}곳 — inbox/{period}/ 에 자료가 빠졌거나 파일 형식이 바뀐 경우가 많습니다.")
    if t.get("not_implemented"):
        out.append(f"미구현 단계 때문에 끝까지 못 간 곳 {t['not_implemented']}곳 — 엔진 업데이트 전까지 해당 단계는 수작업.")
    if t.get("with_blockers") and not urgent:
        out.append(f"차단이 남은 {t['with_blockers']}곳은 아직 신고 불가 — 차단 항목부터 처리.")
    unv = sum(1 for r in rows if r.get("unverified_law_params"))
    if unv:
        out.append(f"미검증 세법 파라미터를 쓴 거래처 {unv}곳 — config/law 값 확인 후 verified 처리 필요.")
    ready_warn = [r for r in rows if r.get("ready_to_file") and (r.get("warn_open") or 0)]
    if ready_warn:
        out.append(f"차단 없이 경고만 남은 {len(ready_warn)}곳은 경고 확인만 끝내면 위하고 반영·신고 진행 가능.")
    return out

"""학습(build) · 평가(eval) · 리포트(report).

private/{period}/
  clients.csv                      (선택) id,name,biz_no,industry,industry_code[,industry_group]
  {client_id}/
    *.xlsx|xls|csv                 위하고 매입매출전표 내보내기(필수) + 홈택스 원천 엑셀(선택)
    transactions.json              (선택) 이미 정규화된 원천 거래
    *.pdf                          (선택) 세무사 매출/매입 요약 보고서
"""

from __future__ import annotations

import csv
import json
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from ..law import CONFIG_DIR
from ..models import Client, Filing, ParseIssue, Source, TaxPeriod, Transaction
from ..redact import mask_biz_no
from .features import client_group, is_person_like, style_config
from .ledger_import import LedgerEntry, parse_ledger_file
from .miner import FIELD_LABEL, MineResult, Question, StyleModel, display_value, mine
from .pairing import build_examples, pair_entries
from .pdf_summary import parse_summary_pdf, save_summary
from .store import (
    ALL_PACK,
    load_client_answers,
    load_examples,
    load_questions,
    save_client_style,
    save_examples,
    save_pack,
    save_questions,
)

EXCEL_EXT = {".xlsx", ".xlsm", ".xls", ".csv", ".txt", ".tsv", ".htm", ".html"}
EVAL_FIELDS = ["account", "entry_type", "nd_reason", "settlement", "summary", "fixed_asset"]

# 예측 유형 → PDF 요약 항목 (eval 의 신고 합계 대사용)
ENTRY_TO_PDF = {
    ("매출", "과세"): "sales.tax_invoice", ("매출", "카과"): "sales.card_cash", ("매출", "현과"): "sales.card_cash",
    ("매출", "건별"): "sales.other", ("매입", "과세"): "purchase.tax_invoice", ("매입", "카과"): "purchase.card_cash",
    ("매입", "현과"): "purchase.card_cash", ("매입", "불공"): "purchase.non_deductible",
}


def learn_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "_learn"


# ---------------------------------------------------------------------------
# 거래처 정보
# ---------------------------------------------------------------------------


def load_client_infos(clients_yaml: Path | None, src_dir: Path | None) -> dict[str, dict]:
    """clients.yaml + private/{period}/clients.csv → {id: {id,name,biz_no,industry,industry_code,group}}."""
    infos: dict[str, dict] = {}
    if clients_yaml and Path(clients_yaml).exists():
        data = yaml.safe_load(Path(clients_yaml).read_text(encoding="utf-8")) or {}
        for c in data.get("clients") or []:
            cl = Client.from_dict(c)
            infos[cl.id] = {"id": cl.id, "name": cl.name, "biz_no": cl.biz_no, "industry": cl.industry,
                            "industry_code": cl.industry_code, "industry_group": str(c.get("industry_group") or "")}
    if src_dir and (Path(src_dir) / "clients.csv").exists():
        raw = (Path(src_dir) / "clients.csv").read_bytes()
        for enc in ("utf-8-sig", "cp949"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        for row in csv.DictReader(text.splitlines()):
            cid = str(row.get("id") or row.get("client_id") or "").strip()
            if not cid:
                continue
            d = infos.setdefault(cid, {"id": cid})
            for k in ("name", "biz_no", "industry", "industry_code", "industry_group"):
                if row.get(k) and not d.get(k):
                    d[k] = str(row[k]).strip()
    for d in infos.values():
        d["group"] = client_group(d)
    return infos


def _filing_for(period: str, client_id: str) -> Filing:
    from ..period import coverage

    try:
        tp = TaxPeriod.parse(period)
        s, e = coverage(tp, filed_preliminary=False)
    except (ValueError, IndexError):
        tp = TaxPeriod(date.today().year, 1, "F")
        s, e = date(tp.year, 1, 1), date(tp.year, 12, 31)
    return Filing(client_id=client_id, period=tp, coverage_start=s, coverage_end=e, due_date=e)


def _read_sources(path: Path, info: dict, period: str, issues: list[ParseIssue]) -> list[Transaction]:
    """홈택스 원천 엑셀 → Transaction (정규화 섹터 파서 재사용). 실패해도 학습은 계속."""
    try:
        from ..ingest.normalize import parse_file
    except ImportError:
        issues.append(ParseIssue(path.name, 0, "원천 파서(ingest.normalize) 없음 - 전표만으로 학습"))
        return []
    client = Client(id=info["id"], name=info.get("name", ""), biz_no=info.get("biz_no", ""))
    try:
        r = parse_file(path, client, _filing_for(period, info["id"]))
    except Exception as e:  # 다른 섹터 파서 오류가 학습 전체를 멈추지 않게
        issues.append(ParseIssue(path.name, 0, f"원천 파일 해석 실패({type(e).__name__})"))
        return []
    issues.extend(r.issues)
    return [t for t in r.transactions if t.source != Source.WEHAGO_LEDGER]


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


@dataclass
class ClientLoad:
    client_id: str
    group: str
    entries: list[LedgerEntry] = field(default_factory=list)
    sources: list[Transaction] = field(default_factory=list)
    summaries: list[dict] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    pairing: dict = field(default_factory=dict)


@dataclass
class BuildResult:
    period: str
    loads: list[ClientLoad]
    examples: list[dict]
    mined: MineResult
    paths: dict[str, str] = field(default_factory=dict)


def load_client_dir(cdir: Path, info: dict, period: str, data_dir: Path | None, config_dir: Path | None = None) -> ClientLoad:
    cid = info["id"]
    cl = ClientLoad(cid, info.get("group") or "other")
    year = TaxPeriod.parse(period).year if _is_period(period) else None
    for p in sorted(cdir.iterdir()):
        if not p.is_file() or p.name.startswith(("~$", ".", "_")):
            continue
        ext = p.suffix.lower()
        if ext == ".pdf":
            s = parse_summary_pdf(p, config_dir)
            cl.summaries.append(s)
            if data_dir is not None and period:
                save_summary(s, data_dir, period, cid)
        elif p.name == "transactions.json":
            cl.sources += [t for t in (Transaction.from_dict(d) for d in json.loads(p.read_text(encoding="utf-8")))
                           if t.source != Source.WEHAGO_LEDGER]
        elif ext in EXCEL_EXT:
            r = parse_ledger_file(p, cid, config_dir, year)
            if r.is_ledger:
                cl.entries += r.entries
                cl.issues += r.issues
            else:
                cl.sources += _read_sources(p, info, period, cl.issues)
    if not cl.sources and data_dir is not None and period:
        ws = Path(data_dir) / period / cid / "transactions.json"   # 파이프라인이 이미 만든 원천 거래
        if ws.exists():
            cl.sources = [t for t in (Transaction.from_dict(d) for d in json.loads(ws.read_text(encoding="utf-8")))
                          if t.source != Source.WEHAGO_LEDGER]
    return cl


def _is_period(code: str) -> bool:
    try:
        TaxPeriod.parse(code)
        return True
    except (ValueError, IndexError):
        return False


def build(
    src_dir: Path,
    clients_yaml: Path | None,
    data_dir: Path,
    clients_dir: Path,
    packs_dir: Path,
    config_dir: Path | None = None,
) -> BuildResult:
    src_dir = Path(src_dir)
    cfg = style_config(config_dir)
    period = src_dir.name if _is_period(src_dir.name) else ""
    infos = load_client_infos(clients_yaml, src_dir)
    loads: list[ClientLoad] = []
    examples: list[dict] = []
    for cdir in sorted(p for p in src_dir.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        info = infos.get(cdir.name) or {"id": cdir.name, "group": "other"}
        cl = load_client_dir(cdir, info, period, data_dir, config_dir)
        if not info.get("industry") and not info.get("industry_code") and not info.get("industry_group"):
            cl.issues.append(ParseIssue("", 0, "업종 정보 없음(clients.yaml/clients.csv) → 업종그룹 other"))
        pr = pair_entries(cl.entries, cl.sources)
        cl.pairing = pr.counts
        exs = build_examples(pr.pairs, cl.group, cfg)
        for ex in exs:
            ex["period"] = period
        examples += exs
        loads.append(cl)

    # 질문 답변(이전 build 의 questions.yaml + 거래처 style.yaml 보존분)
    ldir = learn_dir(data_dir)
    answers = {k: v for k, v in load_questions(ldir / "questions.yaml").items() if v.answer}
    for cl in loads:
        for k, v in load_client_answers(Path(clients_dir) / cl.client_id).items():
            answers.setdefault(k, v)
    mined = mine(examples, cfg, answers)

    res = BuildResult(period, loads, examples, mined)
    for cl in loads:
        ans = [q.to_dict() for q in mined.questions if q.client_id == cl.client_id and q.answer]
        p = save_client_style(Path(clients_dir) / cl.client_id, cl.client_id, cl.group,
                              mined.client_rules.get(cl.client_id, []),
                              {"source": str(src_dir), "examples": sum(1 for e in examples if e["client_id"] == cl.client_id),
                               "answers": ans})
        res.paths[f"style:{cl.client_id}"] = str(p)
    group_clients = Counter(cl.group for cl in loads)
    for g in sorted(set(group_clients) | set(mined.industry_rules)):
        p = save_pack(packs_dir, g, mined.industry_rules.get(g, []), {"clients": group_clients.get(g, 0)})
        res.paths[f"pack:{g}"] = str(p)
    res.paths[f"pack:{ALL_PACK}"] = str(save_pack(packs_dir, ALL_PACK, mined.all_rules, {"clients": len(loads)}))
    save_examples(ldir / "examples.jsonl", examples)
    save_questions(ldir / "questions.yaml", mined.questions)
    res.paths["examples"] = str(ldir / "examples.jsonl")
    res.paths["questions"] = str(ldir / "questions.yaml")
    rp = ldir / "style_report.md"
    rp.write_text(render_build_report(res, cfg), encoding="utf-8")
    res.paths["report"] = str(rp)
    return res


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------


def split_holdout(examples: list[dict], holdout: float, seed: int) -> tuple[set[str], set[str]]:
    """거래처 단위 홀드아웃(업종그룹별 층화). 반환 (train_ids, test_ids)."""
    by_group: dict[str, list[str]] = defaultdict(list)
    for cid, g in sorted({(e["client_id"], e.get("group", "other")) for e in examples}):
        by_group[g].append(cid)
    rng = random.Random(seed)
    test: set[str] = set()
    for g in sorted(by_group):
        ids = sorted(by_group[g])
        rng.shuffle(ids)
        n = len(ids)
        k = round(n * holdout)
        if n >= 2:
            k = min(max(k, 1), n - 1)
        else:
            k = 0
        test.update(ids[:k])
    train = {e["client_id"] for e in examples} - test
    return train, test


def _pred_equal(fld: str, pred: str, label: str) -> bool:
    if fld == "account":
        pc, _, pn = pred.partition("|")
        lc, _, ln = label.partition("|")
        return pc == lc if (pc and lc) else pn == ln
    return pred == label


def evaluate(
    examples: list[dict],
    holdout: float = 0.25,
    seed: int = 1,
    by: str = "client",
    cfg: dict | None = None,
    summaries: dict[str, list[dict]] | None = None,
    top_wrong: int = 15,
) -> dict:
    cfg = cfg if cfg is not None else style_config()
    if by == "time":
        train_ex, test_ex = [], []
        per: dict[str, list[dict]] = defaultdict(list)
        for e in examples:
            per[e["client_id"]].append(e)
        for cid, exs in per.items():
            exs = sorted(exs, key=lambda x: (x.get("tx_date", ""), x.get("row_no", 0)))
            cut = int(round(len(exs) * (1 - holdout)))
            train_ex += exs[:cut]
            test_ex += exs[cut:]
        train_ids = test_ids = set(per)
    else:
        train_ids, test_ids = split_holdout(examples, holdout, seed)
        train_ex = [e for e in examples if e["client_id"] in train_ids]
        test_ex = [e for e in examples if e["client_id"] in test_ids]
    m = mine(train_ex, cfg)
    model = StyleModel.from_mine(m, cfg)
    stats = {f: {"n": 0, "covered": 0, "correct": 0} for f in EVAL_FIELDS}
    by_level: dict[str, Counter] = {f: Counter() for f in EVAL_FIELDS}
    wrong: list[dict] = []
    pdf_pred: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in test_ex:
        preds = model.predict(e)
        labels = e.get("labels") or {}
        for f in EVAL_FIELDS:
            lab = labels.get(f)
            if lab in (None, ""):
                continue
            st = stats[f]
            st["n"] += 1
            p = preds.get(f)
            if p is None:
                continue
            st["covered"] += 1
            by_level[f][p.rule.level] += 1
            if _pred_equal(f, p.value, str(lab)):
                st["correct"] += 1
            else:
                wrong.append({"client_id": e["client_id"], "field": f, "merchant": e.get("display", ""), "mcat": e.get("mcat", ""),
                              "doc_type": e.get("doc_type", ""), "amount": e.get("supply_amount", 0),
                              "pred": display_value(f, p.value), "label": display_value(f, str(lab)), "rule": f"{p.origin}#{p.rule.id}({p.rule.level})"})
        et = preds.get("entry_type")
        key = ENTRY_TO_PDF.get((e.get("direction", ""), et.value if et else labels.get("entry_type", "")))
        if key:
            pdf_pred[e["client_id"]][key] += int(e.get("supply_amount", 0))
    for f, st in stats.items():
        n, c, k = st["n"], st["covered"], st["correct"]
        st["coverage"] = round(c / n, 4) if n else None
        st["precision"] = round(k / c, 4) if c else None
        st["accuracy"] = round(k / n, 4) if n else None
        st["by_level"] = dict(by_level[f])
    # PDF 정답 합계 대사(테스트 거래처 중 요약 PDF가 있는 곳)
    pdf_checks = []
    for cid in sorted(test_ids if by == "client" else {e["client_id"] for e in test_ex}):
        for s in (summaries or {}).get(cid, []):
            for key, amt in sorted(pdf_pred.get(cid, {}).items()):
                sec, item = key.split(".", 1)
                if item == "card_cash":
                    pv = sum(int(((s.get(sec) or {}).get(x) or {}).get("amount", 0)) for x in ("card", "cash_receipt", "card_cash"))
                    has = any((s.get(sec) or {}).get(x) for x in ("card", "cash_receipt", "card_cash"))
                else:
                    v = (s.get(sec) or {}).get(item) or {}
                    pv, has = int(v.get("amount", 0)), bool(v)
                if has:
                    pdf_checks.append({"client_id": cid, "item": key, "pdf": pv, "predicted": amt, "diff": amt - pv})
    wrong_counter = Counter((w["field"], w["merchant"], w["pred"], w["label"]) for w in wrong)
    top = []
    seen = set()
    for w in sorted(wrong, key=lambda w: -wrong_counter[(w["field"], w["merchant"], w["pred"], w["label"])]):
        k = (w["field"], w["merchant"], w["pred"], w["label"])
        if k in seen:
            continue
        seen.add(k)
        top.append({**w, "count": wrong_counter[k]})
        if len(top) >= top_wrong:
            break
    return {
        "by": by, "holdout": holdout, "seed": seed,
        "train_clients": sorted(train_ids), "test_clients": sorted(test_ids),
        "train_examples": len(train_ex), "test_examples": len(test_ex),
        "rules": {"client": sum(len(v) for v in m.client_rules.values()), "industry": sum(len(v) for v in m.industry_rules.values()),
                  "all": len(m.all_rules)},
        "fields": stats, "top_wrong": top, "pdf_checks": pdf_checks,
    }


def load_summaries(data_dir: Path, examples: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for cid, per in sorted({(e["client_id"], e.get("period", "")) for e in examples}):
        p = Path(data_dir) / per / cid / "summary_pdf.json" if per else None
        if p and p.exists():
            out[cid].append(json.loads(p.read_text(encoding="utf-8")))
    return out


def run_eval(data_dir: Path, holdout: float, seed: int, by: str = "client", today: date | None = None,
             config_dir: Path | None = None) -> tuple[dict, Path]:
    ldir = learn_dir(data_dir)
    examples = load_examples(ldir / "examples.jsonl")
    if not examples:
        raise FileNotFoundError(f"학습 사례 없음: {ldir / 'examples.jsonl'} - 먼저 build 실행")
    cfg = style_config(config_dir)
    res = evaluate(examples, holdout, seed, by, cfg, load_summaries(data_dir, examples))
    d = (today or date.today()).strftime("%Y%m%d")
    md = ldir / f"eval_{d}.md"
    md.write_text(render_eval_report(res, cfg), encoding="utf-8")
    (ldir / f"eval_{d}.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res, md


# ---------------------------------------------------------------------------
# 리포트(markdown)
# ---------------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def _safe(name: str, cfg: dict) -> str:
    return "(개인명)" if is_person_like(name, cfg) else name


def render_eval_report(res: dict, cfg: dict) -> str:
    L = [f"# 스타일 학습 평가 ({res['by']} 홀드아웃 {res['holdout']}, seed {res['seed']})", ""]
    L.append(f"- 학습 거래처 {len(res['train_clients'])}곳 / 사례 {res['train_examples']}건, "
             f"테스트 거래처 {len(res['test_clients'])}곳 / 사례 {res['test_examples']}건")
    L.append(f"- 테스트 거래처: {', '.join(res['test_clients'])}")
    r = res["rules"]
    L.append(f"- 학습셋 규칙: 거래처 {r['client']} / 업종 {r['industry']} / 전체 {r['all']}")
    L += ["", "| 필드 | 대상 | 커버리지 | 정밀도(맞힘/예측) | 정확도(맞힘/전체) | 계층별 예측 |", "|---|---:|---:|---:|---:|---|"]
    for f, st in res["fields"].items():
        lv = ", ".join(f"{k} {v}" for k, v in sorted(st["by_level"].items()))
        L.append(f"| {FIELD_LABEL.get(f, f)} | {st['n']} | {_pct(st['coverage'])} | {_pct(st['precision'])} | {_pct(st['accuracy'])} | {lv} |")
    L += ["", "해석: 커버리지 = 규칙이 답을 낸 비율, 정밀도 = 낸 답 중 맞은 비율. 거래처 홀드아웃이므로 '처음 보는 거래처'에 업종 공용 규칙만으로 얼마나 맞히는지다.",
          "같은 seed·holdout 으로 규칙/설정/모델을 바꾼 전후 숫자를 비교한다.", ""]
    if res["top_wrong"]:
        L += ["## 오답 상위", "", "| 거래처 | 필드 | 가맹점 | 업종 | 문서 | 예측 | 정답 | 건수 | 근거 |", "|---|---|---|---|---|---|---|---:|---|"]
        for w in res["top_wrong"]:
            L.append(f"| {w['client_id']} | {FIELD_LABEL.get(w['field'], w['field'])} | {_safe(w['merchant'], cfg)} | {w['mcat']} | {w['doc_type']} | "
                     f"{w['pred']} | {w['label']} | {w['count']} | {w['rule']} |")
        L.append("")
    if res["pdf_checks"]:
        L += ["## 요약 PDF(신고 정답) 대사 — 예측 유형으로 묶은 공급가액 합계", "", "| 거래처 | 항목 | PDF | 예측합계 | 차이 |", "|---|---|---:|---:|---:|"]
        for c in res["pdf_checks"]:
            L.append(f"| {c['client_id']} | {c['item']} | {c['pdf']:,} | {c['predicted']:,} | {c['diff']:+,} |")
        L.append("")
    return "\n".join(L) + "\n"


def render_build_report(res: BuildResult, cfg: dict) -> str:
    m = res.mined
    L = [f"# 세무사 전표 스타일 학습 리포트 ({res.period or '기간 미상'})", ""]
    L.append(f"- 거래처 {len(res.loads)}곳, 전표 {sum(len(c.entries) for c in res.loads)}건, 학습 사례 {len(res.examples)}건")
    L.append(f"- 규칙: 거래처 전용 {sum(len(v) for v in m.client_rules.values())}, 업종 공용 {sum(len(v) for v in m.industry_rules.values())}, 전체 공용 {len(m.all_rules)}")
    L.append(f"- 공용 규칙에서 제외: 1개 거래처에서만 나온 키 {m.single_client_skipped}, 개인이름형 상호 {m.privacy_excluded}")
    open_q = [q for q in m.questions if not q.answer]
    L.append(f"- 세무사 확인 질문: 미답 {len(open_q)} / 답변 {len(m.questions) - len(open_q)} (data/_learn/questions.yaml 의 answer 칸에 적고 다시 build)")
    L += ["", "## 거래처별", "", "| 거래처 | 업종그룹 | 전표 | 원천 | 짝지음(승인/일자금액/근접/전표만) | 원천만 | 규칙 | 질문 | 파싱경고 | 요약PDF |",
          "|---|---|---:|---:|---|---:|---:|---:|---:|---:|"]
    for c in res.loads:
        pc = c.pairing
        L.append(f"| {c.client_id} | {c.group} | {len(c.entries)} | {len(c.sources)} | "
                 f"{pc.get('approval', 0)}/{pc.get('date_amount_biz', 0)}/{pc.get('near_date_amount', 0)}/{pc.get('ledger_only', 0)} | "
                 f"{pc.get('source_unpaired', 0)} | {len(m.client_rules.get(c.client_id, []))} | "
                 f"{sum(1 for q in open_q if q.client_id == c.client_id)} | {len(c.issues)} | {len(c.summaries)} |")
    if open_q:
        L += ["", "## 세무사 확인 질문(같은 가맹점인데 처리가 다름)", ""]
        for q in open_q:
            opts = " / ".join(f"{o['label']} ({o['count']}건)" for o in q.options)
            where = q.direction + (f"·{q.doc_type}" if q.doc_type else "")
            L.append(f"- `{q.id}` [{q.client_id}] {q.merchant or mask_biz_no(q.disc)} ({where}) {FIELD_LABEL.get(q.field, q.field)}: {opts}")
    if m.divergent:
        L += ["", "## 거래처마다 달라 공용 규칙을 만들지 않은 항목(참고)", ""]
        for d in m.divergent[:30]:
            dist = ", ".join(f"{k} {v}" for k, v in d["dist"].items())
            L.append(f"- {d['scope']} {d['direction']}{('·' + d['doc_type']) if d['doc_type'] else ''} "
                     f"'{_safe(d['key'], cfg)}' {FIELD_LABEL.get(d['field'], d['field'])}: {dist} (거래처 {d['clients']}곳)")
    issues = [(c.client_id, i) for c in res.loads for i in c.issues]
    if issues:
        L += ["", "## 파싱 경고(상위 30)", ""]
        for cid, i in issues[:30]:
            L.append(f"- [{cid}] {i.source_file}:{i.row_no} {i.message}")
    L += ["", "다음 단계: 질문 답변 → `python -m taxauto.learn build` 재실행 → `python -m taxauto.learn eval --holdout 0.25 --seed 1` 로 숫자 확인.", ""]
    return "\n".join(L)


def render_status(data_dir: Path, clients_dir: Path, packs_dir: Path) -> str:
    """report: 현재 학습 상태 요약(사례·규칙·질문·최근 평가)."""
    ldir = learn_dir(data_dir)
    ex = load_examples(ldir / "examples.jsonl")
    qs = load_questions(ldir / "questions.yaml")
    L = ["# 스타일 학습 현황", "", f"- 학습 사례 {len(ex)}건 (거래처 {len({e['client_id'] for e in ex})}곳)"]
    L.append(f"- 질문 {len(qs)}개 (미답 {sum(1 for q in qs.values() if not q.answer)})")
    packs = sorted(Path(packs_dir).glob("*.yaml")) if Path(packs_dir).exists() else []
    for p in packs:
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        L.append(f"- 업종팩 {p.stem}: 규칙 {len(d.get('rules') or [])} (거래처 {d.get('clients', '?')}곳, {d.get('built_at', '')})")
    styles = sorted(Path(clients_dir).glob("*/style.yaml")) if Path(clients_dir).exists() else []
    L.append(f"- 거래처 style.yaml {len(styles)}개")
    evals = sorted(ldir.glob("eval_*.json"))
    if evals:
        r = json.loads(evals[-1].read_text(encoding="utf-8"))
        L.append(f"- 최근 평가 {evals[-1].stem}: " + ", ".join(
            f"{FIELD_LABEL.get(f, f)} 정확도 {_pct(st.get('accuracy'))}/커버리지 {_pct(st.get('coverage'))}" for f, st in r["fields"].items()))
    return "\n".join(L) + "\n"

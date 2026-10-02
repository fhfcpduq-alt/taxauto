"""계층별 규칙 추출 + 불일치 질문 + 예측 모델(StyleModel).

계층(우선순위, config/learn/style_learn.yaml miner.levels):
  biz         (거래처 + 가맹점 사업자번호)        → clients/{id}/style.yaml
  name        (거래처 + 정규화 상호)              → clients/{id}/style.yaml
  group_name  (업종그룹 + 정규화 상호)            → config/style/industry/{group}.yaml
  group_mcat  (업종그룹 + 가맹점업종)             → config/style/industry/{group}.yaml
  all_mcat    (전체 + 가맹점업종)                 → config/style/industry/_all.yaml
필드별 분포에서 support ≥ N, 일관성 ≥ p 이면 규칙. 거래처 계층에서 일관성이 낮으면 '세무사 확인 질문'.
공용 계층은 min_clients(기본 2) 이상 거래처에서 나온 키만, 개인 이름형 상호는 제외(개인정보).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from .features import Features, is_person_like, style_config

DISC = {"biz": "biz_no", "name": "name_key", "group_name": "name_key", "group_mcat": "mcat", "all_mcat": "mcat"}
SCOPE_ATTR = {"biz": "client_id", "name": "client_id", "group_name": "group", "group_mcat": "group", "all_mcat": None}
WHEN_KEY = {"biz_no": "biz_no", "name_key": "name", "mcat": "mcat"}
SKIP_ANSWERS = {"거래별", "규칙없음", "skip", "-"}

FIELD_LABEL = {
    "account": "계정과목",
    "entry_type": "매입매출 유형",
    "nd_reason": "불공제 사유",
    "settlement": "분개유형",
    "summary": "적요",
    "fixed_asset": "고정자산 처리",
}


@dataclass
class Level:
    name: str
    scope: str                 # client | industry | all
    min_support: int = 2
    min_consistency: float = 0.8
    min_clients: int = 1


@dataclass
class Rule:
    id: str
    level: str
    scope: str                 # client_id / group / '*'
    direction: str
    doc_type: str              # '' = 문서종류 무관
    disc: str                  # 사업자번호·정규화상호·가맹점업종 값
    field: str
    value: str
    support: int = 0
    consistency: float = 1.0
    n_clients: int = 1
    sample: str = ""
    source: str = "mined"      # mined | answer

    @property
    def key(self) -> tuple:
        return (self.level, self.scope, self.direction, self.doc_type, self.disc, self.field)

    def to_dict(self) -> dict:
        when: dict[str, Any] = {"direction": self.direction}
        if self.doc_type:
            when["doc_type"] = self.doc_type
        when[WHEN_KEY[DISC[self.level]]] = self.disc
        d: dict[str, Any] = {"id": self.id, "level": self.level, "when": when, "field": self.field, "value": self.value,
                             "support": self.support, "consistency": round(self.consistency, 3)}
        if self.n_clients > 1:
            d["clients"] = self.n_clients
        if self.sample:
            d["sample"] = self.sample
        if self.source != "mined":
            d["source"] = self.source
        return d

    @classmethod
    def from_dict(cls, d: dict, scope: str) -> "Rule":
        w = d.get("when") or {}
        lv = str(d["level"])
        return cls(
            id=str(d.get("id", "")), level=lv, scope=scope, direction=str(w.get("direction", "")),
            doc_type=str(w.get("doc_type", "") or ""), disc=str(w.get(WHEN_KEY[DISC[lv]], "")),
            field=str(d["field"]), value=str(d.get("value", "")), support=int(d.get("support", 0)),
            consistency=float(d.get("consistency", 1.0)), n_clients=int(d.get("clients", 1)),
            sample=str(d.get("sample", "") or ""), source=str(d.get("source", "mined")),
        )


@dataclass
class Question:
    id: str
    client_id: str
    level: str
    field: str
    direction: str
    doc_type: str
    disc: str
    merchant: str
    options: list[dict]
    answer: str = ""

    def to_dict(self) -> dict:
        where = f"{self.direction}" + (f"·{self.doc_type}" if self.doc_type else "")
        return {
            "id": self.id,
            "client_id": self.client_id,
            "field": self.field,
            "level": self.level,
            "when": {"direction": self.direction, **({"doc_type": self.doc_type} if self.doc_type else {}),
                     WHEN_KEY[DISC[self.level]]: self.disc},
            "merchant": self.merchant,
            "question": f"[{self.client_id}] '{self.merchant}'({where}) {FIELD_LABEL.get(self.field, self.field)}이(가) 전표마다 다릅니다. 원칙을 골라 주세요.",
            "options": self.options,
            "answer": self.answer,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Question":
        w = d.get("when") or {}
        lv = str(d["level"])
        return cls(
            id=str(d["id"]), client_id=str(d["client_id"]), level=lv, field=str(d["field"]),
            direction=str(w.get("direction", "")), doc_type=str(w.get("doc_type", "") or ""),
            disc=str(w.get(WHEN_KEY[DISC[lv]], "")), merchant=str(d.get("merchant", "")),
            options=list(d.get("options") or []), answer=str(d.get("answer", "") or "").strip(),
        )


@dataclass
class MineResult:
    client_rules: dict[str, list[Rule]] = field(default_factory=dict)
    industry_rules: dict[str, list[Rule]] = field(default_factory=dict)
    all_rules: list[Rule] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    divergent: list[dict] = field(default_factory=list)      # 공용 계층에서 거래처별로 달라 규칙 안 만든 것
    privacy_excluded: int = 0                                 # 개인이름형 상호라 공용에서 뺀 키 수
    single_client_skipped: int = 0                            # 1개 거래처에서만 나와 공용에서 뺀 키 수

    def all(self) -> list[Rule]:
        out = [r for rs in self.client_rules.values() for r in rs]
        out += [r for rs in self.industry_rules.values() for r in rs]
        return out + list(self.all_rules)


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------


def miner_config(cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else style_config()
    return cfg.get("miner") or {}


def levels_of(cfg: dict | None = None) -> list[Level]:
    out = []
    for d in miner_config(cfg).get("levels") or []:
        out.append(Level(name=str(d["name"]), scope=str(d.get("scope", "client")), min_support=int(d.get("min_support", 2)),
                         min_consistency=float(d.get("min_consistency", 0.8)), min_clients=int(d.get("min_clients", 1))))
    return out


def rule_key(level: str, f: dict, field_name: str, by_doc: Iterable[str]) -> tuple | None:
    disc = str(f.get(DISC[level]) or "")
    if not disc:
        return None
    attr = SCOPE_ATTR[level]
    scope = str(f.get(attr) or "") if attr else "*"
    if not scope:
        return None
    doc = str(f.get("doc_type") or "") if field_name in by_doc else ""
    return (level, scope, str(f.get("direction") or ""), doc, disc, field_name)


def _qid(*parts: str) -> str:
    return "q_" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


def display_value(fld: str, v: str) -> str:
    if fld == "account" and "|" in v:
        c, n = v.split("|", 1)
        return f"{c} {n}".strip()
    return v


def normalize_answer(fld: str, ans: str, options: list[dict]) -> str:
    """사람이 적은 답 → 라벨 값. 옵션 표시값('146 원재료비')이나 값 그대로 모두 허용."""
    a = (ans or "").strip()
    for o in options:
        if a in (str(o.get("value", "")), str(o.get("label", ""))):
            return str(o.get("value", ""))
    if fld == "account" and "|" not in a:
        from .ledger_import import parse_account

        c, n = parse_account(a)
        return f"{c}|{n}"
    return a


# ---------------------------------------------------------------------------
# 추출
# ---------------------------------------------------------------------------


def mine(examples: list[dict], cfg: dict | None = None, answers: dict[str, Question] | None = None) -> MineResult:
    """사례 → 규칙·질문. answers: 이전에 답한 질문(id → Question, answer 채워짐)."""
    cfg = cfg if cfg is not None else style_config()
    mcfg = miner_config(cfg)
    fields = list(mcfg.get("fields") or [])
    by_doc = set(mcfg.get("by_doc_type") or [])
    q_min = int(mcfg.get("question_min_support", 3))
    q_fields = set(mcfg.get("question_fields") or fields)
    q_max = int(mcfg.get("max_questions_per_client", 30))
    answers = answers or {}
    res = MineResult()
    seq = 0
    asked: set[tuple] = set()
    q_count: Counter = Counter()

    for lv in levels_of(cfg):
        groups: dict[tuple, Counter] = {}
        clients: dict[tuple, set[str]] = {}
        names: dict[tuple, Counter] = {}
        for ex in examples:
            labels = ex.get("labels") or {}
            for fld in fields:
                v = labels.get(fld)
                if v in (None, ""):
                    continue
                k = rule_key(lv.name, ex, fld, by_doc)
                if k is None:
                    continue
                groups.setdefault(k, Counter())[str(v)] += 1
                clients.setdefault(k, set()).add(str(ex.get("client_id")))
                names.setdefault(k, Counter())[str(ex.get("display") or ex.get("name") or "")] += 1
        for k in sorted(groups):
            cnt = groups[k]
            level, scope, direction, doc, disc, fld = k
            n = sum(cnt.values())
            top_v, top_c = sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[0]
            cons = top_c / n
            sample_names = [x for x, _ in names[k].most_common()]
            sample = sample_names[0] if sample_names else ""
            if lv.scope != "client":
                if len(clients[k]) < lv.min_clients:
                    res.single_client_skipped += 1
                    continue
                if lv.name == "group_name" and any(is_person_like(x, cfg) for x in sample_names):
                    res.privacy_excluded += 1
                    continue
                if lv.name != "group_name":
                    sample = ""  # 업종 키 규칙엔 상호를 남기지 않음
            if n >= lv.min_support and cons >= lv.min_consistency:
                seq += 1
                r = Rule(f"r{seq}", level, scope, direction, doc, disc, fld, top_v, n, cons, len(clients[k]), sample)
                if lv.scope == "client":
                    res.client_rules.setdefault(scope, []).append(r)
                elif lv.scope == "industry":
                    res.industry_rules.setdefault(scope, []).append(r)
                else:
                    res.all_rules.append(r)
                continue
            if lv.scope == "client":
                if n >= q_min and cons < lv.min_consistency and fld in q_fields:
                    dkey = (scope, fld, direction, doc, sample)
                    if dkey in asked:
                        continue
                    asked.add(dkey)
                    qid = _qid(scope, level, direction, doc, disc, fld)
                    opts = [{"value": v, "label": display_value(fld, v), "count": c}
                            for v, c in sorted(cnt.items(), key=lambda x: (-x[1], x[0]))]
                    q = Question(qid, scope, level, fld, direction, doc, disc, sample, opts)
                    prev = answers.get(qid)
                    if prev and prev.answer:
                        q.answer = prev.answer
                    if q.answer or q_count[scope] < q_max:
                        res.questions.append(q)
                        if not q.answer:
                            q_count[scope] += 1
            elif n >= lv.min_support:
                res.divergent.append({"level": level, "scope": scope, "direction": direction, "doc_type": doc,
                                      "key": disc if lv.name != "group_name" else sample, "field": fld,
                                      "dist": {display_value(fld, v): c for v, c in cnt.most_common(4)},
                                      "clients": len(clients[k])})

    # 답변 → 규칙(최우선 같은 계층 키)
    for q in res.questions:
        if not q.answer or q.answer in SKIP_ANSWERS:
            continue
        seq += 1
        val = normalize_answer(q.field, q.answer, q.options)
        n = sum(int(o.get("count", 0)) for o in q.options)
        res.client_rules.setdefault(q.client_id, []).append(
            Rule(f"a{seq}", q.level, q.client_id, q.direction, q.doc_type, q.disc, q.field, val, n, 1.0, 1, q.merchant, "answer"))
    return res


# ---------------------------------------------------------------------------
# 예측
# ---------------------------------------------------------------------------


@dataclass
class Prediction:
    value: str
    rule: Rule
    origin: str            # 'memory:C001' | 'industry:restaurant' | 'industry:_all'

    @property
    def source(self) -> str:
        return f"{self.origin}#{self.rule.id}"


class StyleModel:
    """규칙 묶음 → 필드별 예측. 계층 우선순위는 설정 순서."""

    def __init__(self, rules: list[tuple[Rule, str]], cfg: dict | None = None):
        cfg = cfg if cfg is not None else style_config()
        mcfg = miner_config(cfg)
        self.fields = list(mcfg.get("fields") or [])
        self.by_doc = set(mcfg.get("by_doc_type") or [])
        self.levels = [lv.name for lv in levels_of(cfg)]
        self.index: dict[tuple, tuple[Rule, str]] = {}
        for r, origin in rules:
            prev = self.index.get(r.key)
            if prev is None or (r.source == "answer" and prev[0].source != "answer"):
                self.index[r.key] = (r, origin)

    def __len__(self) -> int:
        return len(self.index)

    @classmethod
    def from_mine(cls, m: MineResult, cfg: dict | None = None, include_client: bool = True) -> "StyleModel":
        rules: list[tuple[Rule, str]] = []
        if include_client:
            for cid, rs in m.client_rules.items():
                rules += [(r, f"memory:{cid}") for r in rs]
        for g, rs in m.industry_rules.items():
            rules += [(r, f"industry:{g}") for r in rs]
        rules += [(r, "industry:_all") for r in m.all_rules]
        return cls(rules, cfg)

    def predict(self, f: Features | dict) -> dict[str, Prediction]:
        fd = f.to_dict() if isinstance(f, Features) else f
        out: dict[str, Prediction] = {}
        for fld in self.fields:
            for lv in self.levels:
                k = rule_key(lv, fd, fld, self.by_doc)
                if k is None:
                    continue
                hit = self.index.get(k)
                if hit:
                    out[fld] = Prediction(hit[0].value, hit[0], hit[1])
                    break
        return out

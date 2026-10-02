"""학습·적용 공통 특징: 상호 정규화, 업종그룹, 가맹점업종, 금액구간.

설정: config/learn/style_learn.yaml (names, merchant_categories, amount_bands)
      config/style/industry_groups.yaml
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..law import CONFIG_DIR
from ..models import Client, Transaction

_CORP_RE = re.compile(r"(\(주\)|㈜|\(유\)|\(사\)|\(재\)|\(합\)|주식회사|유한회사|유한책임회사|합자회사|합명회사|사단법인|재단법인)")
_PAREN_RE = re.compile(r"\(.*?\)|\[.*?\]|（.*?）")
_HANGUL = re.compile(r"[가-힣]")
_DIGITS = re.compile(r"\d{6,}")


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict:
    if not Path(path).exists():
        return {}
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=8)
def _style_cfg(config_dir: str) -> dict:
    return load_yaml(Path(config_dir) / "learn" / "style_learn.yaml")


@lru_cache(maxsize=8)
def _group_cfg(config_dir: str) -> dict:
    return load_yaml(Path(config_dir) / "style" / "industry_groups.yaml")


def style_config(config_dir: Path | None = None) -> dict:
    return _style_cfg(str(config_dir or CONFIG_DIR))


def group_config(config_dir: Path | None = None) -> dict:
    return _group_cfg(str(config_dir or CONFIG_DIR))


# ---------------------------------------------------------------------------
# 상호
# ---------------------------------------------------------------------------


def compact(s: Any) -> str:
    """비교용: 소문자, 한글·영문·숫자만."""
    return re.sub(r"[^0-9a-z가-힣]", "", str(s or "").lower())


def clean_name(name: str) -> str:
    """표시용: 법인격·괄호 표기 제거, 공백 정리. (지점명은 유지)"""
    s = _CORP_RE.sub(" ", str(name or ""))
    s = _PAREN_RE.sub(" ", s)
    return " ".join(s.split())


def _brands(cfg: dict) -> list[str]:
    b = [compact(x) for x in (cfg.get("names") or {}).get("brands") or []]
    return sorted({x for x in b if x}, key=len, reverse=True)


def display_name(name: str, cfg: dict | None = None) -> str:
    """대표 상호(표시용): 법인격 제거 + 공백 뒤 지점 토큰 제거."""
    cfg = cfg if cfg is not None else style_config()
    ncfg = cfg.get("names") or {}
    s = clean_name(name)
    toks = s.split()
    rx = re.compile(ncfg.get("branch_regex") or r"^[가-힣A-Za-z0-9]{1,12}점$")
    exc = set(ncfg.get("branch_exceptions") or [])
    while len(toks) > 1 and rx.match(toks[-1]) and not any(toks[-1].endswith(e) for e in exc):
        toks.pop()
    return " ".join(toks)


def normalize_name(name: str, cfg: dict | None = None) -> str:
    """규칙 키용 정규화 상호: 법인격·지점명·공백·특수문자 제거, 대표 브랜드로 묶음."""
    cfg = cfg if cfg is not None else style_config()
    c = compact(display_name(name, cfg))
    if not c:
        return ""
    for b in _brands(cfg):
        if c == b:
            return b
        if c.startswith(b):
            rest = c[len(b):]
            # 짧은 브랜드(cu, kt)는 뒤가 한글(지점명)일 때만 인정
            if len(b) >= 3 or _HANGUL.match(rest[:1] or "") or rest[:1].isdigit():
                return b
    return c


def is_person_like(name: str, cfg: dict | None = None) -> bool:
    """개인 이름처럼 보이는 상호(한글 2~4자, 업종 꼬리말 없음) → 공용 규칙·LLM 전송에서 제외."""
    cfg = cfg if cfg is not None else style_config()
    ncfg = cfg.get("names") or {}
    n = clean_name(name).replace(" ", "")
    if not re.match(ncfg.get("person_name_regex") or r"^[가-힣]{2,4}$", n):
        return False
    return not any(n.endswith(s) for s in ncfg.get("business_suffixes") or [])


def mask_name(name: str, cfg: dict | None = None) -> str:
    """외부 전송용 상호: 개인 이름형은 가리고 긴 숫자 제거."""
    if is_person_like(name, cfg):
        return "(개인명 마스킹)"
    return _DIGITS.sub("***", clean_name(name))


# ---------------------------------------------------------------------------
# 업종
# ---------------------------------------------------------------------------


def merchant_category(name: str, raw_category: str = "", cfg: dict | None = None) -> str:
    """가맹점업종 표준화: 원천 업종 문구 → 별칭 매핑, 없으면 상호 키워드로 추정, 그래도 없으면 ''."""
    cfg = cfg if cfg is not None else style_config()
    raw = compact(raw_category)
    if raw:
        for cat, aliases in (cfg.get("merchant_category_aliases") or {}).items():
            if any(compact(a) and compact(a) in raw for a in aliases or []):
                return cat
    n = compact(display_name(name, cfg))
    if n:
        for cat, kws in (cfg.get("merchant_categories") or {}).items():
            if any(compact(k) and compact(k) in n for k in kws or []):
                return cat
    return raw_category.strip() if raw else ""


def industry_group(industry: str = "", industry_code: str = "", gcfg: dict | None = None) -> str:
    """거래처 업종그룹: 업종코드 앞자리 → 업태·종목 키워드 → default."""
    gcfg = gcfg if gcfg is not None else group_config()
    groups = gcfg.get("groups") or {}
    code = re.sub(r"\D", "", str(industry_code or ""))
    if code:
        best = ("", 0)
        for g, d in groups.items():
            for p in (d or {}).get("code_prefixes") or []:
                p = str(p)
                if code.startswith(p) and len(p) > best[1]:
                    best = (g, len(p))
        if best[0]:
            return best[0]
    text = compact(industry)
    if text:
        for g, d in groups.items():
            if any(compact(k) and compact(k) in text for k in (d or {}).get("keywords") or []):
                return g
    return str(gcfg.get("default") or "other")


def client_group(client: Client | dict, gcfg: dict | None = None) -> str:
    if isinstance(client, dict):
        if client.get("industry_group"):
            return str(client["industry_group"])
        return industry_group(client.get("industry", ""), client.get("industry_code", ""), gcfg)
    return industry_group(client.industry, client.industry_code, gcfg)


def amount_band(amount: int, cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else style_config()
    bands = sorted(int(x) for x in cfg.get("amount_bands") or [0])
    a = abs(int(amount or 0))
    lo = bands[0]
    for b in bands:
        if a >= b:
            lo = b
    idx = bands.index(lo)
    hi = bands[idx + 1] if idx + 1 < len(bands) else None

    def w(x: int) -> str:
        return f"{x // 10000}만" if x >= 10000 else str(x)

    return f"{w(lo)}~{w(hi)}" if hi is not None else f"{w(lo)}~"


# ---------------------------------------------------------------------------
# 특징 묶음
# ---------------------------------------------------------------------------


@dataclass
class Features:
    client_id: str
    group: str
    direction: str            # Direction.value (매출/매입)
    doc_type: str             # DocType.value
    biz_no: str
    name: str                 # 원래 상호(표시용, 법인격 제거)
    name_key: str             # 정규화 상호
    display: str              # 대표 상호(지점 제거)
    mcat: str                 # 표준 가맹점업종
    supply_amount: int
    total: int
    band: str
    item: str
    month: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Features":
        return cls(**{k: d.get(k, "" if k not in ("supply_amount", "total", "month") else 0) for k in cls.__dataclass_fields__})


def make_features(
    client_id: str,
    group: str,
    direction: str,
    doc_type: str,
    biz_no: str,
    name: str,
    raw_category: str,
    supply_amount: int,
    total: int,
    item: str,
    month: int,
    cfg: dict | None = None,
) -> Features:
    cfg = cfg if cfg is not None else style_config()
    return Features(
        client_id=str(client_id),
        group=group,
        direction=direction,
        doc_type=doc_type,
        biz_no=biz_no or "",
        name=clean_name(name),
        name_key=normalize_name(name, cfg),
        display=display_name(name, cfg),
        mcat=merchant_category(name, raw_category, cfg),
        supply_amount=int(supply_amount or 0),
        total=int(total or 0),
        band=amount_band(supply_amount, cfg),
        item=str(item or "").strip(),
        month=int(month or 0),
    )


def features_from_transaction(t: Transaction, group: str, cfg: dict | None = None) -> Features:
    return make_features(
        t.client_id, group, t.direction.value, t.doc_type.value, t.counterparty_biz_no, t.counterparty_name,
        t.merchant_category, t.supply_amount, t.total, t.item, t.tx_date.month, cfg,
    )


# ---------------------------------------------------------------------------
# 적요 템플릿
# ---------------------------------------------------------------------------


def summary_template(summary: str, f: Features) -> str:
    """적요 → 템플릿: 상호·품목·월을 자리표시자로."""
    s = str(summary or "").strip()
    if not s:
        return ""
    if f.name and f.display and f.name != f.display and f.name in s:
        s = s.replace(f.name, "{거래처명}")
    elif f.display and len(f.display) >= 2 and f.display in s:
        s = s.replace(f.display, "{상호}")
    if f.item and len(f.item) >= 2 and f.item in s:
        s = s.replace(f.item, "{품목}")
    if f.month:
        s = re.sub(rf"(?<!\d)0?{f.month}월", "{월}월", s)
    return s


def render_summary(template: str, f: Features) -> str:
    if not template:
        return ""
    out = template.replace("{거래처명}", f.name or f.display).replace("{상호}", f.display or f.name)
    out = out.replace("{품목}", f.item or "").replace("{월}", str(f.month or ""))
    return " ".join(out.split())


def name_similarity(a: str, b: str) -> float:
    """문자 2-gram Jaccard (few-shot 유사사례 검색용)."""
    a, b = compact(a), compact(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga = {a[i : i + 2] for i in range(max(1, len(a) - 1))}
    gb = {b[i : i + 2] for i in range(max(1, len(b) - 1))}
    return len(ga & gb) / len(ga | gb)

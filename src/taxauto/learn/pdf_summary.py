"""세무사 '매출/매입 요약 보고서' PDF 파서 + 엔진 신고서와 대사.

실제 양식 미확인 → '라벨 근처 숫자' 휴리스틱 + config/learn/pdf_summary.yaml 라벨 별칭.
  parse_summary_pdf(path) -> dict          (표 행 → 텍스트 줄 순으로, 먼저 찾은 값 우선)
  save_summary(dict, data_dir, period, client_id) -> data/{period}/{client}/summary_pdf.json
  compare_summary(pdf_dict, VatReturn) -> list[ReviewItem]  (코드 V030_PDF_SUMMARY_MISMATCH)
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..ingest import excel
from ..law import CONFIG_DIR
from ..models import Line, ReviewItem, Severity, VatReturn, normalize_biz_no
from .features import load_yaml

REVIEW_CODE = "V030_PDF_SUMMARY_MISMATCH"

_NUMBERING = re.compile(r"^\s*(\(\d{1,2}\)|\d{1,2}[.)]|[①-⑳]|[가-하][.)]|[IVX]+\.)\s*")
_NUM_TOKEN = re.compile(r"^[△▲\-−(]?\d[\d,]*\)?-?(원)?$")
_BIZ = re.compile(r"(?<!\d)(\d{3})-?(\d{2})-?(\d{5})(?!\d)")
_PERIOD = re.compile(r"(20\d{2})\s*년?\s*([12])\s*기\s*(확정|예정)?")
_RANGE = re.compile(r"(20\d{2})[.\-/년\s]*(\d{1,2})[.\-/월\s]*(\d{1,2})일?\s*[~∼\-]\s*(20\d{2})[.\-/년\s]*(\d{1,2})[.\-/월\s]*(\d{1,2})")


@lru_cache(maxsize=4)
def _cfg(config_dir: str) -> dict:
    return load_yaml(Path(config_dir) / "learn" / "pdf_summary.yaml")


def summary_config(config_dir: Path | None = None) -> dict:
    return _cfg(str(config_dir or CONFIG_DIR))


def _n(s: Any) -> str:
    return re.sub(r"[\s·ㆍ:：()\[\]【】<>〈〉]", "", str(s or "")).lower()


# ---------------------------------------------------------------------------
# 줄 해석
# ---------------------------------------------------------------------------


def _split_label(line: str) -> tuple[str, list[str]]:
    """'1. 세금계산서  12  1,000,000  100,000' → ('세금계산서', ['12','1,000,000','100,000'])."""
    s = _NUMBERING.sub("", line.strip())
    toks = s.split()
    label: list[str] = []
    nums: list[str] = []
    for i, tk in enumerate(toks):
        if _NUM_TOKEN.match(tk):
            nums = [t for t in toks[i:] if _NUM_TOKEN.match(t)]
            break
        label.append(tk)
    return " ".join(label), nums


def _parse_num(tok: str) -> int | None:
    try:
        return excel.parse_amount(tok.replace("원", ""))
    except ValueError:
        return None


class _Parser:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sections = {k: sorted((_n(x) for x in v or []), key=len, reverse=True) for k, v in (cfg.get("sections") or {}).items()}
        self.items = cfg.get("items") or {}
        self.col_hdr = {k: [_n(x) for x in v or []] for k, v in (cfg.get("column_headers") or {}).items()}
        self.single = cfg.get("single_value_as") or {}
        self.section = ""
        self.order: list[str] = []
        self.values: dict[str, dict[str, int]] = {}

    def _strip_section(self, lab: str) -> str:
        for sec, words in self.sections.items():
            for w in words:
                if w and lab.startswith(w):
                    self.section = sec
                    return lab[len(w):]
        return lab

    def _header_order(self, line: str) -> list[str] | None:
        if any(_NUM_TOKEN.match(t) for t in line.split()):
            return None
        toks = [_n(t) for t in line.split()]
        order = []
        for t in toks:
            for kind, words in self.col_hdr.items():
                if t in words:
                    order.append(kind)
                    break
        return order if len(order) >= 2 else None

    def _match_item(self, lab: str) -> str | None:
        best: tuple[int, str] | None = None
        for key, d in self.items.items():
            sec = (d or {}).get("section", "any")
            if sec != "any" and sec != self.section:
                continue
            for a in (d or {}).get("labels") or []:
                na = _n(a)
                if not na:
                    continue
                if lab == na:
                    return key
                if lab.endswith(na) and (best is None or len(na) > best[0]):
                    best = (len(na), key)
        return best[1] if best else None

    def feed(self, line: str) -> None:
        if not line or not line.strip():
            return
        order = self._header_order(line)
        if order:
            self.order = order
            self._strip_section(_n(_split_label(line)[0]))
            return
        label, nums = _split_label(line)
        lab = _n(label)
        if not lab:
            return
        rest = self._strip_section(lab)
        if not nums:
            return
        key = self._match_item(rest) if rest else None
        if key is None and rest:  # '세금계산서 공급가액 1,000 세액 100' 처럼 열이름이 섞인 줄
            stripped = rest
            for w in sorted((w for ws in self.col_hdr.values() for w in ws), key=len, reverse=True):
                stripped = stripped.replace(w, "")
            key = self._match_item(stripped) if stripped else None
        if key is None or key in self.values:
            return
        vals = [v for v in (_parse_num(t) for t in nums) if v is not None]
        if not vals:
            return
        out: dict[str, int] = {}
        if self.order and len(vals) == len(self.order):
            out = dict(zip(self.order, vals))
        elif len(vals) >= 3:
            out = {"count": vals[0], "amount": vals[1], "tax": vals[2]}
        elif len(vals) == 2:
            out = {"amount": vals[0], "tax": vals[1]}
        else:
            out = {self.single.get(key, "amount"): vals[0]}
        self.values[key] = out


def _text_field(lines: list[str], aliases: list[str]) -> str:
    for ln in lines:
        for a in sorted(aliases, key=len, reverse=True):
            m = re.search(rf"{re.escape(a)}\s*[:：]?\s*(.+)", ln)
            if m:
                v = re.split(r"\s{2,}|\s(?:사업자|등록번호|기간|대표|과세기간|신고기간)", m.group(1).strip())[0].strip()
                if v:
                    return v
    return ""


def parse_summary_lines(lines: list[str], cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else summary_config()
    p = _Parser(cfg)
    for ln in lines:
        p.feed(ln)
    text = "\n".join(lines)
    out: dict[str, Any] = {"sales": {}, "purchase": {}}
    for key, v in p.values.items():
        if "." in key:
            sec, item = key.split(".", 1)
            out.setdefault(sec, {})[item] = v
        else:
            out[key] = v
    tf = cfg.get("text_fields") or {}
    out["company_name"] = _text_field(lines, tf.get("company_name") or [])
    m = _BIZ.search(text)
    out["biz_no"] = "".join(m.groups()) if m else ""
    pm = _PERIOD.search(text)
    out["period"] = ""
    if pm:
        kind = "P" if pm.group(3) == "예정" else "F" if pm.group(3) == "확정" else ""
        out["period"] = f"{pm.group(1)}-{pm.group(2)}{kind}" if kind else f"{pm.group(1)}-{pm.group(2)}"
    rm = _RANGE.search(text)
    out["period_range"] = (
        [f"{rm.group(1)}-{int(rm.group(2)):02d}-{int(rm.group(3)):02d}", f"{rm.group(4)}-{int(rm.group(5)):02d}-{int(rm.group(6)):02d}"]
        if rm else []
    )
    out["period_text"] = _text_field(lines, tf.get("period") or [])
    return out


def parse_summary_pdf(path: Path, config_dir: Path | None = None) -> dict:
    """PDF → 요약 dict. 표 행을 먼저, 그다음 텍스트 줄. 읽기 실패 시 warnings 에 기록."""
    cfg = summary_config(config_dir)
    lines: list[str] = []
    warnings: list[str] = []
    try:
        import pdfplumber
    except ImportError:
        return {"source_file": Path(path).name, "sales": {}, "purchase": {}, "warnings": ["pdfplumber 미설치"]}
    try:
        with pdfplumber.open(str(path)) as pdf:
            table_lines: list[str] = []
            text_lines: list[str] = []
            for page in pdf.pages:
                for tb in page.extract_tables() or []:
                    for row in tb:
                        cells = [" ".join(str(c).split()) for c in row if c not in (None, "")]
                        if cells:
                            table_lines.append("  ".join(cells))
                text_lines.extend((page.extract_text() or "").splitlines())
            lines = table_lines + text_lines
    except Exception as e:  # 손상·암호 PDF 등
        warnings.append(f"PDF 읽기 실패({type(e).__name__})")
    out = parse_summary_lines(lines, cfg)
    if lines and not (out.get("sales") or out.get("purchase")):
        warnings.append("매출/매입 항목을 찾지 못함 - config/learn/pdf_summary.yaml 라벨 별칭 추가 필요(스캔 PDF면 OCR 필요)")
    out["source_file"] = Path(path).name
    out["warnings"] = warnings
    return out


def save_summary(summary: dict, data_dir: Path, period: str, client_id: str) -> Path:
    p = Path(data_dir) / period / client_id / "summary_pdf.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


# ---------------------------------------------------------------------------
# 대사
# ---------------------------------------------------------------------------


def _pdf_value(summary: dict, key: str) -> dict[str, int] | None:
    if "." in key:
        sec, item = key.split(".", 1)
        return (summary.get(sec) or {}).get(item)
    v = summary.get(key)
    return v if isinstance(v, dict) else None


def compare_summary(
    summary: dict,
    ret: VatReturn,
    client_id: str | None = None,
    period: str | None = None,
    client_biz_no: str = "",
    config_dir: Path | None = None,
) -> list[ReviewItem]:
    """요약 PDF 값과 엔진 독립 재계산 신고서를 항목별로 비교 → 불일치 ReviewItem(경고)."""
    cfg = summary_config(config_dir)
    tol = int(cfg.get("tolerance", 0) or 0)
    cid = client_id or ret.client_id
    per = period or ret.period
    items: list[ReviewItem] = []
    src = summary.get("source_file", "요약 PDF")
    if client_biz_no and summary.get("biz_no") and normalize_biz_no(client_biz_no) != summary["biz_no"]:
        items.append(ReviewItem(cid, per, REVIEW_CODE, Severity.WARN, "요약 PDF 사업자번호가 거래처와 다름",
                                detail=f"{src}: 다른 거래처 PDF일 수 있음", suggested_action="PDF 파일 위치 확인"))
        return items
    for spec in cfg.get("compare") or []:
        vals = [v for v in (_pdf_value(summary, k) for k in spec.get("pdf") or []) if v]
        if not vals:
            continue
        pdf = {f: sum(int(v.get(f, 0)) for v in vals) for f in ("amount", "tax") if any(f in v for v in vals)}
        eng = {"amount": 0, "tax": 0}
        if spec.get("card_receipt_summary"):
            for lv in ret.card_receipt_summary.values():
                eng["amount"] += lv.amount
                eng["tax"] += lv.tax
        for ln in spec.get("lines") or []:
            lv = ret.lines.get(str(ln))
            if lv is None and str(ln) in Line.__members__:
                lv = ret.lines.get(Line[str(ln)].name)
            if lv is not None:
                eng["amount"] += lv.amount
                eng["tax"] += lv.tax
        fields = spec.get("fields") or ["amount", "tax"]
        diffs = []
        for f in fields:
            if f not in pdf:
                continue
            if abs(pdf[f] - eng[f]) > tol:
                name = "금액" if f == "amount" else "세액"
                diffs.append(f"{name}: PDF {pdf[f]:,} / 엔진 {eng[f]:,} (차 {pdf[f] - eng[f]:+,})")
        if diffs:
            items.append(ReviewItem(
                cid, per, REVIEW_CODE, Severity.WARN,
                f"세무사 요약 PDF와 신고서 불일치: {spec.get('label', '/'.join(spec.get('pdf') or []))}",
                detail=f"{src} - " + "; ".join(diffs),
                suggested_action="누락·중복 전표 또는 공제 판정 차이 확인(PDF가 세무사 확정본이면 엔진 쪽 원인 추적)",
            ))
    return items

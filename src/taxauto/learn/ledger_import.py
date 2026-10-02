"""위하고 매입매출전표 내보내기 엑셀 → LedgerEntry (세무사 전표 스타일 학습용).

헤더·코드표는 config/learn/wehago_ledger.yaml (별칭, 실제 양식 미확인 → '추정').
엑셀 읽기·헤더 탐지·날짜/금액 파싱은 taxauto.ingest.excel 재사용.
카드번호는 뒤 4자리만, 주민번호형 거래처번호는 버린다(개인정보).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..ingest import excel
from ..law import CONFIG_DIR
from ..models import Direction, DocType, ParseIssue, Severity, Source, Transaction, normalize_biz_no
from ..redact import redact
from .features import load_yaml

_CODE_NAME_RE = re.compile(r"^\s*(\d{1,4})\s*[.\-:)\s]?\s*([^\d].*)?$")
_NAME_CODE_RE = re.compile(r"^\s*([^\d(]+?)\s*[(\[]\s*(\d{1,4})\s*[)\]]\s*$")
_YEAR_RE = re.compile(r"(20\d{2})\s*[년.\-/]")


@lru_cache(maxsize=4)
def _cfg_cached(config_dir: str) -> dict:
    return load_yaml(Path(config_dir) / "learn" / "wehago_ledger.yaml")


def ledger_config(config_dir: Path | None = None) -> dict:
    return _cfg_cached(str(config_dir or CONFIG_DIR))


@dataclass
class LedgerEntry:
    client_id: str
    tx_date: date
    direction: str                 # Direction.value
    entry_type: str                # 표준 유형명(과세·카과·불공…)
    entry_code: str = ""           # 유형코드(있으면)
    doc_type: str = DocType.NONE.value
    supply_amount: int = 0
    vat: int = 0
    total: int = 0
    item: str = ""
    counterparty_code: str = ""
    counterparty_name: str = ""
    counterparty_biz_no: str = ""
    electronic: bool | None = None
    settlement: str = ""
    account_code: str = ""
    account_name: str = ""
    summary: str = ""
    nd_reason: str = ""            # NonDeductibleReason.value 또는 ''
    nd_reason_raw: str = ""
    card_company: str = ""
    card_no_masked: str = ""
    deemed: bool | None = None
    fixed_asset: bool | None = None
    zero_rated: bool = False
    approval_no: str = ""
    source_file: str = ""
    row_no: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tx_date"] = self.tx_date.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LedgerEntry":
        d = dict(d)
        d["tx_date"] = date.fromisoformat(str(d["tx_date"])[:10])
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def account(self) -> str:
        """학습 라벨용 계정 키 'code|name'."""
        if not (self.account_code or self.account_name):
            return ""
        return f"{self.account_code}|{self.account_name}"

    def to_transaction(self) -> Transaction:
        return Transaction(
            client_id=self.client_id,
            source=Source.WEHAGO_LEDGER,
            direction=Direction(self.direction),
            doc_type=DocType(self.doc_type),
            tx_date=self.tx_date,
            supply_amount=self.supply_amount,
            vat=self.vat,
            total=self.total,
            approval_no=self.approval_no,
            counterparty_biz_no=self.counterparty_biz_no,
            counterparty_name=self.counterparty_name,
            item=self.item,
            card_no_masked=self.card_no_masked,
            zero_rated=self.zero_rated,
            source_file=self.source_file,
            row_no=self.row_no,
        )


@dataclass
class LedgerParseResult:
    entries: list[LedgerEntry] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    is_ledger: bool = False


# ---------------------------------------------------------------------------
# 값 해석
# ---------------------------------------------------------------------------


def split_code_name(v: Any) -> tuple[str, str]:
    """'51.과세' / '830 소모품비' / '소모품비(830)' / '830' / '과세' → (code, name)."""
    s = excel.cell_text(v)
    if not s:
        return "", ""
    m = _NAME_CODE_RE.match(s)
    if m:
        return m.group(2), m.group(1).strip()
    m = _CODE_NAME_RE.match(s)
    if m:
        return m.group(1), (m.group(2) or "").strip(" .")
    return "", s.strip()


def _alias_lookup(text: str, table: dict) -> str:
    t = excel.norm_text(text)
    if not t:
        return ""
    for std, aliases in (table or {}).items():
        for a in aliases or []:
            if excel.norm_text(a) == t:
                return std
    for std, aliases in (table or {}).items():  # 부분 일치(예: '카과(신용카드)')
        for a in aliases or []:
            a2 = excel.norm_text(a)
            if a2 and len(a2) >= 2 and a2 in t:
                return std
    return ""


def _truthy(v: Any, cfg: dict) -> bool | None:
    s = excel.cell_text(v)
    if s == "":
        return None
    if s in [str(x) for x in cfg.get("truthy") or []]:
        return True
    if s in [str(x) for x in cfg.get("falsy") or []]:
        return False
    return True  # 무언가 적혀 있으면 표시된 것으로 봄


def resolve_entry_type(
    code_text: Any, name_text: Any, direction_text: Any, account_code: str, cfg: dict
) -> tuple[str, str, str | None, str, bool, bool]:
    """→ (code, name, direction|None, doc_type, zero_rated, non_deductible)."""
    types: dict = {str(k): v for k, v in (cfg.get("entry_types") or {}).items()}
    code, name = split_code_name(code_text)
    if not code and not name:
        code, name = split_code_name(name_text)
    elif not name:
        _, name = split_code_name(name_text)
    if code and code.zfill(2) in types:
        t = types[code.zfill(2)]
        return code.zfill(2), t["name"], t["direction"], t.get("doc_type", DocType.NONE.value), bool(t.get("zero_rated")), bool(t.get("non_deductible"))
    std = _alias_lookup(name, cfg.get("entry_name_aliases") or {}) or name
    if not std:
        return "", "", None, DocType.NONE.value, False, False
    direction: str | None = None
    dt = excel.norm_text(direction_text)
    if dt:
        if "매출" in dt:
            direction = Direction.SALES.value
        elif "매입" in dt:
            direction = Direction.PURCHASE.value
    if direction is None and std in (cfg.get("purchase_only_names") or []):
        direction = Direction.PURCHASE.value
    if direction is None and account_code:
        pref = [str(p) for p in cfg.get("sales_account_prefixes") or []]
        direction = Direction.SALES.value if any(account_code.startswith(p) for p in pref) else Direction.PURCHASE.value
    cands = [(c, t) for c, t in types.items() if t["name"] == std and (direction is None or t["direction"] == direction)]
    if not cands:
        return "", std, direction, DocType.NONE.value, False, False
    if direction is None and len({t["direction"] for _, t in cands}) == 1:
        direction = cands[0][1]["direction"]
    c, t = cands[0]
    code_out = c if direction is not None else ""
    return code_out, std, direction, t.get("doc_type", DocType.NONE.value), bool(t.get("zero_rated")), bool(t.get("non_deductible"))


def resolve_settlement(v: Any, cfg: dict) -> str:
    code, name = split_code_name(v)
    if code and not name:
        return str((cfg.get("settlements") or {}).get(code, (cfg.get("settlements") or {}).get(str(int(code)), "")) or "")
    return _alias_lookup(name, cfg.get("settlement_aliases") or {}) or name


def resolve_nd_reason(v: Any, cfg: dict) -> str:
    code, name = split_code_name(v)
    table = {str(k): str(x) for k, x in (cfg.get("nd_reasons") or {}).items()}
    if code and code in table:
        return table[code]
    t = excel.norm_text(name)
    if not t:
        return ""
    for reason, kws in (cfg.get("nd_reason_keywords") or {}).items():
        if any(excel.norm_text(k) in t for k in kws or []):
            return str(reason)
    return "기타"


def parse_account(v: Any) -> tuple[str, str]:
    """계정과목 셀 → (code, name). '146 원재료비' / '원재료비(146)' / '146' / '원재료비'."""
    return split_code_name(v)


# ---------------------------------------------------------------------------
# 파일 파싱
# ---------------------------------------------------------------------------


def _get(row: list, cols: dict, k: str) -> Any:
    i = cols.get(k)
    return row[i] if i is not None and i < len(row) else None


def find_ledger_header(rows: list[list[Any]], cfg: dict) -> excel.HeaderMatch | None:
    fields = cfg.get("fields") or {}
    det = cfg.get("detect") or {}
    hm = excel.find_header(rows, fields, min_fields=int(det.get("min_fields", 4)))
    if hm is None:
        return None
    cols = hm.columns
    if not any(k in cols for k in det.get("required_any") or []):
        return None
    if not any(k in cols for k in det.get("required_amount_any") or []):
        return None
    return hm


def parse_ledger_rows(
    rows: list[list[Any]], client_id: str, file_name: str, cfg: dict, default_year: int | None = None
) -> LedgerParseResult:
    res = LedgerParseResult()
    hm = find_ledger_header(rows, cfg)
    if hm is None:
        return res
    res.is_ledger = True
    cols = hm.columns
    m = _YEAR_RE.search(hm.title_text or "") or _YEAR_RE.search(file_name)
    year = default_year or (int(m.group(1)) if m else None)
    known = {excel.norm_header(h) for h in hm.headers}
    markers = [excel.norm_text(x) for x in cfg.get("summary_row_markers") or []]
    fa_lo, fa_hi = (list(cfg.get("fixed_asset_account_range") or [0, -1]) + [0, -1])[:2]
    last_date: date | None = None
    for i in range(hm.data_start, len(rows)):
        row = rows[i]
        rno = i + 1
        if not row or not any(c is not None and str(c).strip() for c in row):
            continue
        if excel.is_repeated_header(row, known):
            continue
        texts = [excel.norm_text(c) for c in row[:4] if c is not None and str(c).strip()]
        if texts and texts[0].strip("[]<>()") in markers:
            continue  # 합계·월계 행
        try:
            d = None
            if "tx_date" in cols:
                d = excel.parse_date(_get(row, cols, "tx_date"), year)
            if d is None and "day" in cols:
                mo = _get(row, cols, "month")
                dy = _get(row, cols, "day")
                if mo not in (None, "") and dy not in (None, "") and year:
                    d = date(year, int(excel.parse_amount(mo)), int(excel.parse_amount(dy)))
            if d is None:
                d = last_date  # 위하고 화면처럼 같은 일자 반복 생략된 경우
            if d is None:
                raise ValueError("일자 없음")
            last_date = d
            acode, aname = "", ""
            if "account_code" in cols or "account_name" in cols:
                acode = split_code_name(_get(row, cols, "account_code"))[0]
                aname = excel.cell_text(_get(row, cols, "account_name"))
            if "account" in cols:
                c2, n2 = parse_account(_get(row, cols, "account"))
                acode, aname = acode or c2, aname or n2
            code, name, direction, doc_type, zr, nd = resolve_entry_type(
                _get(row, cols, "entry_code") if "entry_code" in cols else _get(row, cols, "entry_type"),
                _get(row, cols, "entry_name") if "entry_name" in cols else _get(row, cols, "entry_type"),
                _get(row, cols, "direction_text"), acode, cfg,
            )
            if not name:
                raise ValueError("유형 해석 불가")
            if direction is None:
                raise ValueError(f"매입/매출 구분 불가(유형 '{name}', 구분열·계정코드 없음)")
            supply = excel.parse_amount(_get(row, cols, "supply_amount"))
            vat = excel.parse_amount(_get(row, cols, "vat"))
            total = excel.parse_amount(_get(row, cols, "total"))
            if not total:
                total = supply + vat
            if not supply and total:
                supply = total - vat
            if not (supply or vat or total):
                continue
            biz_raw = excel.cell_text(_get(row, cols, "counterparty_biz_no"))
            biz = normalize_biz_no(biz_raw)
            if len(biz) != 10:   # 주민번호·외국인번호 등은 보관하지 않음
                biz = ""
            ndr_raw = excel.cell_text(_get(row, cols, "nd_reason"))
            ndr = resolve_nd_reason(ndr_raw, cfg) if ndr_raw else ""
            if nd and not ndr:
                ndr = "기타"
            fixed = _truthy(_get(row, cols, "fixed_asset"), cfg) if "fixed_asset" in cols else None
            if acode.isdigit() and int(fa_lo) <= int(acode) <= int(fa_hi):
                fixed = True
            e = LedgerEntry(
                client_id=str(client_id),
                tx_date=d,
                direction=direction,
                entry_type=name,
                entry_code=code,
                doc_type=doc_type,
                supply_amount=supply,
                vat=vat,
                total=total,
                item=redact(excel.cell_text(_get(row, cols, "item"))),
                counterparty_code=excel.cell_text(_get(row, cols, "counterparty_code")),
                counterparty_name=redact(excel.cell_text(_get(row, cols, "counterparty_name"))),
                counterparty_biz_no=biz,
                electronic=_truthy(_get(row, cols, "electronic"), cfg) if "electronic" in cols else None,
                settlement=resolve_settlement(_get(row, cols, "settlement"), cfg) if "settlement" in cols else "",
                account_code=acode,
                account_name=aname,
                summary=redact(excel.cell_text(_get(row, cols, "summary"))),
                nd_reason=ndr,
                nd_reason_raw=ndr_raw,
                card_company=excel.cell_text(_get(row, cols, "card_company")),
                card_no_masked=excel.mask_card_no(_get(row, cols, "card_no")) if "card_no" in cols else "",
                deemed=_truthy(_get(row, cols, "deemed"), cfg) if "deemed" in cols else None,
                fixed_asset=fixed if fixed is not None else (False if direction == Direction.PURCHASE.value else None),
                zero_rated=zr,
                approval_no=re.sub(r"[^0-9A-Za-z]", "", excel.cell_text(_get(row, cols, "approval_no"))),
                source_file=file_name,
                row_no=rno,
            )
            res.entries.append(e)
        except (ValueError, TypeError) as ex:
            res.issues.append(ParseIssue(file_name, rno, f"전표 행 해석 실패: {ex}", Severity.WARN))
    return res


def parse_ledger_file(
    path: Path, client_id: str, config_dir: Path | None = None, default_year: int | None = None
) -> LedgerParseResult:
    """파일 → 전표. 위하고 전표 양식이 아니면 is_ledger=False (다른 파서로 넘김)."""
    cfg = ledger_config(config_dir)
    out = LedgerParseResult()
    sheets, issues = excel.read_workbook(Path(path))
    out.issues.extend(issues)
    for sh in sheets:
        r = parse_ledger_rows(sh.rows, client_id, Path(path).name, cfg, default_year)
        if r.is_ledger:
            out.is_ledger = True
            out.entries.extend(r.entries)
            out.issues.extend(r.issues)
    return out

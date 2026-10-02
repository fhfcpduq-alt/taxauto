"""normalize 단계: raw/ 파일 → list[Transaction] (+ParseIssue, 선택: wehago_return.json).

파일 종류 판별·헤더 매핑은 전부 config/columns.yaml (별칭) 기반.
모르는 형식은 오류가 아니라 ParseIssue 로 남기고 계속 진행한다.

순수 함수(테스트용):
  load_columns(config_dir)                   설정 로드
  detect_kind(sheet, file_name, cfg)         시트 → KindMatch
  parse_file(path, client, filing, cfg)      파일 → ParseResult
  dedupe(txns)                               id 기준 중복 제거
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ..context import RunContext, StageResult
from ..law import CONFIG_DIR
from ..models import (
    CardKind,
    Client,
    Direction,
    DocType,
    Filing,
    Line,
    LineValue,
    ParseIssue,
    Severity,
    Source,
    Transaction,
    VatReturn,
    normalize_biz_no,
)
from . import excel
from .base import is_junk_file
from .excel import HeaderMatch, Sheet

COLUMNS_FILE = "columns.yaml"
WEHAGO_RETURN_FILE = "wehago_return.json"

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------


@dataclass
class KindDef:
    key: str
    label: str
    source: Source
    direction: Direction | None          # None = AUTO(행별 판별)
    doc_type: DocType
    fields: dict[str, list[str]]
    card_kind: CardKind | None = None
    pair: str = ""
    vat_inclusive: bool = False
    tax_free: bool = False
    required: list[str] = field(default_factory=list)
    any_of: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    keywords_all: list[str] = field(default_factory=list)
    keywords_any: list[str] = field(default_factory=list)
    keywords_none: list[str] = field(default_factory=list)
    bonus: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)       # vat_type_map, template 등


@dataclass
class ColumnsConfig:
    kinds: dict[str, KindDef]
    values: dict
    wehago_return: dict

    def known_headers(self) -> set[str]:
        out: set[str] = set()
        for k in self.kinds.values():
            for aliases in k.fields.values():
                out.update(excel._parse_alias(str(a))[0] for a in aliases)
        out.discard("")
        return out

    def marker(self, name: str) -> list[str]:
        return [str(x) for x in self.values.get(name) or []]


_CFG_CACHE: dict[tuple[str, float], ColumnsConfig] = {}


def load_columns(config_dir: Path | None = None) -> ColumnsConfig:
    p = (config_dir or CONFIG_DIR) / COLUMNS_FILE
    if not p.exists():
        p = CONFIG_DIR / COLUMNS_FILE  # 거래처별 config_dir 에 없으면 저장소 기본값
    key = (str(p), p.stat().st_mtime)
    if key in _CFG_CACHE:
        return _CFG_CACHE[key]
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    kinds: dict[str, KindDef] = {}
    for k, v in (d.get("kinds") or {}).items():
        det = v.get("detect") or {}
        dir_name = str(v.get("direction", "AUTO")).upper()
        kinds[k] = KindDef(
            key=k,
            label=v.get("label", k),
            source=Source[v["source"]],
            direction=None if dir_name == "AUTO" else Direction[dir_name],
            doc_type=DocType[v.get("doc_type", "NONE")],
            fields={f: [str(a) for a in (al or [])] for f, al in (v.get("fields") or {}).items()},
            card_kind=CardKind[v["card_kind"]] if v.get("card_kind") else None,
            pair=v.get("pair", ""),
            vat_inclusive=bool(v.get("vat_inclusive", False)),
            tax_free=bool(v.get("tax_free", False)),
            required=list(det.get("required") or []),
            any_of=list(det.get("any") or []),
            forbid=list(det.get("forbid") or []),
            keywords_all=[str(x) for x in det.get("keywords_all") or []],
            keywords_any=[str(x) for x in det.get("keywords_any") or []],
            keywords_none=[str(x) for x in det.get("keywords_none") or []],
            bonus=[str(x) for x in det.get("bonus") or []],
            extra={x: v[x] for x in ("vat_type_map", "template") if x in v},
        )
    cfg = ColumnsConfig(kinds, d.get("values") or {}, d.get("wehago_return") or {})
    _CFG_CACHE[key] = cfg
    return cfg


# ---------------------------------------------------------------------------
# 종류 판별
# ---------------------------------------------------------------------------


@dataclass
class KindMatch:
    kind: KindDef
    header: HeaderMatch
    score: int
    confirmed: bool       # 가산점 키워드로 종류(특히 매출/매입)가 확인됨


def _has(ctx: str, kw: str) -> bool:
    return excel.norm_text(kw) in ctx


def detect_kind(sheet: Sheet, file_name: str, cfg: ColumnsConfig) -> KindMatch | None:
    best: KindMatch | None = None
    for kind in cfg.kinds.values():
        hm = excel.find_header(sheet.rows, kind.fields, min_fields=max(2, len(kind.required)))
        if hm is None:
            continue
        cols = hm.columns
        if any(f not in cols for f in kind.required):
            continue
        if kind.any_of and not any(f in cols for f in kind.any_of):
            continue
        if any(f in cols for f in kind.forbid):
            continue
        ctx = excel.norm_text(" ".join((Path(file_name).stem, sheet.name, hm.title_text, hm.header_text)))
        if kind.keywords_all and not all(_has(ctx, k) for k in kind.keywords_all):
            continue
        if kind.keywords_any and not any(_has(ctx, k) for k in kind.keywords_any):
            continue
        if any(_has(ctx, k) for k in kind.keywords_none):
            continue
        hits = sum(1 for k in kind.bonus if _has(ctx, k))
        score = len(cols) + 3 * hits
        if best is None or score > best.score:
            best = KindMatch(kind, hm, score, hits > 0)
    return best


# ---------------------------------------------------------------------------
# 행 해석
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    transactions: list[Transaction] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    wehago_return: VatReturn | None = None
    kinds: list[str] = field(default_factory=list)      # 파일 안에서 인식된 종류(시트별)


def _contains_any(text: str, markers: list[str]) -> bool:
    t = excel.norm_text(text)
    return any(excel.norm_text(m) in t for m in markers if m)


def _yes(text: str, extra_markers: list[str] = ()) -> bool:
    t = excel.norm_text(text)
    if not t:
        return False
    return t in ("y", "yes", "o", "예", "1", "true", "√", "v") or any(excel.norm_text(m) in t for m in extra_markers)


def _split_vat_inclusive(total: int) -> tuple[int, int]:
    """공급대가 → (공급가액, 세액). 세액 = 합계×10/110 원 미만 절사(부호 유지)."""
    sign = -1 if total < 0 else 1
    vat = sign * (abs(total) * 10 // 110)
    return total - vat, vat


class _RowError(Exception):
    pass


def _parse_sheet(
    sheet: Sheet, m: KindMatch, file_name: str, client: Client, filing: Filing, cfg: ColumnsConfig
) -> tuple[list[Transaction], list[ParseIssue]]:
    kind = m.kind
    hm = m.header
    cols = hm.columns
    rows = sheet.rows
    known = cfg.known_headers()
    year = filing.period.year
    client_biz = normalize_biz_no(client.biz_no)
    txns: list[Transaction] = []
    issues: list[ParseIssue] = []
    foreign_rows: list[int] = []
    dir_conflicts: list[int] = []
    unsure_rows: list[int] = []
    summary_markers = {excel.norm_text(x) for x in cfg.marker("summary_row_markers")}
    card_col = cols.get("card_no")
    cp_cols = {cols[f] for f in ("counterparty_biz_no", "supplier_biz_no", "buyer_biz_no") if f in cols}

    for ri in range(hm.data_start, len(rows)):
        row = rows[ri]
        row_no = ri + 1
        if not any(c is not None and str(c).strip() for c in row):
            continue
        if excel.is_repeated_header(row, known):
            continue

        def get(f: str) -> Any:
            i = cols.get(f)
            return row[i] if i is not None and i < len(row) else None

        def txt(f: str) -> str:
            return excel.cell_text(get(f))

        # 합계행
        first = next((c for c in row if c is not None and str(c).strip()), None)
        date_cell = get("tx_date") if "tx_date" in cols else get("month")
        if any(isinstance(v, str) and excel.norm_text(v) in summary_markers for v in (first, date_cell)):
            continue

        try:
            # --- 금액
            def amt(f: str) -> int:
                try:
                    return excel.parse_amount(get(f))
                except ValueError as e:
                    raise _RowError(f"{f} {e}") from None

            total = amt("total") if "total" in cols else 0
            supply = amt("supply_amount") if "supply_amount" in cols else 0
            vat = amt("vat") if "vat" in cols else 0
            service = amt("service_charge") if "service_charge" in cols else 0
            if not (total or supply or vat):
                continue  # 금액 없는 행(빈 행·안내문)
            memo: list[str] = []

            # --- 날짜
            monthly = False
            try:
                tx_date = excel.parse_date(get("tx_date"), default_year=year) if "tx_date" in cols else None
                if tx_date is None and ("month" in cols or "day" in cols):
                    ym = excel.parse_month(get("month"), default_year=year) if "month" in cols else None
                    day = get("day")
                    if ym and day not in (None, ""):
                        tx_date = date(ym[0], ym[1], int(excel.parse_amount(day)))
                    elif ym:
                        tx_date = excel.month_end(*ym)
                        monthly = True
                        memo.append("월합계(말일 기준)")
                issue_date = excel.parse_date(get("issue_date"), default_year=year) if "issue_date" in cols else None
                transmit_date = excel.parse_date(get("transmit_date"), default_year=year) if "transmit_date" in cols else None
            except ValueError as e:
                raise _RowError(str(e)) from None
            if tx_date is None:
                raise _RowError("거래일자 없음")

            # --- 방향·종류
            k = kind
            direction = kind.direction
            source, doc_type = kind.source, kind.doc_type
            zero_from_type = False
            sup = normalize_biz_no(get("supplier_biz_no"))
            buy = normalize_biz_no(get("buyer_biz_no"))
            own = normalize_biz_no(get("client_biz_no"))
            if client_biz and own and own != client_biz:
                foreign_rows.append(row_no)
                continue
            biz_dir: Direction | None = None
            if client_biz and (sup or buy):
                if sup == client_biz and buy != client_biz:
                    biz_dir = Direction.SALES
                elif buy == client_biz and sup != client_biz:
                    biz_dir = Direction.PURCHASE
                elif sup != client_biz and buy != client_biz:
                    foreign_rows.append(row_no)
                    continue
            if direction is None:
                direction, doc_type, zero_from_type = _auto_direction(kind, get, cfg, doc_type)
                if direction is None:
                    raise _RowError(f"매출/매입 구분 불가(유형='{txt('vat_type') or txt('direction_text')}')")
            if biz_dir and biz_dir != direction:
                if kind.pair and kind.pair in cfg.kinds:
                    if m.confirmed:
                        dir_conflicts.append(row_no)
                    k = cfg.kinds[kind.pair]
                    source, doc_type = k.source, k.doc_type
                elif m.confirmed or kind.direction is not None:
                    dir_conflicts.append(row_no)
                direction = biz_dir
            elif biz_dir is None and kind.pair and not m.confirmed:
                unsure_rows.append(row_no)

            # 수기양식: 증빙종류 '계산서'(면세)
            tax_free = k.tax_free
            dk = txt("doc_kind_text")
            if dk and "계산서" in dk and "세금" not in dk:
                doc_type, tax_free = DocType.INVOICE, True
            flag_text = txt("tax_free_flag")
            if "vat" not in cols:  # 세액 열이 없으면 가맹점 과세유형(면세사업자)도 면세 표시로 봄
                flag_text += " " + txt("merchant_tax_type")
            if not tax_free and _contains_any(flag_text, cfg.marker("tax_free_markers")):
                tax_free = True

            # --- 금액 정리
            has_supply, has_vat = "supply_amount" in cols, "vat" in cols
            if has_supply and (has_vat or tax_free):
                if tax_free and vat:
                    issues.append(ParseIssue(file_name, row_no, f"면세(계산서) 자료인데 세액 {vat:,}원 표시 - 세액 0 처리, 원본 확인", Severity.WARN))
                    vat = 0
            elif has_supply:  # 공급가액만 있고 세액 열 없음
                vat = (total - service - supply) if total else 0
            elif total:
                base = total - service
                if tax_free:
                    supply, vat = base, 0
                elif has_vat:
                    supply = base - vat
                elif k.vat_inclusive:
                    supply, vat = _split_vat_inclusive(base)
                    memo.append("공급대가만 있음: 세액=합계×10/110")
                else:
                    supply, vat = base, 0
                    issues.append(ParseIssue(file_name, row_no, "공급가액·세액 열 없음 - 합계를 공급가액으로 처리, 원본 확인", Severity.WARN))
            if service:
                memo.append(f"봉사료 {service:,}원 제외")
            tx_total = supply + vat
            if total and not service and has_supply and has_vat and total != supply + vat:
                tx_total = total  # 원본 합계 보존(불일치는 검증 섹터가 판단)

            # 취소
            if _contains_any(txt("tx_type"), cfg.marker("cancel_markers")) and (supply > 0 or vat > 0 or tx_total > 0):
                supply, vat, tx_total = -abs(supply), -abs(vat), -abs(tx_total)
                memo.append("취소거래")

            # --- 거래상대방
            if sup or buy or "supplier_name" in cols or "buyer_name" in cols:
                if direction == Direction.SALES:
                    cp_raw, cp_name = get("buyer_biz_no"), txt("buyer_name")
                else:
                    cp_raw, cp_name = get("supplier_biz_no"), txt("supplier_name")
            else:
                cp_raw, cp_name = get("counterparty_biz_no"), txt("counterparty_name")
            cp_biz = normalize_biz_no(cp_raw)
            if excel.is_rrn(cp_raw):
                cp_biz = ""
                memo.append("거래처 주민등록번호 발급분(마스킹)")

            # --- 표시값
            cls_text = " ".join(x for x in (txt("classification"), txt("invoice_type"), txt("vat_type"), txt("zero_rated_text")) if x)
            zero_rated = vat == 0 and (
                zero_from_type
                or _contains_any(cls_text, cfg.marker("zero_rated_markers"))
                or _yes(txt("zero_rated_text"))
            )
            amended = _contains_any(" ".join((txt("classification"), txt("invoice_type"))), cfg.marker("amended_markers")) or _yes(
                txt("amended_text"), cfg.marker("amended_markers")
            )
            ded_text = txt("deductible")
            deductible: bool | None = None
            if ded_text:
                n = excel.norm_text(ded_text)
                if n in {excel.norm_text(x) for x in cfg.marker("deductible_false")} or "불공제" in n:
                    deductible = False
                elif n in {excel.norm_text(x) for x in cfg.marker("deductible_true")} or "공제" in n:
                    deductible = True
            if monthly and "count" in cols:
                memo.append(f"건수 {excel.cell_text(get('count'))}")
            if txt("memo"):
                memo.append(excel.mask_rrn(txt("memo")))
            if txt("purpose"):
                memo.append(f"용도 {txt('purpose')}")

            raw = _raw_dict(row, hm.headers, card_col, cp_cols)
            t = Transaction(
                client_id=client.id,
                source=source,
                direction=direction,
                doc_type=doc_type,
                tx_date=tx_date,
                supply_amount=supply,
                vat=vat,
                total=tx_total,
                issue_date=issue_date,
                transmit_date=transmit_date,
                approval_no=txt("approval_no"),
                counterparty_biz_no=cp_biz,
                counterparty_name=cp_name,
                counterparty_tax_type=txt("merchant_tax_type"),
                item=txt("item"),
                merchant_category=txt("merchant_category"),
                card_kind=k.card_kind,
                card_no_masked=excel.mask_card_no(get("card_no")) if card_col is not None else "",
                zero_rated=zero_rated,
                is_amended=amended,
                deductible_flag_from_source=deductible,
                memo="; ".join(memo),
                source_file=file_name,
                row_no=row_no,
                raw=raw,
            )
            txns.append(t)
        except _RowError as e:
            issues.append(ParseIssue(file_name, row_no, f"[{kind.label}] 행 해석 실패: {e}", Severity.WARN))

    if foreign_rows:
        issues.append(ParseIssue(file_name, foreign_rows[0],
                                 f"[{kind.label}] 거래처 사업자번호와 무관한 행 {len(foreign_rows)}건 제외(다른 거래처 파일 혼입 의심) - 행 {_rows(foreign_rows)}",
                                 Severity.WARN))
    if dir_conflicts:
        issues.append(ParseIssue(file_name, dir_conflicts[0],
                                 f"[{kind.label}] 파일 종류와 사업자번호로 본 매출/매입 방향이 다른 행 {len(dir_conflicts)}건 - 사업자번호 기준 적용, 행 {_rows(dir_conflicts)}",
                                 Severity.WARN))
    if unsure_rows:
        issues.append(ParseIssue(file_name, unsure_rows[0],
                                 f"[{kind.label}] 매출/매입 확인 불가(제목·파일명에 표시 없고 사업자번호 불일치/없음) {len(unsure_rows)}건 - '{kind.direction.value if kind.direction else '?'}'로 가정",
                                 Severity.WARN))
    return txns, issues


def _rows(rows: list[int], limit: int = 10) -> str:
    s = ",".join(str(r) for r in rows[:limit])
    return s + (" …" if len(rows) > limit else "")


def _auto_direction(kind: KindDef, get, cfg: ColumnsConfig, doc_type: DocType) -> tuple[Direction | None, DocType, bool]:
    """위하고 전표 유형코드 / 수기양식 '구분' 으로 방향 결정. 반환 (방향, 증빙, 영세율여부)."""
    dtext = excel.cell_text(get("direction_text"))
    d_hint: Direction | None = None
    if _contains_any(dtext, cfg.marker("direction_sales")):
        d_hint = Direction.SALES
    elif _contains_any(dtext, cfg.marker("direction_purchase")):
        d_hint = Direction.PURCHASE
    vmap: dict = kind.extra.get("vat_type_map") or {}
    vt = excel.cell_text(get("vat_type"))
    if vmap and vt:
        mcode = re.match(r"\s*(\d{2})", vt)
        ent = vmap.get(mcode.group(1)) if mcode else None
        if ent is None:
            name = re.sub(r"^[\d.\s]+", "", vt).strip()
            cands = [e for e in vmap.values() if e.get("name") == name]
            if d_hint:
                cands = [e for e in cands if Direction[e["direction"]] == d_hint]
            ent = cands[0] if len(cands) == 1 else None
        if ent:
            return Direction[ent["direction"]], DocType[ent.get("doc_type", doc_type.name)], bool(ent.get("zero_rated"))
        return None, doc_type, False
    return d_hint, doc_type, False


def _raw_dict(row: list[Any], headers: list[str], card_col: int | None, cp_cols: set[int]) -> dict:
    """원본 행 보존(민감정보 마스킹)."""
    out: dict[str, str] = {}
    for i, v in enumerate(row):
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        h = headers[i] if i < len(headers) else f"열{i + 1}"
        if h in out:
            n = 2
            while f"{h}#{n}" in out:
                n += 1
            h = f"{h}#{n}"
        if i == card_col:
            s = excel.mask_card_no(v)
        elif i in cp_cols and excel.is_rrn(v):
            d = normalize_biz_no(v)
            s = f"{d[:6]}-{d[6:7]}******"
        else:
            s = excel.mask_rrn(excel.cell_text(v))
        out[h] = s
    return out


# ---------------------------------------------------------------------------
# 위하고 신고서 내보내기 (선택)
# ---------------------------------------------------------------------------

_LINE_NO_RE = re.compile(r"^\((\d{1,2}(?:-\d)?)\)$")
_NUMLIKE_RE = re.compile(r"^[\(\-△▲]?[\d,]+\)?-?$")
_LINE_BY_VALUE = {ln.value: ln for ln in Line}


def parse_wehago_return(sheet: Sheet, file_name: str, client: Client, filing: Filing, cfg: ColumnsConfig) -> VatReturn | None:
    """라인명→행 라벨 별칭(columns.yaml wehago_return) 기반. 실제 양식 미확인 - 자리만 잡아둔 파서."""
    spec = cfg.wehago_return
    if not spec:
        return None
    rows = sheet.rows
    ctx = excel.norm_text(" ".join([Path(file_name).stem, sheet.name] + [excel.cell_text(c) for r in rows[:15] for c in r if c is not None]))
    if not any(_has(ctx, k) for k in (spec.get("detect") or {}).get("keywords_any") or []):
        return None
    amount_h = {excel.norm_header(x) for x in spec.get("amount_headers") or []}
    tax_h = {excel.norm_header(x) for x in spec.get("tax_headers") or []}
    amount_col = tax_col = None
    for r in rows[:30]:
        hs = [excel.norm_header(c) if isinstance(c, str) else "" for c in r]
        a = next((i for i, h in enumerate(hs) if h in amount_h), None)
        t = next((i for i, h in enumerate(hs) if h in tax_h), None)
        if a is not None and t is not None:
            amount_col, tax_col = a, t
            break
    aliases = sorted(
        ((Line[ln], str(al)) for ln, als in (spec.get("labels") or {}).items() if ln in Line.__members__ for al in als),
        key=lambda x: -len(excel.norm_text(x[1].replace("+", ""))),
    )
    result: dict[str, LineValue] = {}
    section = ""
    for r in rows:
        if r and isinstance(r[0], str) and r[0].strip():
            section = excel.norm_text(r[0])
        strs = [(i, excel.norm_text(c)) for i, c in enumerate(r) if isinstance(c, str) and c.strip() and not _NUMLIKE_RE.match(c.strip())]
        if not strs:
            continue
        line: Line | None = None
        label_idx = -1
        for i, c in enumerate(r):
            mm = _LINE_NO_RE.match(c.strip()) if isinstance(c, str) else None
            if mm and mm.group(1) in _LINE_BY_VALUE:
                line, label_idx = _LINE_BY_VALUE[mm.group(1)], i
                break
        if line is None:
            rowtext = section + "".join(s for _, s in strs)
            for ln, al in aliases:
                parts = [excel.norm_text(p) for p in al.split("+")]
                if ln.name not in result and all(p in rowtext for p in parts):
                    line, label_idx = ln, strs[-1][0]  # 숫자는 마지막 문자열 칸 오른쪽에서
                    break
        if line is None or line.name in result:
            continue
        try:
            if amount_col is not None and tax_col is not None:
                lv = LineValue(excel.parse_amount(_at(r, amount_col)), excel.parse_amount(_at(r, tax_col)), 0)
            else:
                nums = [excel.parse_amount(c) for c in r[label_idx + 1:] if _is_num(c)]
                if not nums:
                    continue
                lv = LineValue(nums[0], nums[1] if len(nums) > 1 else 0, 0)
        except ValueError:
            continue
        result[line.name] = lv
    if len(result) < int((spec.get("detect") or {}).get("min_lines", 3)):
        return None
    return VatReturn(
        client_id=client.id,
        period=filing.period.code,
        lines=result,
        notes=[f"위하고 신고서 내보내기 파일에서 읽음: {file_name} (라벨 별칭 매칭, 양식 미확인 - 대사 전 육안 확인)"],
        computed_by="wehago",
    )


def _at(r: list, i: int) -> Any:
    return r[i] if i < len(r) else None


def _is_num(c: Any) -> bool:
    if isinstance(c, bool):
        return False
    if isinstance(c, (int, float)):
        return True
    return isinstance(c, str) and bool(_NUMLIKE_RE.match(c.strip())) and any(ch.isdigit() for ch in c)


# ---------------------------------------------------------------------------
# 파일 단위
# ---------------------------------------------------------------------------


def parse_file(path: Path, client: Client, filing: Filing, cfg: ColumnsConfig | None = None) -> ParseResult:
    cfg = cfg or load_columns()
    res = ParseResult()
    name = path.name
    sheets, issues = excel.read_workbook(path)
    res.issues.extend(issues)
    unknown: list[str] = []
    for sh in sheets:
        if not any(any(c is not None and str(c).strip() for c in r) for r in sh.rows):
            continue
        wr = parse_wehago_return(sh, name, client, filing, cfg)
        if wr is not None:
            res.wehago_return = wr
            res.kinds.append("wehago_return")
            continue
        m = detect_kind(sh, name, cfg)
        if m is None:
            unknown.append(sh.name)
            continue
        txns, iss = _parse_sheet(sh, m, name, client, filing, cfg)
        res.kinds.append(m.kind.key)
        res.transactions.extend(txns)
        res.issues.extend(iss)
    if unknown and not res.kinds:
        res.issues.append(ParseIssue(name, 0, "알 수 없는 형식 - 헤더를 인식하지 못함(config/columns.yaml 별칭 추가 필요)", Severity.WARN))
    elif unknown:
        res.issues.append(ParseIssue(name, 0, f"인식하지 못한 시트 건너뜀: {', '.join(unknown)}", Severity.INFO))
    return res


def dedupe(txns: list[Transaction]) -> tuple[list[Transaction], list[Transaction]]:
    """id 동일 거래 중복 제거(먼저 읽은 것 유지). 반환 (유지, 제거)."""
    seen: set[str] = set()
    keep, dup = [], []
    for t in txns:
        (dup if t.id in seen else keep).append(t)
        seen.add(t.id)
    return keep, dup


def raw_files(raw_dir: Path) -> list[Path]:
    if not raw_dir.exists():
        return []
    return sorted(p for p in raw_dir.iterdir() if p.is_file() and not is_junk_file(p))


def normalize_files(paths: list[Path], client: Client, filing: Filing, cfg: ColumnsConfig) -> tuple[list[Transaction], list[ParseIssue], VatReturn | None]:
    """파일 목록 → (거래, 이슈, 위하고신고서). run() 의 순수 로직."""
    all_tx: list[Transaction] = []
    issues: list[ParseIssue] = []
    wr: VatReturn | None = None
    for p in paths:
        r = parse_file(p, client, filing, cfg)
        all_tx.extend(r.transactions)
        issues.extend(r.issues)
        if r.wehago_return is not None:
            wr = r.wehago_return
    txns, dup = dedupe(all_tx)
    if dup:
        files = sorted({d.source_file for d in dup})
        issues.append(ParseIssue(files[0], 0, f"중복 거래 {len(dup)}건 제거(같은 승인번호·금액 등): {', '.join(files)}", Severity.INFO))
    out = [t for t in txns if not (filing.coverage_start <= t.tx_date <= filing.coverage_end)]
    if out:
        before = sum(1 for t in out if t.tx_date < filing.coverage_start)
        after = len(out) - before
        issues.append(ParseIssue("", 0,
                                 f"집계기간({filing.coverage_start}~{filing.coverage_end}) 밖 거래 {len(out)}건 유지(이전 {before}건·이후 {after}건) - 예정신고 누락분 등 검증에서 판단",
                                 Severity.INFO))
    # 같은 출처에 월합계와 건별 자료가 함께 있으면 이중집계 위험
    by_src: dict[Source, set[bool]] = {}
    for t in txns:
        if t.source != Source.WEHAGO_LEDGER:
            by_src.setdefault(t.source, set()).add("월합계" in t.memo)
    for s, kinds in by_src.items():
        if kinds == {True, False}:
            issues.append(ParseIssue("", 0, f"{s.value}: 월합계 자료와 건별 자료가 함께 있음 - 이중집계 우려, 한쪽 파일 제거 필요", Severity.WARN))
    return txns, issues, wr


# ---------------------------------------------------------------------------
# 단계
# ---------------------------------------------------------------------------


def run(ctx: RunContext) -> StageResult:
    cfg = load_columns(ctx.config_dir)
    paths = raw_files(ctx.workspace.raw_dir)
    txns, issues, wr = normalize_files(paths, ctx.client, ctx.filing, cfg)
    if not ctx.dry_run:
        ctx.workspace.save_transactions(txns)
        ctx.workspace.save_parse_issues(issues)
        if wr is not None:
            ctx.workspace.save_return(wr, WEHAGO_RETURN_FILE)
    counts: dict[str, int] = {"files": len(paths), "transactions": len(txns),
                              "parse_issues": len(issues),
                              "parse_warn": sum(1 for i in issues if i.severity != Severity.INFO)}
    for t in txns:
        counts[f"src:{t.source.value}"] = counts.get(f"src:{t.source.value}", 0) + 1
    if wr is not None:
        counts["wehago_return"] = 1
    msg = f"파일 {len(paths)}개 → 거래 {len(txns)}건, 확인필요 {counts['parse_warn']}건"
    ctx.log.info("[normalize] %s %s", ctx.client.id, msg)
    return StageResult(ok=True, message=msg if paths else "raw/ 에 파일 없음", counts=counts)

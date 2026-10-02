"""enrich 단계: 매입 거래상대방 사업자 상태조회(국세청, 공공데이터포털).

API: 국세청_사업자등록정보 진위확인 및 상태조회 서비스
  POST https://api.odcloud.kr/api/nts-businessman/v1/status?serviceKey=KEY
  body {"b_no": ["1234567890", ...]}   (요청당 최대 100개)
  응답 data[]: b_no, b_stt(계속사업자/휴업자/폐업자), b_stt_cd(01/02/03), tax_type, end_dt(YYYYMMDD)

- policy.nts_status.enabled=false 또는 환경변수 NTS_SERVICE_KEY 없음 → 건너뜀(ok, skipped)
- 캐시: data/_cache/nts_status.json  (policy.nts_status.cache_days 일 유효)
- HTTP 는 urllib(표준). 전송 함수(transport) 주입 가능 → 테스트에서 가짜 응답
- 조회 대상: 매입 세금계산서·계산서·카드·현금영수증·종이세금계산서 (사업자번호 10자리만)
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from ..context import RunContext, StageResult
from ..models import Direction, Source, Transaction, _date

API_URL = "https://api.odcloud.kr/api/nts-businessman/v1/status"
ENV_KEY = "NTS_SERVICE_KEY"
BATCH = 100
CACHE_FILE = "nts_status.json"

# transport(url, body_bytes, headers) -> 응답 bytes
Transport = Callable[[str, bytes, dict], bytes]

TARGET_SOURCES = {
    Source.ETAX_PURCHASE,
    Source.EINV_PURCHASE,
    Source.CARD_PURCHASE,
    Source.CASH_RECEIPT_PURCHASE,
    Source.PAPER_TAX_INVOICE,
}

_STATUS_SHORT = {"01": "계속", "02": "휴업", "03": "폐업"}


class NtsError(Exception):
    pass


def urllib_transport(url: str, body: bytes, headers: dict, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (고정 https URL)
        return resp.read()


def _service_url(key: str) -> str:
    # 포털의 'Encoding' 키(이미 %인코딩)와 'Decoding' 키 모두 허용
    enc = key if "%" in key else urllib.parse.quote(key, safe="")
    return f"{API_URL}?serviceKey={enc}&returnType=JSON"


def fetch_status(b_nos: list[str], service_key: str, transport: Transport | None = None) -> dict[str, dict]:
    """사업자번호 목록 → {b_no: 응답항목}. 100개씩 나눠 조회. 실패 시 NtsError(키는 메시지에 넣지 않음)."""
    transport = transport or urllib_transport
    out: dict[str, dict] = {}
    url = _service_url(service_key)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    for i in range(0, len(b_nos), BATCH):
        chunk = b_nos[i:i + BATCH]
        body = json.dumps({"b_no": chunk}).encode()
        try:
            raw = transport(url, body, headers)
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise NtsError(f"국세청 상태조회 실패({type(e).__name__}): {_scrub(str(e), service_key)}") from None
        if not isinstance(data, dict) or "data" not in data:
            raise NtsError(f"국세청 상태조회 응답 형식 오류: {_scrub(str(data)[:200], service_key)}")
        for item in data.get("data") or []:
            b = "".join(ch for ch in str(item.get("b_no", "")) if ch.isdigit())
            if b:
                out[b] = item
    return out


def _scrub(s: str, key: str) -> str:
    if key:
        s = s.replace(key, "***").replace(urllib.parse.quote(key, safe=""), "***")
    return s


# ---------------------------------------------------------------------------
# 캐시
# ---------------------------------------------------------------------------


def load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _fresh(entry: dict, today: date, cache_days: int) -> bool:
    try:
        return (today - date.fromisoformat(entry["fetched_on"])) < timedelta(days=cache_days)
    except (KeyError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 적용
# ---------------------------------------------------------------------------


def is_target(t: Transaction) -> bool:
    return t.direction == Direction.PURCHASE and t.source in TARGET_SOURCES and len(t.counterparty_biz_no) == 10


def apply_status(t: Transaction, item: dict) -> None:
    """응답항목 → 거래 counterparty_* 필드."""
    tax_type = str(item.get("tax_type") or "").strip()
    stt = str(item.get("b_stt") or "").strip()
    cd = str(item.get("b_stt_cd") or "").strip()
    if tax_type:
        t.counterparty_tax_type = tax_type
    if cd in _STATUS_SHORT:
        t.counterparty_status = _STATUS_SHORT[cd]
    elif stt:
        t.counterparty_status = stt.replace("사업자", "").replace("자", "") or stt
    elif "등록되지" in tax_type:
        t.counterparty_status = "미등록"
    end = str(item.get("end_dt") or "").strip()
    if len(end) == 8 and end.isdigit():
        t.counterparty_closed_on = _date(f"{end[:4]}-{end[4:6]}-{end[6:]}")


def enrich_transactions(
    txns: list[Transaction],
    service_key: str,
    cache: dict,
    today: date,
    cache_days: int = 30,
    transport: Transport | None = None,
    offline: bool = False,
) -> dict:
    """대상 거래에 상태 채움(cache 갱신). 반환 counts."""
    targets = [t for t in txns if is_target(t)]
    b_nos = sorted({t.counterparty_biz_no for t in targets})
    need = [b for b in b_nos if not _fresh(cache.get(b, {}), today, cache_days)]
    fetched = 0
    if need and not offline:
        res = fetch_status(need, service_key, transport)
        for b, item in res.items():
            cache[b] = {"fetched_on": today.isoformat(), "data": item}
        fetched = len(res)
    filled = 0
    for t in targets:
        e = cache.get(t.counterparty_biz_no)
        if e and e.get("data"):
            apply_status(t, e["data"])
            filled += 1
    closed = sum(1 for t in targets if t.counterparty_status == "폐업")
    return {"targets": len(targets), "biz_nos": len(b_nos), "fetched": fetched, "filled": filled, "closed": closed}


def run(ctx: RunContext, transport: Transport | None = None) -> StageResult:
    conf = (ctx.policy or {}).get("nts_status") or {}
    key = os.environ.get(ENV_KEY, "").strip()
    if not conf.get("enabled", False):
        return StageResult(ok=True, message="국세청 상태조회 꺼짐(policy.nts_status.enabled=false)", skipped=True)
    if not key:
        return StageResult(ok=True, message=f"국세청 상태조회 키 없음(환경변수 {ENV_KEY}) - 건너뜀", skipped=True)
    cache_path = ctx.workspace.root.parent.parent / "_cache" / CACHE_FILE   # data/_cache/
    cache = load_cache(cache_path)
    txns = ctx.workspace.load_transactions()
    try:
        counts = enrich_transactions(txns, key, cache, ctx.today, int(conf.get("cache_days", 30)), transport,
                                     offline=ctx.dry_run)
    except NtsError as e:
        # 보조 단계 - 실패해도 파이프라인은 계속(분류는 원천 과세유형 표시로 진행)
        counts = enrich_transactions(txns, key, cache, ctx.today, 10**6, transport, offline=True)
        counts["errors"] = 1
        if not ctx.dry_run:
            ctx.workspace.save_transactions(txns)
        return StageResult(ok=True, message=f"{e} - 캐시값만 반영({counts['filled']}건)", counts=counts)
    if not ctx.dry_run:
        save_cache(cache_path, cache)
        ctx.workspace.save_transactions(txns)
    msg = f"상태조회 {counts['biz_nos']}개 사업자(신규 {counts['fetched']}), 반영 {counts['filled']}건, 폐업 {counts['closed']}건"
    ctx.log.info("[enrich] %s %s", ctx.client.id, msg)
    return StageResult(ok=True, message=msg, counts=counts)

"""세무사 전표 스타일 적용 — classify 단계 마지막에 호출: apply_style(txns, ctx).

순서: 거래처 style.yaml → 업종팩(config/style/industry/{group}.yaml → _all.yaml)
      → (policy.llm.enabled 이면) few-shot: data/_learn/examples.jsonl 유사사례 k개 + Claude
      → 그래도 없으면 비워둠(위하고 기본값 유지). needs_review 는 건드리지 않는다.
채우는 것: Classification 의 account_code/account_name/entry_type/settlement/summary_text/style_source.
VAT 판정값(category·사유)은 절대 바꾸지 않는다. 학습 스타일이 '불공'인데 VAT 판정이 일반매입이면
needs_review=True + note 로 충돌만 표시.
매출: compute 가 매출 classification 을 읽지 않으므로(compute/vat_return.py 확인) 스타일 전용 껍데기
      Classification(category=일반매입, decided_by=default, note='매출-스타일전용') 에 담는다.
LLM 전송: 가맹점명(개인명 마스킹)·가맹점업종·금액·문서종류·방향만. 사업자번호·카드번호·품목·메모 제외.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..compute.lawutil import policy_get
from ..models import Classification, DecidedBy, Direction, DocType, PurchaseCategory, Source, Transaction
from ..learn.features import (
    Features,
    client_group,
    features_from_transaction,
    mask_name,
    name_similarity,
    render_summary,
    style_config,
)
from ..learn.miner import StyleModel
from ..learn.store import default_packs_dir, load_client_style, load_examples, load_model

log = logging.getLogger("taxauto.classify.account")

SALES_NOTE = "매출-스타일전용"
CONFLICT_MARK = "스타일충돌"
FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
STYLE_FIELDS = ("account_code", "account_name", "entry_type", "settlement", "summary_text")


@dataclass
class StyleApplyResult:
    styled: int = 0
    sales_shells: int = 0
    conflicts: int = 0
    fewshot_sent: int = 0
    fewshot_applied: int = 0
    skipped_reason: str = ""
    by_source: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 규칙 적용
# ---------------------------------------------------------------------------


def _set_account(c: Classification, value: str) -> None:
    code, _, name = value.partition("|")
    c.account_code, c.account_name = code, name


def _clear_style(c: Classification) -> None:
    for k in STYLE_FIELDS:
        setattr(c, k, "")
    c.style_source = ""


def _append_note(c: Classification, text: str) -> None:
    if text not in (c.note or ""):
        c.note = f"{c.note} | {text}" if c.note else text


def reconcile(c: Classification, t: Transaction, style_nd: str, style_fixed: str) -> bool:
    """VAT 판정과 스타일 충돌 처리(판정값은 불변). 충돌 표시했으면 True."""
    cat = c.category
    conflict = False
    style_is_nd = c.entry_type == "불공" or (style_nd not in ("", "없음"))
    if t.direction == Direction.PURCHASE and style_is_nd and cat in (
        PurchaseCategory.GENERAL, PurchaseCategory.FIXED_ASSET, PurchaseCategory.DEEMED_INPUT
    ):
        c.needs_review = True
        _append_note(c, f"{CONFLICT_MARK}: 세무사 전표는 불공({style_nd or '사유없음'})인데 VAT 판정은 {cat.value} - 판정 유지, 확인 필요")
        conflict = True
    if cat == PurchaseCategory.NON_DEDUCTIBLE and c.entry_type and c.entry_type != "불공":
        if t.doc_type == DocType.TAX_INVOICE:
            _append_note(c, f"스타일 유형 '{c.entry_type}' → VAT 판정(불공제)에 맞춰 '불공'")
            c.entry_type = "불공"
        else:
            _append_note(c, f"스타일 유형 '{c.entry_type}' 비움(VAT 판정 불공제 - 공제 유형으로 입력 금지)")
            c.entry_type = ""
    if cat == PurchaseCategory.NOT_APPLICABLE and c.entry_type in ("카과", "현과", "과세"):
        _append_note(c, f"스타일 유형 '{c.entry_type}' 비움(신고제외 판정)")
        c.entry_type = ""
    if style_fixed == "Y" and cat == PurchaseCategory.GENERAL:
        _append_note(c, "스타일참고: 세무사는 이런 매입을 고정자산으로 처리 - 고정자산 여부 확인")
    return conflict


def apply_with_model(
    txns: list[Transaction], model: StyleModel, group: str, cfg: dict | None = None
) -> tuple[StyleApplyResult, list[tuple[Transaction, Features]]]:
    """규칙(거래처·업종팩)만으로 스타일 채움. 반환: (결과, few-shot 후보[계정 미정])."""
    cfg = cfg if cfg is not None else style_config()
    res = StyleApplyResult()
    pending: list[tuple[Transaction, Features]] = []
    for t in txns:
        if t.source == Source.WEHAGO_LEDGER:
            continue
        if t.direction == Direction.PURCHASE and t.classification is None:
            continue  # 매입 미분류는 판정 섹터 몫(스타일만 따로 만들지 않음)
        if t.direction == Direction.SALES and not (cfg.get("apply") or {}).get("sales", True):
            continue
        c = t.classification
        if c is not None and c.style_source.startswith("human"):
            continue
        f = features_from_transaction(t, group, cfg)
        preds = model.predict(f)
        prev_fewshot = c is not None and "fewshot" in (c.style_source or "")
        if not preds and not prev_fewshot:
            if c is not None and c.style_source:
                _clear_style(c)
            pending.append((t, f))
            continue
        if c is None:
            c = Classification(category=PurchaseCategory.GENERAL, decided_by=DecidedBy.DEFAULT, note=SALES_NOTE)
            t.classification = c
            res.sales_shells += 1
        if preds:
            keep_fs = {k: getattr(c, k) for k in STYLE_FIELDS} if prev_fewshot else {}
            _clear_style(c)
            srcs: dict[str, list[str]] = {}
            if "account" in preds:
                _set_account(c, preds["account"].value)
            if "entry_type" in preds:
                c.entry_type = preds["entry_type"].value
            if "settlement" in preds:
                c.settlement = preds["settlement"].value
            if "summary" in preds:
                c.summary_text = render_summary(preds["summary"].value, f)
            for fld, p in preds.items():
                srcs.setdefault(p.source, []).append(fld)
                res.by_source[p.origin] = res.by_source.get(p.origin, 0) + 1
            for k, v in keep_fs.items():  # 규칙이 없는 칸은 이전 few-shot 값 유지(재질의 비용 절감)
                if not getattr(c, k) and v:
                    setattr(c, k, v)
            c.style_source = ";".join(f"{s}({','.join(fs)})" for s, fs in srcs.items()) + (";fewshot" if keep_fs else "")
            nd = preds["nd_reason"].value if "nd_reason" in preds else ""
            fx = preds["fixed_asset"].value if "fixed_asset" in preds else ""
        else:
            nd = fx = ""
        if t.direction == Direction.PURCHASE and reconcile(c, t, nd, fx):
            res.conflicts += 1
        if not c.account_code and not c.account_name and not prev_fewshot:
            pending.append((t, f))
        res.styled += 1
    return res, pending


# ---------------------------------------------------------------------------
# few-shot (Claude)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """당신은 한국 세무사무실에서 위하고(WEHAGO) 매입매출전표를 입력하는 보조자입니다.
이 사무실 세무사가 실제로 입력한 '유사 사례'를 보고, 새 거래를 세무사와 같은 스타일로 입력할 값을 고릅니다.
- 계정과목(account)은 반드시 후보 목록에서 고르세요. 맞는 후보가 없으면 빈 문자열.
- 매입매출 유형(entry_type)·분개(settlement)·적요(summary_text)도 유사 사례의 표기 방식을 그대로 따르세요.
- 부가세 공제 여부는 판단하지 마세요(별도 엔진이 판정). 스타일만 고릅니다.
- 유사 사례와 거리가 멀거나 확신이 없으면 confidence 를 낮게 주세요.
반드시 입력의 모든 index 에 대해 결과를 하나씩 돌려주세요."""


def _schema(accounts: list[str], entry_types: list[str], settlements: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "account": {"type": "string", "enum": [""] + accounts},
                        "entry_type": {"type": "string", "enum": [""] + entry_types},
                        "settlement": {"type": "string", "enum": [""] + settlements},
                        "summary_text": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["index", "account", "entry_type", "settlement", "summary_text", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def retrieve(f: Features, examples: list[dict], k: int) -> list[dict]:
    """유사사례 k개: 같은 방향 필수, 같은 거래처·업종그룹·가맹점업종·문서종류·상호 유사도·금액구간 가점."""
    scored = []
    for e in examples:
        if e.get("direction") != f.direction or not (e.get("labels") or {}).get("account"):
            continue
        s = 3.0 * name_similarity(f.name_key, e.get("name_key", ""))
        s += 3.0 if e.get("client_id") == f.client_id else 0.0
        s += 2.0 if e.get("group") == f.group else 0.0
        s += 2.0 if f.mcat and e.get("mcat") == f.mcat else 0.0
        s += 1.0 if e.get("doc_type") == f.doc_type else 0.0
        s += 0.5 if e.get("band") == f.band else 0.0
        scored.append((-s, e.get("client_id", ""), e.get("row_no", 0), e))
    scored.sort(key=lambda x: x[:3])
    return [x[3] for x in scored[:k]]


def _acct_label(v: str) -> str:
    c, _, n = v.partition("|")
    return f"{c} {n}".strip()


def _example_item(e: dict, cfg: dict) -> dict:
    lab = e.get("labels") or {}
    fe = Features.from_dict(e)
    masked = mask_name(fe.display or fe.name, cfg)
    fe.display = fe.name = masked
    return {
        "가맹점명": masked, "가맹점업종": e.get("mcat", ""), "문서": e.get("doc_type", ""), "방향": e.get("direction", ""),
        "공급가액": int(e.get("supply_amount", 0)), "계정": _acct_label(lab.get("account", "")), "유형": lab.get("entry_type", ""),
        "분개": lab.get("settlement", ""), "적요": render_summary(lab.get("summary", ""), fe),
    }


def _target_item(i: int, f: Features, cfg: dict) -> dict:
    return {"index": i, "가맹점명": mask_name(f.display or f.name, cfg), "가맹점업종": f.mcat, "문서": f.doc_type,
            "방향": f.direction, "공급가액": f.supply_amount}


def _default_client_factory() -> Any:
    import anthropic  # 선택 의존성

    return anthropic.Anthropic()


def _call(client: Any, model: str, policy: dict, cfg: dict, payload: dict, schema: dict) -> list[dict]:
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
    effort = (cfg.get("apply") or {}).get("fewshot_effort")
    if effort and "haiku" not in model:
        output_config["effort"] = effort
    kwargs = dict(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        output_config=output_config,
    )
    fb = policy_get(policy, "llm.fallbacks", "auto")
    if fb == "auto":
        fb = "default" if model in FALLBACK_MODELS else None
    if fb:
        resp = client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks=fb)
    else:
        resp = client.messages.create(**kwargs)
    if getattr(resp, "stop_reason", None) in ("refusal", "max_tokens"):
        log.warning("few-shot 응답 중단(stop_reason=%s) - 이 묶음 건너뜀", resp.stop_reason)
        return []
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    return list((json.loads(text or "{}")).get("results") or [])


def fewshot_fill(
    pending: list[tuple[Transaction, Features]],
    examples: list[dict],
    policy: dict,
    cfg: dict | None = None,
    client_factory: Callable[[], Any] | None = None,
    group_label: str = "",
) -> tuple[int, int, str]:
    """계정 미정 거래를 유사사례 few-shot 으로 채움. 반환 (보낸 수, 반영 수, 건너뛴 사유)."""
    cfg = cfg if cfg is not None else style_config()
    acfg = cfg.get("apply") or {}
    if not policy_get(policy, "llm.enabled", False):
        return 0, 0, "policy.llm.enabled=false"
    model = policy_get(policy, "llm.model")
    if not model:
        return 0, 0, "policy.llm.model 없음"
    if client_factory is None and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return 0, 0, "API 자격증명 없음"
    if not pending:
        return 0, 0, "대상 없음"
    if not examples:
        return 0, 0, "학습 사례(examples.jsonl) 없음"
    try:
        client = (client_factory or _default_client_factory)()
    except Exception as e:  # SDK 미설치 등
        return 0, 0, f"클라이언트 생성 실패: {type(e).__name__}"
    k = int(acfg.get("fewshot_k", 6))
    batch = int(acfg.get("fewshot_batch", 30))
    min_conf = float(acfg.get("fewshot_min_confidence", 0.7))
    pending = pending[: int(acfg.get("fewshot_max_items", 200))]
    entry_types = sorted({str((e.get("labels") or {}).get("entry_type", "")) for e in examples} - {""})
    settlements = sorted({str((e.get("labels") or {}).get("settlement", "")) for e in examples} - {""})
    sent = applied = 0
    reason = ""
    for start in range(0, len(pending), batch):
        chunk = pending[start : start + batch]
        pool: dict[int, dict] = {}
        for _, f in chunk:
            for e in retrieve(f, examples, k):
                pool[id(e)] = e
        exs = list(pool.values())
        accounts = sorted({str((e.get("labels") or {}).get("account", "")) for e in exs} - {""})
        if not accounts:
            continue
        acct_by_label = {_acct_label(a): a for a in accounts}
        payload = {"거래처업종": group_label, "계정후보": list(acct_by_label), "유사사례": [_example_item(e, cfg) for e in exs],
                   "거래": [_target_item(i, f, cfg) for i, (_, f) in enumerate(chunk)]}
        schema = _schema(list(acct_by_label), entry_types, settlements)
        try:
            results = _call(client, str(model), policy, cfg, payload, schema)
        except Exception as e:  # 네트워크·API·파싱 오류 → 조용히 건너뜀(엔진은 결정적 결과로 완결)
            reason = f"호출 실패: {type(e).__name__}"
            log.info("few-shot 호출 실패(건너뜀): %s", type(e).__name__)
            continue
        sent += len(chunk)
        for r in results:
            try:
                t, f = chunk[int(r["index"])]
                conf = float(r.get("confidence", 0))
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            if conf < min_conf:
                continue
            c = t.classification
            if c is None:
                if t.direction != Direction.SALES:
                    continue
                c = Classification(category=PurchaseCategory.GENERAL, decided_by=DecidedBy.DEFAULT, note=SALES_NOTE)
                t.classification = c
            filled = []
            acct = acct_by_label.get(str(r.get("account", "")))
            if acct and not (c.account_code or c.account_name):
                _set_account(c, acct)
                filled.append("account")
            for fld, key in (("entry_type", "entry_type"), ("settlement", "settlement"), ("summary_text", "summary_text")):
                v = str(r.get(key) or "").strip()
                if v and not getattr(c, fld):
                    setattr(c, fld, v[:60])
                    filled.append(fld)
            if filled:
                tag = f"fewshot:{model}({','.join(filled)})"
                c.style_source = f"{c.style_source};{tag}" if c.style_source else tag
                if t.direction == Direction.PURCHASE:
                    reconcile(c, t, "", "")
                applied += 1
    return sent, applied, reason


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def apply_style(txns: list[Transaction], ctx, client_factory: Callable[[], Any] | None = None) -> StyleApplyResult:
    """classify/stage.py 가 판정 직후 호출. txns 를 제자리 갱신."""
    cfg = style_config(ctx.config_dir)
    gfile, _ = load_client_style(ctx.client_dir)
    group = gfile or client_group(ctx.client)
    model = load_model(ctx.client.id, group, ctx.client_dir, default_packs_dir(ctx.config_dir), cfg)
    res, pending = apply_with_model(txns, model, group, cfg)
    if len(model) == 0:
        res.skipped_reason = "학습된 스타일 없음(style.yaml·업종팩)"
    data_root = Path(ctx.workspace.root).parent.parent
    examples: list[dict] = []
    if policy_get(ctx.policy, "llm.enabled", False) and pending:
        examples = load_examples(data_root / "_learn" / "examples.jsonl")
    sent, applied, reason = fewshot_fill(pending, examples, ctx.policy, cfg, client_factory, group)
    res.fewshot_sent, res.fewshot_applied = sent, applied
    if reason and not res.skipped_reason:
        res.skipped_reason = f"few-shot: {reason}"
    return res

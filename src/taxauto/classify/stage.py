"""classify 단계: 매입 거래에 공제판정(classification)을 채운다.

순서: 사람 결정 유지 → 거래단위 룰(before_memory) → 거래처 메모리 → 일반 룰 → (선택) AI.
매출은 classification 을 두지 않는다.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from ..context import RunContext, StageResult
from ..law import Law
from ..models import Classification, Client, DecidedBy, Direction, Source, Transaction
from .llm import classify_with_llm
from .memory import Memory
from .rules import RuleContext, build_context, classify_by_rules, load_rules


def classify_transactions(
    txns: list[Transaction],
    client: Client,
    policy: dict,
    law: Law | None = None,
    memory: Memory | None = None,
    rules=None,
    on: date | None = None,
) -> RuleContext:
    """결정적 판정(메모리+룰). txns 를 제자리 갱신하고 RuleContext(누락 law 키 등)를 돌려준다."""
    rules = rules if rules is not None else load_rules()
    memory = memory or Memory([])
    ctx = build_context(txns, client, policy, law, on)
    for t in txns:
        if t.direction != Direction.PURCHASE or t.source == Source.WEHAGO_LEDGER:
            continue
        if t.classification is not None and t.classification.decided_by == DecidedBy.HUMAN:
            continue
        c: Classification | None = classify_by_rules(t, rules, ctx, phase="pre")
        if c is None:
            c = memory.lookup(t)
        if c is None:
            # 이전 실행의 AI 판정은 비용 절감을 위해 유지(메모리·거래단위 룰이 우선)
            if t.classification is not None and t.classification.decided_by == DecidedBy.LLM:
                continue
            c = classify_by_rules(t, rules, ctx, phase="post")
        if c is not None:
            t.classification = c
    return ctx


def run(ctx: RunContext) -> StageResult:
    txns = ctx.workspace.load_transactions()
    if not txns:
        return StageResult(ok=True, message="거래 없음", skipped=True)
    memory = Memory.load(ctx.client_dir)
    rctx = classify_transactions(txns, ctx.client, ctx.policy, ctx.law, memory, on=ctx.filing.coverage_end)
    llm_res = classify_with_llm(txns, ctx.client, ctx.policy)
    if llm_res.skipped_reason:
        ctx.log.info("AI 분류: %s", llm_res.skipped_reason)
    if not ctx.dry_run:
        ctx.workspace.save_transactions(txns)
    purchases = [t for t in txns if t.direction == Direction.PURCHASE and t.classification]
    by = Counter(t.classification.decided_by.value for t in purchases)
    cats = Counter(t.classification.category.value for t in purchases)
    counts = {
        "purchases": len(purchases),
        "needs_review": sum(1 for t in purchases if t.classification.needs_review),
        "memory_entries": len(memory),
        "llm_sent": llm_res.sent,
        "llm_applied": llm_res.applied,
        **{f"by_{k}": v for k, v in by.items()},
        **{f"cat_{k}": v for k, v in cats.items()},
    }
    msg = f"매입 {len(purchases)}건 판정"
    if rctx.missing_law_keys:
        msg += f" (법 파라미터 없음: {', '.join(sorted(rctx.missing_law_keys))} → 룰 파일 키워드만 사용)"
    return StageResult(ok=True, message=msg, counts=counts)

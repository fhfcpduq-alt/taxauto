"""파이프라인 단계 계약.

모든 단계는 다음 시그니처의 함수 하나로 구현한다.

    def run(ctx: RunContext) -> StageResult

단계 모듈(섹터별 담당):
    collect   taxauto.ingest.collect      (수집·정규화 섹터)
    normalize taxauto.ingest.normalize    (수집·정규화 섹터)
    enrich    taxauto.ingest.enrich       (수집·정규화 섹터)
    classify  taxauto.classify.stage      (판정·계산·검증 섹터)
    compute   taxauto.compute.stage       (판정·계산·검증 섹터)
    validate  taxauto.validate.stage      (판정·계산·검증 섹터)
    report    taxauto.report.stage        (오케스트레이션·에이전트 섹터)

핵심 로직은 단계 함수 안이 아니라 '순수 함수'로 분리해 테스트 가능하게 만든다.
  예) compute_return(txns, client, filing, law, policy) -> VatReturn
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .law import Law
from .models import Client, Filing
from .workspace import Workspace


@dataclass
class RunContext:
    client: Client
    filing: Filing
    workspace: Workspace
    law: Law
    policy: dict
    inbox_dir: Path              # inbox/{period}/{client_id}
    config_dir: Path
    clients_dir: Path            # clients/{client_id}/ (거래처별 메모리·조정값)
    today: date
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("taxauto"))
    dry_run: bool = False

    @property
    def period_code(self) -> str:
        return self.filing.period.code

    @property
    def client_dir(self) -> Path:
        return self.clients_dir / self.client.id


@dataclass
class StageResult:
    ok: bool
    message: str = ""
    counts: dict = field(default_factory=dict)   # 예: {"transactions": 312, "review_open": 4}
    skipped: bool = False

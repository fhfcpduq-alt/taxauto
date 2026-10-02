"""작업공간(파일) 계약.

파이프라인 각 단계의 결과는 사람이 열어볼 수 있는 JSON 파일로 남긴다.
→ 중간에 실패해도 그 단계부터 재실행 가능, AI 에이전트도 파일만 읽으면 상황 파악 가능.

  data/{period}/{client_id}/
    raw/                 collect   : 원본 파일 사본(홈택스·위하고 내려받은 엑셀)
    transactions.json    normalize : list[Transaction]  (enrich/classify 단계가 같은 파일을 갱신)
    parse_issues.json    normalize : list[ParseIssue]
    return.json          compute   : VatReturn (독립 재계산 신고서)
    compute_issues.json  compute   : 계산 중 생긴 검토항목(validate 가 병합)
    compute_meta.json    compute   : 계산 메타(적용 세율·한도·근거)
    wehago_return.json   normalize : 위하고 신고서 내보내기 파일이 있으면 그 값(대사용, 선택)
    review.json          validate  : list[ReviewItem]  (사람 처리결과는 재실행해도 보존)
    report/              report    : review.html, kakao.txt, review_items.xlsx ...
    state.json           pipeline  : 단계별 상태·시각·오류
    filing.json          pipeline  : 이번 실행의 Filing (작업지시서가 읽음)
    decisions.jsonl      ops       : 사람/에이전트 재분류 감사 기록
    wehago/              wehago    : work_order.json, agent_log.jsonl, 스크린샷·재생 로그
  data/{period}/_summary.json, _dashboard.html, _briefing.md   (전체 거래처 요약)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ParseIssue, ReviewItem, ReviewStatus, Transaction, VatReturn


def _dump(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # 원자적 교체(야간 실행 중 중단돼도 파일 깨짐 방지)


def _load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class Workspace:
    root: Path  # data/{period}/{client_id}

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def report_dir(self) -> Path:
        return self.root / "report"

    # -- transactions
    def save_transactions(self, txns: list[Transaction]) -> None:
        _dump(self.root / "transactions.json", [t.to_dict() for t in txns])

    def load_transactions(self) -> list[Transaction]:
        return [Transaction.from_dict(d) for d in _load(self.root / "transactions.json", [])]

    def save_parse_issues(self, issues: list[ParseIssue]) -> None:
        _dump(self.root / "parse_issues.json", [i.to_dict() for i in issues])

    def load_parse_issues(self) -> list[dict]:
        return _load(self.root / "parse_issues.json", [])

    # -- return
    def save_return(self, r: VatReturn, name: str = "return.json") -> None:
        _dump(self.root / name, r.to_dict())

    def load_return(self, name: str = "return.json") -> VatReturn | None:
        d = _load(self.root / name, None)
        return VatReturn.from_dict(d) if d else None

    # -- review (사람 결정 보존 병합)
    def load_review(self) -> list[ReviewItem]:
        return [ReviewItem.from_dict(d) for d in _load(self.root / "review.json", [])]

    def save_review(self, items: list[ReviewItem], merge: bool = True) -> list[ReviewItem]:
        """merge=True: 기존에 사람이 해결/유지 처리한 항목은 상태를 이어받는다.
        새 실행에서 사라진 항목(원인 해소)은 버린다."""
        if merge:
            old = {i.id: i for i in self.load_review()}
            for it in items:
                prev = old.get(it.id)
                if prev and prev.status != ReviewStatus.OPEN:
                    it.status, it.resolution, it.resolved_by = prev.status, prev.resolution, prev.resolved_by
        _dump(self.root / "review.json", [i.to_dict() for i in items])
        return items

    # -- state
    def load_state(self) -> dict:
        return _load(self.root / "state.json", {"stages": {}})

    def save_state(self, state: dict) -> None:
        _dump(self.root / "state.json", state)

    def save_json(self, name: str, obj: Any) -> None:
        _dump(self.root / name, obj)

    def load_json(self, name: str, default: Any = None) -> Any:
        return _load(self.root / name, default)

"""학습 결과 파일 읽기/쓰기.

  clients/{id}/style.yaml                 거래처 전용 규칙 + 질문 답변 (gitignore)
  config/style/industry/{group}.yaml      업종 공용 규칙(2개 이상 거래처 공통만, 커밋 가능)
  config/style/industry/_all.yaml         전체 공용(가맹점업종 키)
  data/_learn/examples.jsonl              학습 사례(few-shot 검색·eval 용, gitignore)
  data/_learn/questions.yaml              세무사 확인 질문(answer 칸을 채우고 재학습)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .miner import Question, Rule, StyleModel

STYLE_FILE = "style.yaml"
ALL_PACK = "_all"


def _dump_yaml(path: Path, obj: Any, header: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, width=200)
    tmp.write_text((header + "\n" if header else "") + text, encoding="utf-8")
    tmp.replace(path)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 거래처 style.yaml
# ---------------------------------------------------------------------------


def save_client_style(client_dir: Path, client_id: str, group: str, rules: list[Rule], meta: dict | None = None) -> Path:
    p = Path(client_dir) / STYLE_FILE
    obj = {"version": 1, "client_id": client_id, "industry_group": group, "built_at": _now(), **(meta or {}),
           "rules": [r.to_dict() for r in rules]}
    _dump_yaml(p, obj, "# 세무사 전표 스타일(거래처 전용) - python -m taxauto.learn build 가 생성. 실데이터, 커밋 금지.")
    return p


def load_client_style(client_dir: Path) -> tuple[str, list[Rule]]:
    d = _load_yaml(Path(client_dir) / STYLE_FILE)
    cid = str(d.get("client_id") or Path(client_dir).name)
    return str(d.get("industry_group") or ""), [Rule.from_dict(r, cid) for r in d.get("rules") or []]


def load_client_answers(client_dir: Path) -> dict[str, Question]:
    """style.yaml 에 보존된 질문 답변(questions.yaml 이 지워져도 재학습 때 유지)."""
    d = _load_yaml(Path(client_dir) / STYLE_FILE)
    out = {}
    for q in d.get("answers") or []:
        try:
            qq = Question.from_dict(q)
        except (KeyError, TypeError):
            continue
        if qq.answer:
            out[qq.id] = qq
    return out


# ---------------------------------------------------------------------------
# 업종팩
# ---------------------------------------------------------------------------


def default_packs_dir(config_dir: Path | None = None) -> Path:
    from ..law import CONFIG_DIR

    return Path(config_dir or CONFIG_DIR) / "style" / "industry"


def pack_path(packs_dir: Path, group: str) -> Path:
    return Path(packs_dir) / f"{group}.yaml"


def save_pack(packs_dir: Path, group: str, rules: list[Rule], meta: dict | None = None) -> Path:
    p = pack_path(packs_dir, group)
    obj = {"version": 1, "group": group, "built_at": _now(), **(meta or {}), "rules": [r.to_dict() for r in rules]}
    _dump_yaml(p, obj, "# 업종 공용 전표 스타일 - 2개 이상 거래처 공통 규칙만(개인이름형 상호 제외). 학습 시 자동 생성.")
    return p


def load_pack(packs_dir: Path, group: str) -> list[Rule]:
    d = _load_yaml(pack_path(packs_dir, group))
    return [Rule.from_dict(r, group if group != ALL_PACK else "*") for r in d.get("rules") or []]


def load_model(client_id: str, group: str, client_dir: Path | None, packs_dir: Path, cfg: dict | None = None) -> StyleModel:
    """거래처 style.yaml → 업종팩 → 전체팩 순 우선순위 모델."""
    rules: list[tuple[Rule, str]] = []
    if client_dir is not None:
        _, rs = load_client_style(client_dir)
        rules += [(r, f"memory:{client_id}") for r in rs]
    if group:
        rules += [(r, f"industry:{group}") for r in load_pack(packs_dir, group)]
    rules += [(r, f"industry:{ALL_PACK}") for r in load_pack(packs_dir, ALL_PACK)]
    return StyleModel(rules, cfg)


# ---------------------------------------------------------------------------
# 사례·질문
# ---------------------------------------------------------------------------


def save_examples(path: Path, examples: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    tmp.replace(path)


def load_examples(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def save_questions(path: Path, questions: list[Question]) -> None:
    _dump_yaml(path, {"version": 1, "built_at": _now(), "questions": [q.to_dict() for q in questions]},
               "# 세무사 확인 질문: answer 에 옵션 value(또는 label) 하나를 적거나 '거래별'(규칙 안 만듦)을 적고 다시 build.")


def load_questions(path: Path) -> dict[str, Question]:
    d = _load_yaml(Path(path))
    out = {}
    for q in d.get("questions") or []:
        try:
            qq = Question.from_dict(q)
        except (KeyError, TypeError):
            continue
        out[qq.id] = qq
    return out

# taxauto — 세무회계 민 부가가치세 신고 자동화

AI 에이전트(Claude 등)와 사람이 함께 쓰는 저장소다. 작업 전에 이 파일을 먼저 읽는다.

## 목적
WEHAGO(위하고)로 하는 부가세 신고 업무 중 **사람 손이 필요 없는 부분은 밤에 자동으로 끝내고**,
아침에는 **예외(검토 항목)만** 사람이 처리하게 만든다. 직원 1명당 거래처 수를 늘리는 게 목표.

## 3층 구조 (바꾸지 말 것)
1. **엔진(결정적 코드)** `src/taxauto/` — 정규화·공제판정·신고서 집계·검증·리포트. 같은 입력이면 항상 같은 결과.
2. **도구 인터페이스(MCP)** `src/taxauto/agent/mcp_server.py` — 엔진 기능을 AI가 호출하는 도구로 노출.
3. **에이전트(Claude)** `.claude/skills/` — 예외 판단, 미분류 거래 판정, 안내문 작성, 화면 조작.

세법 숫자(세율·한도·기한·가산세율)는 **코드에 쓰지 않는다** → `config/law/*.yaml` (적용기간·근거·검증여부 포함).
사무실 내부 기준은 `config/policy.yaml`.

## 파이프라인
`collect → normalize → enrich → classify → compute → validate → report`
각 단계는 `def run(ctx: RunContext) -> StageResult` (계약: `src/taxauto/context.py`),
결과는 `data/{period}/{client_id}/` 아래 JSON (계약: `src/taxauto/workspace.py`).
공통 모델은 `src/taxauto/models.py` — **필드/Enum 변경·삭제 금지, 추가만 허용**.

## 절대 규칙
- 금액은 int(원). float 금지. 부가세는 원 미만 절사.
- **전자신고 제출(홈택스 최종 제출)은 자동화하지 않는다.** 사람이 승인·제출. 엔진은 "제출 직전"까지만.
- 실데이터(`data/ clients/ inbox/`), 인증서, 비밀번호, API 키는 커밋 금지.
- AI 판정은 항상 `decided_by=llm` + `needs_review` 표시. 사람 결정은 거래처 메모리에 저장되어 다음부터 규칙으로 처리.
- 확실하지 않은 세법 판단은 단정하지 말고 ReviewItem(경고)으로 사람에게 넘긴다.
- 주민등록번호·카드번호 전체 등 민감정보는 리포트·로그·LLM 프롬프트에 넣지 않는다(마스킹).

## 테스트
`python -m pytest -q` — 실데이터 없이 `tests/fixtures/` 의 합성 데이터로 돌아가야 한다.

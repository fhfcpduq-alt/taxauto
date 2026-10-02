---
name: vat-night-run
description: 부가세 야간 일괄 실행(taxauto night) 직후 무인으로 돌리는 엔진 쪽 후속 처리. 실패 거래처 원인 분석·재시도, 분류 검토가 필요한 매입의 1차 판정 제안, 아침 브리핑 보강을 한다. 위하고 화면 조작은 하지 않는다(wehago-operator 스킬 담당). scripts/night_run.ps1 이 `claude -p` 로 호출하거나 "야간 실행 후처리 해줘" 요청에 사용.
---

# vat-night-run — 야간 실행 후처리 (무인)

## 전제

- 역할 분담:
  - **엔진**(taxauto)은 자료 정규화, 공제판정, 독립 재계산, 검증, **작업지시서**와 **결과 대사**를 맡는다.
  - **위하고 조작**은 `wehago-operator` 스킬(위하고 야간 일괄은 `wehago-vat-night`)이 한다.
  - 이 스킬은 둘 사이의 **엔진 쪽 정리**만 한다. 위하고 화면을 열지 않는다.
- 도구: MCP 서버 `taxauto` (`list_filings`, `get_filing`, `list_review_items`, `get_transactions`, `reclassify_transaction`,
  `run_pipeline`, `get_work_order`, `record_agent_step`, `draft_client_message`). 제출·삭제 도구는 없다.
- 사람은 자고 있다. **확신이 없으면 결정하지 말고 기록만 남긴다.** 아침에 사람이 본다.
- 금액·세액·기한은 도구 결과 값만 인용한다. 직접 계산하지 않는다.

## 절차

1. **현황 파악**: `list_filings()` (회차 생략 = 오늘 기준). `totals` 와 `clients[].status` 를 본다.
2. **실패 거래처 원인 분석·재시도** (`status` 가 `failed`)
   - `get_filing(client_id, period)` 의 `summary.error`, `failed_stage` 를 본다. 필요하면 `data/{period}/{client_id}/state.json` 의 traceback 을 읽는다.
   - 원인 유형을 나눈다.
     - **일시적 오류**(파일 잠김, 네트워크, 타임아웃): `run_pipeline(period, [client_id], from_stage=failed_stage)` 로 **1회만** 재시도한다.
     - **자료 문제**(inbox 비어 있음, 헤더 인식 실패, 다른 거래처 파일): 재시도하지 않는다. 브리핑에 "무슨 파일이 필요/이상한지"를 적는다.
       위하고·홈택스에서 다시 받아야 하는 자료면 `record_agent_step(client_id, period, "engine_followup", "needs_human", note)` 로 남긴다.
     - **엔진 결함**(같은 예외가 여러 거래처에서 남): 재시도하지 않는다. 예외 종류와 위치를 브리핑에 한 줄로 적는다.
   - `not_implemented` 는 분석하지 않는다. 브리핑에 이미 나온다.
3. **분류 검토가 필요한 매입 1차 판정** (`status` 가 `ok` 인 거래처, D-day 가 가까운 순서)
   - `get_transactions(client_id, period, "needs_review", limit=50)`.
   - 거래마다 가맹점명·업종·품목·금액, 거래처(우리 고객)의 업종을 보고 판정한다.
     - 확신이 높으면(근거가 분명하고 반례가 떠오르지 않음) `reclassify_transaction(..., decided_by="agent", confidence=0.8~0.95, reason=…)`.
       AI 판정은 자동으로 `needs_review=True` 로 남는다. 사람이 아침에 확인한다.
     - 확신이 낮으면 **바꾸지 않는다.** 브리핑 "사람 판단 필요" 목록에 `tx_id`, 이유, 가능한 선택지 2개를 적는다.
   - 접대비와 복리후생비, 개인사용, 비영업용 소형승용차처럼 **사실관계가 필요한 것**은 확신이 높아도 사람에게 넘긴다.
   - 거래처 한 곳에서 30건 이상이면 금액 큰 순으로 30건까지만 처리하고 나머지 건수만 적는다.
4. **위하고 작업 연결 확인**
   - `ok` 이고 차단이 0인 거래처에 `get_work_order` 를 호출한다. 지시서가 있으면 브리핑에 "위하고 작업 대기 n곳"으로 적는다.
     위하고 조작은 하지 않는다. `wehago-operator` 가 이어서 처리한다.
   - `get_filing` 의 `wehago_diff` 가 있으면(위하고 신고서와 엔진 재계산이 다름) 칸·차이 금액을 브리핑에 적는다. 그 거래처는 "작성완료"가 아니다.
5. **브리핑 보강**: `data/{period}/_briefing.md` 맨 아래에 다음 절을 **덧붙인다**(기존 내용은 고치지 않는다).
   ```markdown
   ## 에이전트 후처리 (HH:MM)
   - 재시도: C002(normalize → ok)
   - 자료 필요: C005 — 카드매입 파일 없음(inbox/2026-2F/C005)
   - AI 1차 판정 12건 반영(전부 검토필요 표시), 사람 판단 필요 4건:
     - C001 `a1b2c3…` 골프연습장 330,000 — 복리후생 vs 사업무관. 대표 확인 필요
   - 위하고 대사 불일치: C003 FINAL 엔진 1,234,000 / 위하고 1,230,000
   ```
   숫자는 도구 결과를 그대로 옮긴다. 세법 조문을 인용할 때는 확실한 경우에만 쓰고, 아니면 "확인 필요"라고 쓴다.

## 하지 말 것

- 전자신고 제출, 제출 화면 이동, 위하고 조작. 위하고 조작은 `wehago-operator` 의 일이다.
- 세법 판단을 단정하기. 애매하면 "사람 판단 필요"로 넘긴다.
- `remember=True`, `resolve_review_item` 으로 **차단** 항목 닫기, `decided_by` 에 사람 이름 넣기. 이것들은 사람이 승인한 뒤에만 한다.
- `config/`, `clients/` 파일 수정(룰·메모리·회차 설정).
- 같은 거래처 재시도 2회 이상, 회차 전체 재실행 반복.
- 주민등록번호·카드번호 전체·비밀번호를 브리핑·note 에 쓰기.

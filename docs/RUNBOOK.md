# 운영 매뉴얼 (RUNBOOK) — 사무실 PC 야간 자동화

대상: 세무사·담당 직원·PC 관리자. 이 엔진은 **밤에 사무실 PC에서 돌고, 아침에는 사람이 예외만 처리**하도록 만들어졌다.
전자신고 **제출은 자동화하지 않는다.** 엔진과 에이전트는 "제출 직전"까지만 한다.

---

## 1. 사무실 PC 설치 (10단계)

전제: 상시 켜 두는 Windows 10/11 PC 1대. 위하고에 세무대리인 인증서가 등록돼 있고, 이 PC에서 위하고 로그인이 된다.

1. **전용 Windows 계정을 만든다.** 예: `taxbot`. 관리자 권한은 주지 않는다. 이 계정으로 로그인해 둔다.
   작업 스케줄러는 "로그온 여부와 관계없이 실행"으로 등록한다.
2. **Python 3.11 이상을 설치한다.** python.org 설치본에서 "Add python.exe to PATH" 를 체크한다.
3. **저장소를 받는다.** `C:\taxauto` 에 git clone 하거나 압축을 푼다. 아래에서는 이 폴더를 `TAXAUTO_HOME` 으로 쓴다.
4. **가상환경을 만들고 설치한다.** PowerShell에서:
   ```powershell
   cd C:\taxauto
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -e ".[agent,dev]"
   ```
5. **환경변수를 등록한다.** (시스템 속성 → 환경 변수 → 사용자 변수)
   - `TAXAUTO_HOME = C:\taxauto`
   - (선택) `TAXAUTO_USER = 홍길동` — 검토 처리 기록에 남는 이름
   - (선택) `ANTHROPIC_API_KEY` — `policy.yaml` 의 `llm.enabled: true` 일 때만
   - (선택) `NTS_SERVICE_KEY` — 국세청 사업자 상태조회(`nts_status.enabled: true`)
   - (선택) `TAXAUTO_AGENT = 1` — 야간 실행 뒤 Claude 에이전트(헤드리스)도 돌릴 때
6. **폴더를 초기화한다.** `taxauto init` 을 실행하면 `clients/ inbox/ data/ logs/` 가 생기고 예시 명부가 복사된다.
7. **거래처 명부를 작성한다.** `clients\clients.yaml` 을 실제 거래처로 바꾼다. 형식은 `examples/clients.example.yaml` 을 따른다.
   거래처 코드는 위하고 회사코드와 같게 쓴다. 회차별 예외(예정고지세액, 직접 처리할 거래처 `skip: true` 등)는
   `clients\{코드}\filings\{회차}.yaml` 에 둔다. 예시는 `examples/filings/` 에 있다.
8. **점검한다.** `taxauto doctor` 의 `[오류]` 가 0이 될 때까지 고친다. `[경고]` 는 내용을 읽고 판단한다.
   특히 "공휴일 목록" 과 "미검증 세법 파라미터" 경고를 확인한다.
9. **시험 실행을 한다.** `taxauto run --period 2026-2P --client C001` → `data\2026-2P\C001\report\review.html` 을 열어 본다.
10. **야간 작업을 등록한다.** 관리자 PowerShell에서 `scripts\register_task.ps1` 을 실행한다. 매일 02:00에 실행되고, 절전 중이면 깨운다.
    전원 옵션에서 "절전 해제 타이머 허용"을 켠다. 다음 날 아침 `logs\YYYY-MM-DD.log` 와 `data\{회차}\_briefing.md` 가 생겼는지 확인한다.

MCP(에이전트 도구) 연결: 저장소 루트의 `.mcp.json` 에 `taxauto` 서버가 등록돼 있다. Claude Code를 `C:\taxauto` 에서 열면 바로 쓸 수 있다.
Windows에서는 `command` 를 `.venv\\Scripts\\python.exe` 로 바꾸는 편이 안전하다.

---

## 2. 신고기마다 운영 순서

회차 코드: `YYYY-1P`(1기 예정, 1~3월분, 4/25 기한) · `YYYY-1F`(1기 확정, 7/25) · `YYYY-2P`(2기 예정, 10/25) · `YYYY-2F`(2기 확정, 다음 해 1/25).
`taxauto night` 은 오늘 날짜로 회차를 자동으로 고른다. 예: 10월이면 2기 예정, 11·12·1월이면 2기 확정.

| 시점 | 할 일 | 넣는 곳 |
|---|---|---|
| 기한 약 3주 전 | 회차 설정 확인: 예정고지세액(개인 확정), 예정신고 여부 변경, 직접 처리할 거래처 `skip: true` | `clients\{코드}\filings\{회차}.yaml` |
| 기한 약 3주 전 ~ | 홈택스·위하고 자료를 내려받아 넣는다. 세금계산서·계산서 매출/매입, 카드 매출/매입, 현금영수증, 판매대행(배민·쿠팡이츠·PG) | `inbox\{회차}\{코드}\` (수임처 일괄 파일은 `inbox\{회차}\_bulk\`) |
| 매일 밤 02:00 | 자동 실행 → 브리핑·대시보드 갱신 | — |
| 매일 아침 | 아래 3절 순서대로 처리 | — |
| 위하고 작업 후 | 위하고 신고서 내보내기 파일을 넣어 대사한다. 에이전트가 하면 MCP `ingest_file` 로 넣는다 | `inbox\{회차}\{코드}\` |
| 기한 3~5일 전 | 차단 0, 대사 일치를 확인한다. 세무사가 승인하고 **사람이 제출**한다. 안내 카톡은 `report\kakao.txt` 를 확인한 뒤 보낸다 | — |
| 신고 후 | 다음 회차에 쓸 값(전기 과세표준, 신용카드발행공제 사용액)을 다음 회차 설정에 적는다 | `filings\{다음회차}.yaml` |

자료는 늦게 들어와도 된다. 넣은 다음 날 아침 결과에 반영된다. 급하면 `taxauto run --period {회차} --client {코드}` 로 바로 돌린다.

---

## 3. 아침 처리 순서 (15~30분)

1. **`data\{회차}\_briefing.md` 를 연다.** 밤사이 결과, 사람이 볼 것 TOP 5, 실패한 거래처, 조언이 있다.
2. **실패한 거래처부터 본다.** 브리핑에 원인 한 줄과 재실행 명령이 있다.
   - `collect`/`normalize` 실패: 자료가 빠졌거나 파일 형식이 바뀐 경우가 대부분이다. inbox를 확인하고 재실행한다.
   - `not_implemented`: 엔진에 아직 없는 단계다. 그 단계는 수작업으로 한다.
3. **`_dashboard.html` 에서 차단이 많은 순으로 거래처를 연다.** 거래처 이름을 누르면 `review.html` 검토서가 열린다.
4. **차단 → 경고 순으로 처리한다.**
   - 위하고에서 고칠 것: `report\review_items.xlsx` 의 "해당거래" 시트(또는 에이전트 작업지시서).
   - 처리 기록: `taxauto review resolve {ID} --status 해결 --note "근거"`
     - 지금 값이 맞다고 확인했으면 `--status 확인후유지`.
     - 같은 결정을 다음부터 자동으로 적용하려면 `--remember` 를 붙인다. 거래처 메모리에 저장된다.
   - Claude와 같이 볼 때는 `vat-review` 스킬을 쓴다.
5. **차단이 0이고 위하고 대사가 일치하면** 세무사가 최종 확인하고 제출한다.
6. 현황은 언제든 `taxauto status --period {회차}` 로 본다.

---

## 4. 장애 대응

| 증상 | 확인 | 조치 |
|---|---|---|
| 아침에 브리핑이 없다 | `logs\{날짜}.log`, 작업 스케줄러 "마지막 실행 결과" | PC 절전·재부팅 여부를 확인한다. 수동 실행: `scripts\night_run.ps1` |
| "다른 실행이 진행 중" | `data\.lock` 내용(pid, 시작 시각) | 실제로 실행 중이면 기다린다. 6시간(`policy.yaml` `run.lock_stale_hours`)이 지나면 자동 해제된다. 급하면 해당 프로세스가 없는지 확인한 뒤 `.lock` 을 지운다 |
| 특정 거래처만 실패 | `data\{회차}\{코드}\state.json` 의 `stages.*.error`, `traceback` | 원인을 고친 뒤 `taxauto run --period {회차} --client {코드} --from-stage {단계}` |
| 전 거래처가 같은 단계에서 실패 | 로그에서 첫 오류를 찾는다 | 설정 파일 문법 오류(`taxauto doctor`)나 엔진 업데이트 문제일 가능성이 크다. 직전 버전으로 되돌린다 |
| 실패한 것만 다시 | — | `taxauto run --period {회차} --only-failed` |
| 기한이 하루 틀림 | `config\holidays.yaml` 에 그 해 공휴일이 있는지 | 공휴일을 추가한다(매년 12월) |
| 납부세액이 위하고와 다름 | `review.html` 의 "위하고 신고서 대사", MCP `get_filing` 의 `wehago_diff` | 칸별 차이를 원인 거래까지 추적한다. 엔진 결과를 맹신하지 않는다. 두 번째 검토자로 쓴다 |

엔진이 하루 이틀 멈춰도 신고는 위하고에서 평소처럼 할 수 있다. 엔진은 보조 도구다.

---

## 5. 보안 수칙

- **인증서**: 공동인증서·세무대리인 인증서 파일(`*.pfx`, `*.der`, `*.key`)과 비밀번호는 저장소 폴더에 두지 않는다. 위하고·브라우저 인증서 저장소나 보안 USB에만 둔다.
- **비밀번호**: 위하고·홈택스 비밀번호를 파일·스크립트·`.env`·스킬 문서에 적지 않는다. 자동 로그인은 브라우저 세션 유지로 한다. 2차 인증은 사람이 한다.
- **API 키**: 환경변수(사용자 변수)로만 둔다. `taxauto doctor` 는 키가 "설정됨/없음"만 보여주고 값은 출력하지 않는다. 키가 노출되면 즉시 폐기하고 재발급한다.
- **실데이터 커밋 금지**: `clients/ inbox/ data/ logs/ secrets/` 는 `.gitignore` 대상이다. 다른 곳으로 옮겨 커밋하지 않는다. 테스트는 `tests/fixtures/` 의 합성 데이터로만 한다.
- **민감정보**: 주민등록번호·카드번호 전체·계좌번호는 리포트·로그·AI 프롬프트에 넣지 않는다. 엔진이 마스킹하지만, 메모(`--note`)에도 쓰지 않는다.
- **에이전트 권한**: 위하고 에이전트 전용 계정에서 전자신고 제출 권한을 뺀다. MCP 도구에는 제출·삭제가 없다. 거래처 메모리 저장(`remember`)은 사람이 승인한 경우에만 한다.
- **PC**: 화면 잠금, Windows 업데이트, 백신을 켜 둔다. 원격접속은 사무실 정책에 따른다. `data/` 는 매일 사내 NAS에 백업한다(외부 클라우드 동기화 폴더에 두지 않는다).

---

## 부록: 파일 위치

```
C:\taxauto\
  clients\clients.yaml                  거래처 명부
  clients\{코드}\filings\{회차}.yaml    회차별 설정
  clients\{코드}\memory.yaml            거래처 학습 메모리
  inbox\{회차}\{코드}\                  내려받은 자료
  data\{회차}\_briefing.md              아침 브리핑
  data\{회차}\_dashboard.html           현황판
  data\{회차}\_summary.json             요약(외부 시스템용, 스키마는 아래)
  data\{회차}\{코드}\report\            review.html · kakao.txt · review_items.xlsx
  data\{회차}\{코드}\state.json         단계별 실행 기록
  logs\YYYY-MM-DD.log                   야간 실행 로그
```

---

## 부록: `_summary.json` 스키마 (`taxauto.summary/v1`)

위치는 `data/{회차}/_summary.json` 이다. 실행이 끝날 때마다 다시 쓴다. 검토 처리(`review resolve`, MCP `resolve_review_item`·`reclassify_transaction`·`record_agent_step`) 뒤에도 다시 쓴다.
대시보드·브리핑·MCP `list_filings`·사무실 업무비서(M)가 이 파일을 읽는다.

**호환 규칙**: `schema` 가 `taxauto.summary/v1` 인 동안 아래 필드는 이름·의미·단위를 바꾸지 않는다(추가만 한다). 깨지는 변경은 `v2` 로 올린다. 읽는 쪽은 모르는 필드를 무시한다.
금액은 전부 정수(원)이다. 날짜는 `YYYY-MM-DD`, 시각은 ISO 8601(시간대 포함)이다. `d_day` 는 `as_of` 기준 남은 일수다(0 = 당일, 음수 = 지남).

| 최상위 필드 | 설명 |
|---|---|
| `schema` | `"taxauto.summary/v1"` |
| `period`, `period_label` | `2026-2P` / `2026년 2기 예정` |
| `generated_at`, `as_of` | 작성 시각 / `d_day` 기준일 |
| `due_date`, `d_day` | 회차 기한(대상 중 가장 이른 것) |
| `last_run` | `{command(night\|run\|mcp), started_at, finished_at, stages[], from_stage, only_failed, client_ids, ran[]}` |
| `totals` | `{clients, ok, failed, not_implemented, skipped, pending, ready_to_file, with_blockers, blocker_open, warn_open, payable_total, refund_total}` |
| `clients[]` | 거래처별 행(아래), 이번 회차 신고 대상만 |
| `not_required[]` | 이번 회차 신고 대상이 아닌 활성 거래처 코드 |
| `unknown_client_ids[]` | 실행 때 지정했지만 명부에 없는 코드 |

| `clients[]` 필드 | 설명 |
|---|---|
| `client_id`, `name`, `biz_no_masked`(`123-45-67***`), `taxpayer_type` | 거래처 |
| `status` | `ok`(정상) · `failed`(단계 오류) · `not_implemented`(단계 모듈 없음) · `skipped`(회차설정 skip, 직접 처리) · `pending`(미실행) |
| `failed_stage`, `error` | 멈춘 단계(`setup` = 설정/기한 계산 오류), 오류 한 줄(마스킹) |
| `stages` | `{단계: 상태}` |
| `final_tax` | `return.json` FINAL(27) 세액. **+납부 / −환급**, 계산 전이면 null |
| `payable`, `refund` | `max(final_tax,0)`, `max(-final_tax,0)` |
| `blocker_open`, `warn_open`, `info_open`, `resolved` | 검토항목 수(미해결 차단/경고/참고, 처리됨) |
| `ready_to_file` | `status=ok` 이고 `final_tax` 가 있고 차단 0 |
| `due_date`, `d_day` | 신고·납부기한(토·일·공휴일이면 다음 영업일)과 남은 일수 |
| `last_run_at` | 이 거래처 마지막 실행 종료 시각 |
| `unverified_law_params` | 계산에 쓴 미검증 세법 파라미터 수 |
| `top_items[]` | 우선 볼 미해결 항목 최대 3개 `{id, severity(차단\|경고\|참고), code, title, tax_impact}` |
| `wehago_status` | 위하고 에이전트의 마지막 작업기록 `{step, status, at, note}` 또는 null |
| `review_html` | 검토서 상대경로 `C001/report/review.html` 또는 null |

관련 파일:
- `data/{회차}/{코드}/state.json`(`taxauto.state/v1`): `stages.{단계}` = `{status, started_at, finished_at, duration_sec, message, counts, error, traceback}`, 그리고 `status, failed_stage, error, last_run, unverified_law_params`.
- `data/{회차}/{코드}/wehago/agent_log.jsonl`: 한 줄에 `{ts, step, status, note, evidence_path, by}` 하나씩. 추가만 한다.
- `data/{회차}/{코드}/decisions.jsonl`: 재분류 감사기록 `{ts, action, tx_id, before, after, by, remember}`.

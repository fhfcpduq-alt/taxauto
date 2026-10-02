# 위하고 절차서 (procedures/)

에이전트(Claude)가 위하고를 조작할 때 읽고, 배운 것을 직접 갱신하는 문서다.
레시피(recipes/*.yaml)가 '기계가 그대로 재생하는 동작 목록'이라면, 절차서는 '왜·무엇을·어떻게 확인하는지'를 적은 사람·AI용 설명이다.

| 파일 | 단계 | 레시피(학습 후 생성) |
|---|---|---|
| 00_login.md | 로그인 상태 확인 | recipes/00_login.yaml |
| 10_switch_company.md | 수임처(회사) 전환 | recipes/10_switch_company.yaml |
| 20_collect_hometax.md | 위하고 내 홈택스 자료 수집 실행/확인 | recipes/20_collect_hometax.yaml |
| 30_auto_journal.md | 매입매출 자동전표 생성 | recipes/30_auto_journal.yaml |
| 40_export_ledger.md | 전표 목록 엑셀 다운로드 → 엔진 ingest | recipes/40_export_ledger.yaml |
| 50_apply_work_order.md | 작업지시서대로 전표 수정 | 부분 레시피(전표 찾기 등) |
| 60_vat_return.md | 부가세 신고서·부속서류 작성, 저장 | recipes/60_vat_return.yaml |
| 70_export_return.md | 신고서 내보내기 → 엔진 대사 | recipes/70_export_return.yaml |
| 80_efile_prepare.md | 전자신고 파일 제작(**제출 금지**) | recipes/80_efile_prepare.yaml |

## 규칙
- 지금 적힌 위하고 메뉴명·버튼명은 전부 **추정**이다(계정 없이 작성). 학습모드에서 실제 화면을 보고 고친다.
- 확인된 내용은 '단계'에 쓰고 '추정' 표시를 지운다. 확인 못 한 것은 계속 '추정'으로 둔다.
- 새로 알게 된 것은 각 문서 맨 아래 **학습 노트**에 한 줄씩 추가한다.
  형식: `- 2026-10-05 21:30 [learn|operator] 내용 (근거: 스크린샷 경로)`
- 비밀번호·인증서·주민번호·카드번호 전체는 절대 적지 않는다.
- 금지 동작(제출·전송·삭제·마감·권한·결제 등)은 어떤 절차서에도 넣지 않는다. 안전장치가 막는다(config/wehago/guard.yaml).
- 금액·세액은 엔진(작업지시서) 값만 쓴다. 에이전트가 계산하지 않는다.

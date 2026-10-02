"""위하고(WEHAGO) 조작 에이전트 지원 모듈.

  guard       금지행동 판정(훅·재생기·페이지 주입 스크립트 공용)
  replay      레시피(recipes/*.yaml) 결정적 재생기 — Playwright(CDP 접속)
  work_order  엔진 결과 → 위하고 작업지시서(work_order.json)
  upload_file 엔진 거래 → 위하고 매입매출전표 업로드 xlsx

전자신고 '제출'은 어떤 경로로도 하지 않는다.
"""

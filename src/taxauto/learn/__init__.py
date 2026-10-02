"""스타일 학습 섹터: 세무사가 위하고에 쓴 전표에서 '전표 작성 스타일'을 배운다.

  ledger_import  위하고 매입매출전표 엑셀 → LedgerEntry
  pairing        홈택스 원천(Transaction) ↔ 전표 짝짓기 → 학습 사례(example)
  features       상호 정규화·업종그룹·가맹점업종·금액구간
  miner          계층별 규칙 추출 + 불일치 질문 + StyleModel(예측)
  build          build / eval / report 오케스트레이션 (python -m taxauto.learn)
  pdf_summary    매출·매입 요약 보고서 PDF 파서 + 신고서 대사

신경망 학습이 아니라 규칙 추출 + 거래처 메모리 + 유사사례 few-shot 조합이다.
모든 예측은 근거(style_source)를 남긴다.
"""

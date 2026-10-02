# 지식 카드 작성 규격 (모든 지식 섹터 공통 계약)

## 위치
`knowledge/cards/{domain}/{id}.md`
domain: `vat`(부가세·사업자등록) · `income`(종합소득세) · `corp`(법인세) · `payroll`(원천세·연말정산·4대보험·급여) · `credit`(세액공제·감면·경정청구)

## 형식
```markdown
---
id: vat-card-issue-credit            # 영문 소문자-하이픈, 전역 유일, 파일명과 동일
title: 신용카드매출전표 등 발행세액공제
domain: vat
tags: [카드매출, 세액공제, 개인사업자, 음식점]   # 검색용 동의어·구어 포함 ("카드공제", "1.3%")
questions:                            # 실제로 이렇게 물어볼 법한 질문 2~5개 (검색 정확도에 큰 영향)
  - 카드매출 세액공제 몇 % 받아요?
  - 법인도 카드발행공제 되나요?
status: 확정                          # 확정 | 해석필요 | 개정예정 | 미확인
valid_from: 2024-01-01                # 이 카드 내용이 맞는 기간 (모르면 생략)
valid_to: 2026-12-31                  # 일몰·개정 예정이면 기재
facts: [card_issue_credit.rate, card_issue_credit.annual_limit]   # 본문에서 참조한 숫자 키
sources:
  - {title: "부가가치세법 제46조", url: "https://www.law.go.kr/법령/부가가치세법/제46조", checked_on: 2026-10-02}
related: [vat-card-sales-reporting]
updated: 2026-10-02
---

## 결론
(2~4줄. 실무자가 이것만 읽어도 처리 가능하게)

## 근거
(조문·예규 요지. 확정된 내용과 해석이 갈리는 내용을 구분)

## 실무 처리
(위하고/홈택스에서 무엇을 어떻게. 계산이 있으면 산식과 예시 숫자)

## 주의사항
(자주 틀리는 것, 예외, 가산세 위험)

## 거래처 안내 문구
(선택. 카톡에 바로 붙일 수 있는 2~4줄, 쉬운 말, 과한 공손체·AI 말투 금지)
```

## 숫자 규칙 (가장 중요)
- 바뀔 수 있는 숫자(세율·한도·기준금액·기한·요율)는 본문에 직접 쓰지 말고 **`{{fact:키}}`** 로 쓴다.
  렌더링 시 '질문 기준일'의 값으로 바뀐다. 특정 시점 값은 `{{fact:키@2027-01-01}}`.
- 숫자 키의 값은 `config/law/{파일}.yaml` 에 둔다(부가세 엔진과 같은 원본). 형식은 `config/law/vat.yaml` 참고:
  `키: [{value, from, to, source, verified, checked_on, note, unit}]`
  unit: `rate`(0.013 → 1.3%) · `won`(10000000 → 10,000,000원) · `fraction`("9/109") · `mmdd`("10-25") · `text` · `count` · `days`
- 파일·키 접두어: vat.yaml(기존 키 유지, 신규 가능) · income.yaml `income.*` · corp.yaml `corp.*` · payroll.yaml `payroll.*`(원천·연말정산) / `ins.*`(4대보험) · credit.yaml `credit.*`
- 예시 계산 속 숫자(예: "매출 1,100만원이면")는 그냥 써도 된다.

## 품질 기준
- 결론 먼저. 실무자는 세무사다 — 기초 용어 설명 생략.
- 확실하지 않으면 `status: 해석필요` 또는 `미확인` 으로 두고 본문에 "확인 필요"를 명시. 지어내지 않는다.
- 근거 URL 은 law.go.kr / nts.go.kr / 국세법령정보시스템 / 기재부 / 4대보험 공단 우선.
- 저작권: 유료 교재·DB 문장 복사 금지. 법령·공공자료 요약은 가능.
- 카드 하나 = 질문 하나에 답하는 단위. 너무 크면 쪼개고 related 로 연결.

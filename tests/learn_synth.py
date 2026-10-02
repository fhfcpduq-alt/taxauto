"""스타일 학습 테스트용 합성 데이터: 업종 3개 x 거래처 8곳의 '세무사 위하고 전표' 엑셀.

세무사 스타일(가정):
  음식점: 식자재마트 카드 → 153 원재료비/카과, 주유 카드 → 822 차량유지비/불공(승용), 골프 → 813 접대비/불공(접대)
  제조업: 같은 주유 카드 → 822 차량유지비/카과(화물), 원자재 세금계산서 → 153 원재료비/과세, 기계 → 206 기계장치(고정자산)
  서비스: 커피 카드 → 811 복리후생비, 사무용품 → 829 사무용품비
노이즈: 행의 약 10%는 계정과목을 다른 값으로 바꾼다.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

import yaml
from openpyxl import Workbook

PERIOD = "2026-1F"

CLIENTS = [
    {"id": "R1", "name": "맛나식당", "biz_no": "1010000001", "industry": "음식점업 한식"},
    {"id": "R2", "name": "행복국밥", "biz_no": "1010000002", "industry": "음식점업 한식"},
    {"id": "R3", "name": "바다횟집", "biz_no": "1010000003", "industry": "음식점 일식"},
    {"id": "M1", "name": "대성금속", "biz_no": "2020000001", "industry": "제조업 금속가공"},
    {"id": "M2", "name": "한빛플라스틱", "biz_no": "2020000002", "industry": "제조 플라스틱"},
    {"id": "M3", "name": "우진기계", "biz_no": "2020000003", "industry": "제조업 기계부품"},
    {"id": "S1", "name": "온디자인", "biz_no": "3030000001", "industry": "서비스 디자인"},
    {"id": "S2", "name": "바른컨설팅", "biz_no": "3030000002", "industry": "서비스 경영컨설팅"},
]
GROUP = {"R": "restaurant", "M": "manufacturing", "S": "service"}

GAS = ["GS칼텍스", "SK에너지", "S-OIL", "현대오일뱅크"]
BRANCHES = ["강남점", "역삼점", "성수점", "신촌점", "부평점", "수원점", "일산점", "분당점"]

# (상호패턴, 문서, 가맹점업종, 계정, 유형, 분개, 불공사유코드, 적요, 금액범위, 횟수, 고정자산)
#  상호패턴의 {gas}/{br} 는 거래처마다 다르게 채움
STYLES = {
    "restaurant": [
        ("식자재왕도매마트 {br}", "card", "식료품", "153 원재료비", "57", "4", "", "{상호} 식자재", (50000, 400000), 14, False),
        ("이마트 {br}", "card", "대형할인점", "830 소모품비", "57", "4", "", "주방소모품", (20000, 150000), 6, False),
        ("{gas} {br}", "card", "주유소", "822 차량유지비", "54", "4", "3", "{상호} 주유", (40000, 90000), 8, False),
        ("레이크골프클럽", "ti", "", "813 접대비", "54", "2", "4", "거래처 접대", (200000, 500000), 3, False),
        ("김민수", "inv", "", "153 원재료비", "53", "1", "", "채소 구입", (100000, 300000), 4, False),
        ("한국전력공사", "ti", "", "815 수도광열비", "51", "2", "", "{월}월 전기요금", (150000, 400000), 6, False),
        ("KT", "ti", "", "814 통신비", "51", "2", "", "{월}월 통신비", (40000, 80000), 6, False),
        ("{card}", "sales_card", "", "401 상품매출", "17", "2", "", "카드매출", (3000000, 9000000), 6, False),
    ],
    "manufacturing": [
        ("{gas} {br}", "card", "주유소", "822 차량유지비", "57", "4", "", "{상호} 화물차 주유", (60000, 150000), 8, False),
        ("동국철강", "ti", "", "153 원재료비", "51", "2", "", "원자재 매입", (1000000, 5000000), 10, False),
        ("이마트 {br}", "card", "대형할인점", "830 소모품비", "57", "4", "", "공장소모품", (20000, 150000), 5, False),
        ("한국공작기계", "ti", "", "206 기계장치", "51", "3", "", "기계장치 구입", (8000000, 20000000), 2, True),
        ("레이크골프클럽", "ti", "", "813 접대비", "54", "2", "4", "거래처 접대", (200000, 500000), 2, False),
        ("한국전력공사", "ti", "", "516 전력비", "51", "2", "", "{월}월 공장 전기료", (500000, 2000000), 6, False),
        ("{customer}", "sales_ti", "", "404 제품매출", "11", "2", "", "제품 매출", (5000000, 20000000), 8, False),
    ],
    "service": [
        ("스타벅스 {br}", "card", "커피전문점", "811 복리후생비", "57", "4", "", "직원 음료", (10000, 60000), 8, False),
        ("오피스디포 {br}", "card", "문구", "829 사무용품비", "57", "4", "", "사무용품", (20000, 200000), 6, False),
        ("이마트 {br}", "card", "대형할인점", "830 소모품비", "57", "4", "", "사무실 소모품", (20000, 150000), 5, False),
        ("{gas} {br}", "card", "주유소", "822 차량유지비", "54", "4", "3", "{상호} 주유", (40000, 90000), 6, False),
        ("KT", "ti", "", "814 통신비", "51", "2", "", "{월}월 통신비", (40000, 80000), 6, False),
        ("{customer}", "sales_ti", "", "411 용역매출", "11", "2", "", "용역 매출", (3000000, 10000000), 6, False),
    ],
}
ACCOUNT_POOL = ["830 소모품비", "829 사무용품비", "811 복리후생비", "812 여비교통비", "822 차량유지비", "153 원재료비"]
CARD_COS = ["신한카드", "비씨카드", "국민카드"]
ND_TEXT = {"3": "3.비영업용소형승용자동차 구입·유지", "4": "4.접대비 및 이와 유사한 비용"}


def _biz(name_key: str) -> str:
    h = abs(hash(name_key)) % 10**7
    return f"6{h:07d}{len(name_key) % 10}0"[:10]


def make_rows(client: dict, rng: random.Random, noise: float = 0.1) -> tuple[list[dict], list[dict]]:
    """반환 (전표 행 dict 목록, 원천 카드매입 Transaction dict 목록)."""
    g = GROUP[client["id"][0]]
    gas = GAS[rng.randrange(len(GAS))]
    br = BRANCHES[rng.randrange(len(BRANCHES))]
    rows, sources = [], []
    start = date(2026, 1, 1)
    bizmap: dict[str, str] = {}
    for (pat, kind, mcat, acct, code, settle, nd, summ, (lo, hi), n, fixed) in STYLES[g]:
        for i in range(n):
            name = pat.format(gas=gas, br=br, card=rng.choice(CARD_COS), customer=f"고객사{rng.randint(1, 40):02d}")
            key = name if "{customer}" not in pat else name
            biz = bizmap.setdefault(key, f"{rng.randint(100, 999)}{rng.randint(10, 99)}{rng.randint(10000, 99999)}")
            d = start + timedelta(days=rng.randrange(0, 181))
            supply = rng.randrange(lo, hi) // 10 * 10
            vat = 0 if code == "53" else supply // 10
            a = acct
            if rng.random() < noise:
                a = rng.choice([x for x in ACCOUNT_POOL if x != acct])
            disp = name.split(" ")[0] if " " in name else name
            s = summ.replace("{상호}", disp).replace("{월}", str(d.month))
            row = {"일자": d, "유형": code, "품목": "", "공급가액": supply, "부가세": vat, "합계": supply + vat,
                   "거래처명": name, "사업자번호": "" if kind == "inv" else biz, "분개": settle, "계정과목": a, "적요": s,
                   "불공제사유": ND_TEXT.get(nd, ""), "고정자산": "Y" if fixed else "", "카드번호": "", "승인번호": ""}
            if kind == "card":
                appr = f"{rng.randint(10000000, 99999999)}"
                row["카드번호"] = "9410-1234-5678-" + str(rng.randint(1000, 9999))
                row["카드사"] = "신한카드"
                sources.append({
                    "client_id": client["id"], "source": "신용카드_매입", "direction": "매입", "doc_type": "신용카드",
                    "tx_date": d.isoformat(), "supply_amount": supply, "vat": vat, "total": supply + vat,
                    "approval_no": appr, "counterparty_biz_no": biz, "counterparty_name": name,
                    "merchant_category": mcat, "card_kind": "사업용신용카드",
                })
                # 일부만 전표에 승인번호(나머지는 일자+금액+사업자번호로 짝짓기)
                if i % 2 == 0:
                    row["승인번호"] = appr
            rows.append(row)
    # 거래처 고유 불일치: R1 의 다이소는 소모품비/사무용품비 반반 → 질문 대상
    if client["id"] == "R1":
        for i in range(6):
            d = start + timedelta(days=rng.randrange(0, 181))
            supply = rng.randrange(5000, 40000) // 10 * 10
            rows.append({"일자": d, "유형": "57", "품목": "", "공급가액": supply, "부가세": supply // 10, "합계": supply + supply // 10,
                         "거래처명": "다이소 마포점", "사업자번호": "1234567890", "분개": "4",
                         "계정과목": "830 소모품비" if i % 2 else "829 사무용품비", "적요": "소모품", "불공제사유": "",
                         "고정자산": "", "카드번호": "", "승인번호": ""})
    rows.sort(key=lambda r: r["일자"])
    return rows, sources


def write_ledger_xlsx(path: Path, client: dict, rows: list[dict], style: int = 0) -> None:
    """style 0: 결합형(유형 '57.카과', 계정 '822 차량유지비'), style 1: 분리형(유형코드/유형명, 계정코드/계정과목명)."""
    from taxauto.learn.ledger_import import ledger_config

    names = {k: v["name"] for k, v in ledger_config()["entry_types"].items()}
    wb = Workbook()
    ws = wb.active
    ws.append(["매입매출전표 조회"])
    ws.append([f"회사: {client['name']}", "기간: 2026년 01월 01일 ~ 2026년 06월 30일"])
    if style == 0:
        hdr = ["일자", "유형", "품목", "공급가액", "부가세", "합계", "거래처명", "사업자번호", "전자", "분개", "계정과목", "적요",
               "불공제사유", "카드사", "카드번호", "고정자산", "승인번호"]
        ws.append(hdr)
        for r in rows:
            ws.append([r["일자"], f"{r['유형']}.{names[r['유형']]}", r["품목"], r["공급가액"], r["부가세"], r["합계"], r["거래처명"],
                       r["사업자번호"], "Y" if r["유형"] in ("51", "54", "11") else "", r["분개"], r["계정과목"], r["적요"],
                       r["불공제사유"], r.get("카드사", ""), r["카드번호"], r["고정자산"], r["승인번호"]])
    else:
        settle_txt = {"1": "현금", "2": "외상", "3": "혼합", "4": "카드"}
        hdr = ["전표일자", "유형코드", "유형명", "공급가액", "세액", "합계금액", "거래처", "사업자등록번호", "분개유형", "계정코드",
               "계정과목명", "적요", "불공사유", "카드번호", "승인번호"]
        ws.append(hdr)
        for r in rows:
            c, _, n = r["계정과목"].partition(" ")
            ws.append([r["일자"].strftime("%Y.%m.%d"), int(r["유형"]), names[r["유형"]], f"{r['공급가액']:,}", r["부가세"], r["합계"],
                       r["거래처명"], r["사업자번호"], settle_txt[r["분개"]], c, n, r["적요"],
                       r["불공제사유"].split(".", 1)[-1], r["카드번호"], r["승인번호"]])
    ws.append(["합계", "", "", sum(r["공급가액"] for r in rows)])
    wb.save(path)


def make_dataset(root: Path, seed: int = 7, noise: float = 0.1, with_sources: tuple[str, ...] = ("R1", "M1", "S1")) -> dict:
    """root/private/2026-1F/{cid}/ 에 전표 엑셀(+원천 transactions.json), root/clients/clients.yaml 생성."""
    rng = random.Random(seed)
    src = root / "private" / PERIOD
    src.mkdir(parents=True, exist_ok=True)
    (root / "clients").mkdir(parents=True, exist_ok=True)
    (root / "clients" / "clients.yaml").write_text(
        yaml.safe_dump({"clients": [{k: v for k, v in c.items()} for c in CLIENTS]}, allow_unicode=True), encoding="utf-8")
    out = {}
    for i, c in enumerate(CLIENTS):
        d = src / c["id"]
        d.mkdir(parents=True, exist_ok=True)
        rows, sources = make_rows(c, rng, noise)
        write_ledger_xlsx(d / f"{c['id']}_매입매출전표.xlsx", c, rows, style=i % 2)
        if c["id"] in with_sources:
            (d / "transactions.json").write_text(json.dumps(sources, ensure_ascii=False), encoding="utf-8")
        out[c["id"]] = {"rows": rows, "sources": sources}
    return out

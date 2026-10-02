"""python -m taxauto.learn {build|eval|report}

  build  --src private/2026-1F --clients clients/clients.yaml
         → clients/{id}/style.yaml, config/style/industry/{group}.yaml,
           data/_learn/examples.jsonl, questions.yaml, style_report.md, data/{period}/{id}/summary_pdf.json
  eval   --holdout 0.25 --seed 1 [--by client|time]
         → data/_learn/eval_YYYYMMDD.md/.json (거래처 단위 홀드아웃 정확도·커버리지·오답 상위)
  report → 현재 학습 현황(사례·규칙·질문·최근 평가)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..law import CONFIG_DIR
from .build import build, render_status, run_eval
from .store import default_packs_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m taxauto.learn", description="세무사 전표 스타일 학습")
    ap.add_argument("--data", default="data", help="작업공간 루트(기본 data)")
    ap.add_argument("--config", default=str(CONFIG_DIR), help="설정 폴더(기본 저장소 config)")
    ap.add_argument("--packs", default="", help="업종팩 출력 폴더(기본 {config}/style/industry)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="학습")
    b.add_argument("--src", required=True, help="private/{period} 폴더")
    b.add_argument("--clients", default="clients/clients.yaml", help="거래처 명부(yaml)")
    b.add_argument("--clients-dir", default="", help="style.yaml 출력 폴더(기본 --clients 의 폴더)")
    e = sub.add_parser("eval", help="홀드아웃 평가")
    e.add_argument("--holdout", type=float, default=0.25)
    e.add_argument("--seed", type=int, default=1)
    e.add_argument("--by", choices=["client", "time"], default="client", help="client=거래처 단위, time=거래처별 기간 뒤쪽")
    r = sub.add_parser("report", help="학습 현황")
    r.add_argument("--clients-dir", default="clients")
    a = ap.parse_args(argv)

    data = Path(a.data)
    config = Path(a.config)
    packs = Path(a.packs) if a.packs else default_packs_dir(config)
    if a.cmd == "build":
        src = Path(a.src)
        if not src.is_dir():
            print(f"폴더 없음: {src}", file=sys.stderr)
            return 2
        clients_yaml = Path(a.clients)
        clients_dir = Path(a.clients_dir) if a.clients_dir else clients_yaml.parent
        res = build(src, clients_yaml if clients_yaml.exists() else None, data, clients_dir, packs, config)
        m = res.mined
        print(f"거래처 {len(res.loads)}곳, 사례 {len(res.examples)}건 → 규칙 거래처 {sum(len(v) for v in m.client_rules.values())} / "
              f"업종 {sum(len(v) for v in m.industry_rules.values())} / 전체 {len(m.all_rules)}, "
              f"질문 {sum(1 for q in m.questions if not q.answer)}")
        print(f"리포트: {res.paths['report']}")
        return 0
    if a.cmd == "eval":
        try:
            res, md = run_eval(data, a.holdout, a.seed, a.by, config_dir=config)
        except FileNotFoundError as ex:
            print(str(ex), file=sys.stderr)
            return 2
        for f, st in res["fields"].items():
            acc = "-" if st["accuracy"] is None else f"{st['accuracy'] * 100:.1f}%"
            cov = "-" if st["coverage"] is None else f"{st['coverage'] * 100:.1f}%"
            print(f"{f:12s} 정확도 {acc:>7s}  커버리지 {cov:>7s}  (n={st['n']})")
        print(f"평가 리포트: {md}")
        return 0
    print(render_status(data, Path(a.clients_dir), packs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

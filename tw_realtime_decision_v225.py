# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List

import pandas as pd

from v225_decision_core import apply_v225_decision, write_meta

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
LIVE = DATA / 'live_intraday.csv'
OUT = DATA / 'v225_realtime_decision.csv'
STATE = DATA / 'v225_decision_state.csv'
META = DATA / 'v225_realtime_decision_meta.json'
TAIPEI = timezone(timedelta(hours=8))


def now_tw() -> datetime:
    return datetime.now(TAIPEI)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', default=str(LIVE))
    parser.add_argument('--output', default=str(OUT))
    parser.add_argument('--state', default=str(STATE))
    args = parser.parse_args(argv)

    inp = Path(args.input)
    outp = Path(args.output)
    statep = Path(args.state)
    if not inp.exists():
        write_meta(META, {'status': 'missing_live_intraday', 'updated_at': now_tw().isoformat(timespec='seconds'), 'input': str(inp)})
        print('missing live_intraday.csv')
        return 0

    df = pd.read_csv(inp, dtype=str)
    decision, state = apply_v225_decision(df, statep)
    decision.to_csv(outp, index=False, encoding='utf-8-sig')
    state.to_csv(statep, index=False, encoding='utf-8-sig')

    meta = {
        'status': 'ok',
        'updated_at': now_tw().isoformat(timespec='seconds'),
        'workflow_version': 'v2.25-realtime-decision',
        'input_rows': int(len(df)),
        'output_rows': int(len(decision)),
        'a_count': int(decision.get('v225決策', pd.Series(dtype=str)).astype(str).str.contains('A級', regex=False).sum()) if not decision.empty else 0,
        'b_count': int(decision.get('v225決策', pd.Series(dtype=str)).astype(str).str.contains('B級', regex=False).sum()) if not decision.empty else 0,
        'no_chase_count': int(decision.get('v225決策', pd.Series(dtype=str)).astype(str).str.contains('不可追', regex=False).sum()) if not decision.empty else 0,
        'note': 'v2.25 用狀態鎖定與二次確認降低一分鐘內反覆打臉；A級不是無腦買，是小量試單區與停損一起給。',
    }
    write_meta(META, meta)
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

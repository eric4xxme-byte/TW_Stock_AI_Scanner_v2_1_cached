# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

import tw_live_engine as eng


def test_decision():
    d = eng.calc_live_decision(price=105, prev=100, high=105, low=101, pct=5, volume=2500, ai=72, risk=40, tech=65, chip=60)
    assert d["盤中強度分"] >= 60
    assert d["即時決策"] in {"可小量試單", "等站穩/回測", "觀察", "不可追", "暫不進場"}


def test_quote_to_row():
    meta = {"3441": {"名稱": "聯一光", "市場": "上市", "產業": "光電業", "AI總分": 70, "風險分": 45}}
    q = {"c": "3441", "n": "聯一光", "ex": "tse", "z": "101", "y": "100", "h": "103", "l": "99", "v": "1234", "t": "13:20:00", "ch": "tse_3441.tw"}
    row = eng.quote_to_row(q, meta, "2026-06-15T10:00:00+08:00")
    assert row is not None
    assert row["代號"] == "3441"
    assert row["盤中現價"] == 101
    assert row["盤中漲跌幅"] == 1


def test_write_outputs(tmp_path=None):
    df = pd.DataFrame([
        {"快照時間": "2026-06-15T10:00:00+08:00", "代號": "3441", "名稱": "聯一光", "盤中現價": 101, "盤中漲跌幅": 1, "盤中成交量": 1234, "即時決策": "觀察"}
    ])
    meta = {"updated_at": "2026-06-15T10:00:00+08:00", "status": "ok", "valid_price_count": 1}
    eng.write_outputs(df, meta)
    assert Path("data/live_intraday.csv").exists()
    assert Path("data/live_intraday_meta.json").exists()
    loaded = json.loads(Path("data/live_intraday_meta.json").read_text(encoding="utf-8"))
    assert loaded["status"] == "ok"


if __name__ == "__main__":
    test_decision()
    test_quote_to_row()
    test_write_outputs()
    print("offline tests passed")

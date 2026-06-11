# -*- coding: utf-8 -*-
"""
台股 AI Scanner v2.3 - 回測器（使用每日快照）

用途：
1. 讀取 data/snapshots/rank_YYYYMMDD.csv
2. 以盤後訊號當日收盤價為基準，計算隔 1 / 3 / 5 個交易日後的收盤報酬
3. 產出：
   - data/backtest_trades.csv
   - data/backtest_summary.csv
   - data/backtest_meta.json

注意：
- 第一天啟用時通常還沒有足夠未來交易日，因此 summary 可能是空的或樣本很少。
- 這不是下單績效，只是先驗證 AI 排名訊號是否有統計優勢。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd
import requests

API_URL = "https://api.finmindtrade.com/api/v4/data"
TW_TZ = timezone(timedelta(hours=8))
DATA_DIR = Path("data")
SNAPSHOT_DIR = DATA_DIR / "snapshots"


def now_tw() -> datetime:
    return datetime.now(TW_TZ)


def normalize_stock_id(value: Any) -> str:
    return str(value).strip().zfill(4)


def finmind_get(dataset: str, data_id: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None, retries: int = 2, timeout: int = 15) -> pd.DataFrame:
    token = os.getenv("FINMIND_TOKEN", "").strip()
    params: Dict[str, Any] = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    if token:
        params["token"] = token

    headers = {"User-Agent": "Mozilla/5.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for i in range(retries):
        try:
            resp = requests.get(API_URL, params=params, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                time.sleep(0.3 + i * 0.3)
                continue
            payload = resp.json()
            return pd.DataFrame(payload.get("data", []))
        except Exception:
            time.sleep(0.3 + i * 0.3)
    return pd.DataFrame()


def read_rank_snapshots(max_snapshots: int = 90) -> pd.DataFrame:
    files = sorted(SNAPSHOT_DIR.glob("rank_*.csv"))
    if not files:
        return pd.DataFrame()

    # 只讀最近 max_snapshots 天，避免 workflow 越跑越慢。
    files = files[-max_snapshots:]
    frames: List[pd.DataFrame] = []

    for f in files:
        try:
            df = pd.read_csv(f)
        except Exception:
            continue
        if df.empty or "代號" not in df.columns:
            continue

        snapshot_key = f.stem.replace("rank_", "")
        if "日期" not in df.columns:
            try:
                df["日期"] = datetime.strptime(snapshot_key, "%Y%m%d").strftime("%Y-%m-%d")
            except Exception:
                df["日期"] = snapshot_key

        df = df.copy()
        df["snapshot_file"] = f.name
        df["snapshot_key"] = snapshot_key
        df["rank"] = np.arange(1, len(df) + 1)
        df["代號"] = df["代號"].astype(str).map(normalize_stock_id)
        frames.append(df)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def prepare_price_history(stock_id: str, signal_date: str, max_horizon: int) -> pd.DataFrame:
    start = pd.to_datetime(signal_date).date()
    # 給足日曆天數，避開週末與假日。5 個交易日抓 15 天通常足夠。
    end = start + timedelta(days=max(20, max_horizon * 4 + 7))
    price = finmind_get("TaiwanStockPrice", data_id=stock_id, start_date=str(start), end_date=str(end), retries=2, timeout=15)
    if price.empty:
        return pd.DataFrame()

    price = price.copy()
    price["date"] = pd.to_datetime(price["date"])
    for col in ["open", "max", "min", "close"]:
        if col in price.columns:
            price[col] = pd.to_numeric(price[col], errors="coerce")
    price = price.sort_values("date").reset_index(drop=True)
    return price


def compute_trade_result(row: pd.Series, horizons: Iterable[int]) -> Dict[str, Any]:
    stock_id = normalize_stock_id(row.get("代號", ""))
    signal_date = str(row.get("日期", ""))[:10]
    max_horizon = max(horizons)

    result: Dict[str, Any] = {
        "訊號日期": signal_date,
        "代號": stock_id,
        "名稱": row.get("名稱", stock_id),
        "市場": row.get("市場", ""),
        "產業": row.get("產業", ""),
        "rank": int(row.get("rank", 0)) if pd.notna(row.get("rank", 0)) else 0,
        "AI總分": row.get("AI總分", np.nan),
        "技術分": row.get("技術分", np.nan),
        "籌碼分": row.get("籌碼分", np.nan),
        "風險分": row.get("風險分", np.nan),
        "snapshot_file": row.get("snapshot_file", ""),
    }

    price = prepare_price_history(stock_id, signal_date, max_horizon)
    if price.empty:
        result["基準收盤價"] = np.nan
        result["狀態"] = "無股價資料"
        for h in horizons:
            result[f"{h}日後收盤價"] = np.nan
            result[f"{h}日報酬率"] = np.nan
        return result

    signal_dt = pd.to_datetime(signal_date)
    signal_rows = price[price["date"] <= signal_dt]
    if signal_rows.empty:
        signal_rows = price[price["date"] >= signal_dt]
    if signal_rows.empty:
        result["基準收盤價"] = np.nan
        result["狀態"] = "無基準價"
        for h in horizons:
            result[f"{h}日後收盤價"] = np.nan
            result[f"{h}日報酬率"] = np.nan
        return result

    # 盤後訊號，用訊號日收盤價當基準。未來報酬看之後第 N 個交易日收盤。
    base_row = signal_rows.iloc[-1]
    base_date = base_row["date"]
    base_close = float(base_row["close"])
    future = price[price["date"] > base_date].reset_index(drop=True)

    result["基準日期"] = base_date.strftime("%Y-%m-%d")
    result["基準收盤價"] = base_close
    result["狀態"] = "完成"

    for h in horizons:
        price_col = f"{h}日後收盤價"
        ret_col = f"{h}日報酬率"
        if len(future) >= h:
            close_h = float(future.iloc[h - 1]["close"])
            result[price_col] = close_h
            result[ret_col] = round((close_h / base_close - 1) * 100, 2) if base_close else np.nan
        else:
            result[price_col] = np.nan
            result[ret_col] = np.nan
            result["狀態"] = "等待未來交易日"

    return result


def summarize_backtest(trades: pd.DataFrame, horizons: Iterable[int], top_groups: Iterable[int]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    if trades.empty:
        return pd.DataFrame()

    for top_n in top_groups:
        group_df = trades[trades["rank"] <= top_n].copy()
        if group_df.empty:
            continue
        for h in horizons:
            ret_col = f"{h}日報酬率"
            valid = group_df[pd.notna(group_df[ret_col])].copy()
            if valid.empty:
                rows.append({
                    "群組": f"Top {top_n}",
                    "期間": f"{h}交易日",
                    "成熟樣本數": 0,
                    "勝率%": np.nan,
                    "平均報酬%": np.nan,
                    "中位數報酬%": np.nan,
                    "最大報酬%": np.nan,
                    "最小報酬%": np.nan,
                })
                continue
            rows.append({
                "群組": f"Top {top_n}",
                "期間": f"{h}交易日",
                "成熟樣本數": int(len(valid)),
                "勝率%": round((valid[ret_col] > 0).mean() * 100, 1),
                "平均報酬%": round(valid[ret_col].mean(), 2),
                "中位數報酬%": round(valid[ret_col].median(), 2),
                "最大報酬%": round(valid[ret_col].max(), 2),
                "最小報酬%": round(valid[ret_col].min(), 2),
            })

    return pd.DataFrame(rows)


def run_backtest(top_n: int = 10, horizons: List[int] | None = None, max_snapshots: int = 90) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    horizons = horizons or [1, 3, 5]

    snapshots = read_rank_snapshots(max_snapshots=max_snapshots)
    if snapshots.empty:
        pd.DataFrame().to_csv(DATA_DIR / "backtest_trades.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame().to_csv(DATA_DIR / "backtest_summary.csv", index=False, encoding="utf-8-sig")
        meta = {
            "updated_at": now_tw().isoformat(timespec="seconds"),
            "status": "no_snapshots",
            "snapshot_count": 0,
            "has_finmind_token": bool(os.getenv("FINMIND_TOKEN", "").strip()),
        }
        (DATA_DIR / "backtest_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print("沒有 snapshots，已產出空白回測檔。")
        return

    # 只回測每日 Top N，避免 API 次數太多。
    snapshots = snapshots[snapshots["rank"] <= top_n].copy()
    trades: List[Dict[str, Any]] = []

    for i, (_, row) in enumerate(snapshots.iterrows(), start=1):
        print(f"[{i}/{len(snapshots)}] 回測 {row.get('日期')} {row.get('代號')} rank={row.get('rank')}")
        trades.append(compute_trade_result(row, horizons))
        time.sleep(0.1)

    trades_df = pd.DataFrame(trades)
    summary_df = summarize_backtest(trades_df, horizons, top_groups=[5, top_n])

    trades_df.to_csv(DATA_DIR / "backtest_trades.csv", index=False, encoding="utf-8-sig")
    summary_df.to_csv(DATA_DIR / "backtest_summary.csv", index=False, encoding="utf-8-sig")

    meta = {
        "updated_at": now_tw().isoformat(timespec="seconds"),
        "status": "ok",
        "snapshot_count": int(snapshots["snapshot_file"].nunique()) if "snapshot_file" in snapshots.columns else 0,
        "trade_rows": int(len(trades_df)),
        "summary_rows": int(len(summary_df)),
        "top_n": int(top_n),
        "horizons": horizons,
        "has_finmind_token": bool(os.getenv("FINMIND_TOKEN", "").strip()),
    }
    (DATA_DIR / "backtest_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print("完成輸出：")
    print(DATA_DIR / "backtest_trades.csv")
    print(DATA_DIR / "backtest_summary.csv")
    print(DATA_DIR / "backtest_meta.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-n", type=int, default=10, help="每日回測 AI 排名前 N 檔")
    parser.add_argument("--horizons", type=str, default="1,3,5", help="交易日週期，例如 1,3,5")
    parser.add_argument("--max-snapshots", type=int, default=90, help="最多讀取最近幾個 snapshot")
    args = parser.parse_args()

    horizons = [int(x.strip()) for x in args.horizons.split(",") if x.strip()]
    run_backtest(top_n=args.top_n, horizons=horizons, max_snapshots=args.max_snapshots)


if __name__ == "__main__":
    main()

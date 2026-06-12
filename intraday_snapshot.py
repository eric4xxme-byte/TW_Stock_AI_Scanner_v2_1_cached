# -*- coding: utf-8 -*-
"""
台股 AI Scanner v2.4 - 盤中快照產生器

用途：
- 不重新計算完整 AI 排名。
- 讀取 data/latest_rank.csv 的候選股清單。
- 用 TWSE MIS 報價端點抓盤中快照。
- 產生 data/intraday_snapshot.csv 與 data/intraday_meta.json。

注意：
- 這是盤中快照，不是券商級逐筆即時報價。
- 法人、融資融券仍以盤後資料為主。
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

DATA_DIR = Path("data")
RANK_FILE = DATA_DIR / "latest_rank.csv"
INTRADAY_FILE = DATA_DIR / "intraday_snapshot.csv"
INTRADAY_META_FILE = DATA_DIR / "intraday_meta.json"

TW_TZ = timezone(timedelta(hours=8))
MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_HOME = "https://mis.twse.com.tw/stock/index.jsp"


def now_tw() -> datetime:
    return datetime.now(TW_TZ)


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "null", "None"}:
        return None
    try:
        if math.isnan(float(text)):
            return None
        return float(text)
    except Exception:
        return None


def safe_int(value: Any) -> Optional[int]:
    f = safe_float(value)
    if f is None:
        return None
    try:
        return int(f)
    except Exception:
        return None


def normalize_code(value: Any) -> str:
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text.zfill(4) if text.isdigit() else text


def chunked(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def build_ex_channels(rank_df: pd.DataFrame, max_count: int) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """建立 MIS ex_ch 清單。

    若 rank_df 有「市場」欄位，依市場決定 tse / otc。
    若沒有市場欄位，為了避免誤判，上市與上櫃都查。
    """
    market_map: Dict[str, Dict[str, Any]] = {}
    ex_channels: List[str] = []

    rows = rank_df.head(max_count).copy()
    for _, row in rows.iterrows():
        code = normalize_code(row.get("代號", row.get("stock_id", "")))
        if not code or not code.isdigit():
            continue
        name = str(row.get("名稱", row.get("stock_name", code))).strip() or code
        market = str(row.get("市場", "")).strip()
        industry = str(row.get("產業", row.get("industry_category", "未知"))).strip() or "未知"
        market_map[code] = {"名稱": name, "市場": market, "產業": industry}

        if "上櫃" in market or market.lower() == "otc":
            ex_channels.append(f"otc_{code}.tw")
        elif "上市" in market or market.lower() in {"tse", "twse"}:
            ex_channels.append(f"tse_{code}.tw")
        else:
            # 市場未知時兩邊都查，之後以有有效價格的報價為準。
            ex_channels.append(f"tse_{code}.tw")
            ex_channels.append(f"otc_{code}.tw")

    # 去重但保留順序
    seen = set()
    unique = []
    for item in ex_channels:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique, market_map


def fetch_mis_quotes(ex_channels: List[str], batch_size: int = 50, timeout: int = 12) -> List[Dict[str, Any]]:
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw",
        "Accept": "application/json,text/plain,*/*",
    }

    # 建立 session；失敗不致命。
    try:
        session.get(MIS_HOME, headers=headers, timeout=timeout)
    except Exception:
        pass

    all_quotes: List[Dict[str, Any]] = []
    for batch in chunked(ex_channels, batch_size):
        params = {
            "ex_ch": "|".join(batch),
            "json": "1",
            "delay": "0",
            "_": str(int(time.time() * 1000)),
        }
        try:
            resp = session.get(MIS_URL, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            quotes = data.get("msgArray", [])
            if isinstance(quotes, list):
                all_quotes.extend(quotes)
        except Exception as exc:
            print(f"[WARN] MIS batch failed: {exc}")
        time.sleep(0.2)
    return all_quotes


def quote_to_row(q: Dict[str, Any], fallback: Dict[str, Dict[str, Any]], snapshot_time: str) -> Optional[Dict[str, Any]]:
    code = normalize_code(q.get("c", ""))
    if not code or not code.isdigit():
        return None

    meta = fallback.get(code, {})
    name = str(q.get("n") or meta.get("名稱") or code).strip()
    market_raw = str(q.get("ex") or meta.get("市場") or "").strip()
    if market_raw == "tse":
        market = "上市"
    elif market_raw == "otc":
        market = "上櫃"
    else:
        market = market_raw or "未知"

    last = safe_float(q.get("z"))
    if last is None:
        # 有些時段 z 會是 '-'，嘗試用最近揭示價格替代。
        last = safe_float(q.get("pz"))
    prev_close = safe_float(q.get("y"))
    open_price = safe_float(q.get("o"))
    high = safe_float(q.get("h"))
    low = safe_float(q.get("l"))

    change = None
    change_pct = None
    if last is not None and prev_close not in {None, 0}:
        change = round(last - float(prev_close), 2)
        change_pct = round((last - float(prev_close)) / float(prev_close) * 100, 2)

    volume = safe_int(q.get("v"))
    tv = safe_int(q.get("tv"))

    status = "正常"
    if last is None:
        status = "無即時成交價"

    return {
        "快照時間": snapshot_time,
        "代號": code,
        "名稱": name,
        "市場": market,
        "產業": meta.get("產業", "未知"),
        "昨收": prev_close,
        "開盤": open_price,
        "最高": high,
        "最低": low,
        "盤中現價": last,
        "盤中漲跌": change,
        "盤中漲跌幅": change_pct,
        "盤中成交量": volume,
        "最近單量": tv,
        "盤中時間": q.get("t", ""),
        "盤中狀態": status,
        "來源": "TWSE MIS snapshot",
    }


def choose_best_quotes(rows: List[Dict[str, Any]], candidate_order: List[str]) -> pd.DataFrame:
    """同一代號可能同時查到 tse / otc，優先保留有價格者。"""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["代號"] = df["代號"].astype(str).map(normalize_code)
    df["_has_price"] = df["盤中現價"].notna().astype(int)
    order_map = {normalize_code(code): idx for idx, code in enumerate(candidate_order)}
    df["_order"] = df["代號"].map(order_map).fillna(99999)
    df = df.sort_values(["_order", "_has_price"], ascending=[True, False])
    df = df.drop_duplicates("代號", keep="first")
    return df.drop(columns=["_has_price", "_order"], errors="ignore").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=30, help="從 latest_rank.csv 取前 N 檔更新盤中快照")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_dt = now_tw()
    snapshot_time = snapshot_dt.isoformat(timespec="seconds")

    if not RANK_FILE.exists():
        meta = {
            "updated_at": snapshot_time,
            "mode": "intraday_snapshot",
            "status": "failed",
            "message": "data/latest_rank.csv 不存在，請先跑 daily scanner。",
            "candidate_count": 0,
            "quote_success_count": 0,
            "quote_failed_count": 0,
        }
        INTRADAY_META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(meta["message"])
        return

    rank_df = pd.read_csv(RANK_FILE)
    if rank_df.empty:
        meta = {
            "updated_at": snapshot_time,
            "mode": "intraday_snapshot",
            "status": "failed",
            "message": "latest_rank.csv 是空的。",
            "candidate_count": 0,
            "quote_success_count": 0,
            "quote_failed_count": 0,
        }
        INTRADAY_META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(meta["message"])
        return

    candidate_codes = [normalize_code(x) for x in rank_df.head(args.limit)["代號"].tolist()]
    ex_channels, fallback = build_ex_channels(rank_df, args.limit)
    raw_quotes = fetch_mis_quotes(ex_channels)
    rows = []
    for q in raw_quotes:
        row = quote_to_row(q, fallback=fallback, snapshot_time=snapshot_time)
        if row is not None:
            rows.append(row)

    intraday_df = choose_best_quotes(rows, candidate_codes)
    intraday_df.to_csv(INTRADAY_FILE, index=False, encoding="utf-8-sig")

    success_count = int(intraday_df["盤中現價"].notna().sum()) if not intraday_df.empty and "盤中現價" in intraday_df.columns else 0
    unique_quote_count = int(intraday_df["代號"].nunique()) if not intraday_df.empty else 0
    failed_count = max(0, len(candidate_codes) - unique_quote_count)

    meta = {
        "updated_at": snapshot_time,
        "mode": "intraday_snapshot",
        "source": "TWSE MIS getStockInfo.jsp",
        "candidate_count": len(candidate_codes),
        "quote_count": unique_quote_count,
        "quote_success_count": success_count,
        "quote_failed_count": failed_count,
        "note": "盤中快照只更新現價、漲跌幅與成交量；法人與融資融券仍沿用盤後資料。",
    }
    INTRADAY_META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

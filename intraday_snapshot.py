# -*- coding: utf-8 -*-
"""
台股 AI Scanner v2.4.1 - 盤中快照產生器（提高報價覆蓋率版）

用途：
- 不重新計算完整 AI 排名。
- 讀取 data/latest_rank.csv 的候選股清單。
- 用 TWSE MIS 報價端點抓盤中快照。
- 產生 data/intraday_snapshot.csv 與 data/intraday_meta.json。

v2.4.1 修正重點：
1. 不再只依「市場」欄位決定 tse / otc；每檔同時嘗試 tse 與 otc，提高命中率。
2. MIS 請求改成多來源、多次重試，並用較小批次避免一次太多股票失敗。
3. 盤中現價 z 沒有時，嘗試 pz / 最佳買賣價估算參考價，避免只有 2 檔能顯示。
4. meta 同時記錄「取得報價檔數」與「有效價格檔數」。

注意：
- 這是盤中快照，不是券商級逐筆即時報價。
- 若使用最佳買賣價估算，盤中狀態會標示為「最佳買賣估算」。
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
from urllib.parse import quote

import pandas as pd
import requests

DATA_DIR = Path("data")
RANK_FILE = DATA_DIR / "latest_rank.csv"
INTRADAY_FILE = DATA_DIR / "intraday_snapshot.csv"
INTRADAY_META_FILE = DATA_DIR / "intraday_meta.json"

TW_TZ = timezone(timedelta(hours=8))
MIS_URLS = [
    "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
    "http://mis.twse.com.tw/stock/api/getStockInfo.jsp",
]
MIS_WARMUP_URLS = [
    "https://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw",
    "https://mis.twse.com.tw/stock/index.jsp",
    "http://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw",
]


def now_tw() -> datetime:
    return datetime.now(TW_TZ)


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "null", "None", "nan"}:
        return None
    # MIS 買賣價常見格式："12.35_12.40_..."，只取第一個有效數字。
    if "_" in text:
        for part in text.split("_"):
            parsed = safe_float(part)
            if parsed is not None:
                return parsed
        return None
    try:
        number = float(text)
        if math.isnan(number):
            return None
        return number
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
    # 有些資料可能帶星號或空白，只保留數字部分。
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 4:
        return digits[:4]
    return digits.zfill(4) if digits else text


def chunked(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def read_rank_candidates(limit: int) -> pd.DataFrame:
    rank_df = pd.read_csv(RANK_FILE, dtype={"代號": str, "stock_id": str})
    if rank_df.empty:
        return rank_df

    code_col = "代號" if "代號" in rank_df.columns else "stock_id"
    rank_df["代號"] = rank_df[code_col].map(normalize_code)
    rank_df = rank_df[rank_df["代號"].astype(str).str.match(r"^\d{4}$", na=False)].copy()
    rank_df = rank_df.drop_duplicates("代號", keep="first").head(limit).reset_index(drop=True)
    return rank_df


def build_ex_channels(rank_df: pd.DataFrame) -> Tuple[List[str], Dict[str, Dict[str, Any]], List[str]]:
    """建立 MIS ex_ch 清單。

    v2.4.1 重要修正：
    每檔同時查 tse 與 otc，不完全依賴 market 欄位。
    因為部分候選股來源/合併資料可能市場欄位缺失或判斷錯誤。
    """
    market_map: Dict[str, Dict[str, Any]] = {}
    ex_channels: List[str] = []
    candidate_order: List[str] = []

    for _, row in rank_df.iterrows():
        code = normalize_code(row.get("代號", row.get("stock_id", "")))
        if not code or not code.isdigit():
            continue
        candidate_order.append(code)

        name = str(row.get("名稱", row.get("stock_name", code))).strip() or code
        market = str(row.get("市場", "")).strip()
        industry = str(row.get("產業", row.get("industry_category", "未知"))).strip() or "未知"
        market_map[code] = {"名稱": name, "市場": market, "產業": industry}

        # 同時查上市與上櫃，提高成功率。choose_best_quotes 會挑有效報價。
        ex_channels.append(f"tse_{code}.tw")
        ex_channels.append(f"otc_{code}.tw")

    seen = set()
    unique = []
    for item in ex_channels:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique, market_map, candidate_order


def build_manual_query_url(base_url: str, batch: List[str]) -> str:
    # 有些環境對 requests params 產生的 %7C 相容性不穩，這裡手動組 URL。
    ex_ch = "%7c".join(quote(x, safe="_.") for x in batch)
    return f"{base_url}?ex_ch={ex_ch}&json=1&delay=0&_={int(time.time() * 1000)}"


def warmup_session(session: requests.Session, headers: Dict[str, str], timeout: int) -> None:
    for url in MIS_WARMUP_URLS:
        try:
            session.get(url, headers=headers, timeout=timeout)
            time.sleep(0.2)
        except Exception:
            continue


def fetch_mis_quotes(ex_channels: List[str], batch_size: int = 20, timeout: int = 12, retries: int = 3) -> List[Dict[str, Any]]:
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
        "Referer": "https://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }

    warmup_session(session, headers, timeout)

    all_quotes: List[Dict[str, Any]] = []
    seen_channels = set()

    for batch in chunked(ex_channels, batch_size):
        batch_success = False
        for attempt in range(retries):
            for base_url in MIS_URLS:
                url = build_manual_query_url(base_url, batch)
                try:
                    resp = session.get(url, headers=headers, timeout=timeout)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    quotes = data.get("msgArray", [])
                    if isinstance(quotes, list) and quotes:
                        for q in quotes:
                            ch = str(q.get("ch", ""))
                            # 同一 channel 不重複加入，但允許同代號 tse/otc 各進來，後面再挑。
                            if ch and ch in seen_channels:
                                continue
                            if ch:
                                seen_channels.add(ch)
                            all_quotes.append(q)
                        batch_success = True
                        break
                except Exception as exc:
                    print(f"[WARN] MIS batch failed attempt={attempt + 1}: {exc}")
            if batch_success:
                break
            time.sleep(0.8 + attempt * 0.6)
        time.sleep(0.35)

    return all_quotes


def extract_market_from_quote(q: Dict[str, Any], fallback_market: str = "") -> str:
    ex = str(q.get("ex") or "").strip().lower()
    ch = str(q.get("ch") or "").strip().lower()
    if ex == "tse" or ch.startswith("tse_"):
        return "上市"
    if ex == "otc" or ch.startswith("otc_"):
        return "上櫃"
    return fallback_market or "未知"


def extract_quote_price(q: Dict[str, Any]) -> Tuple[Optional[float], str, str]:
    """回傳 price, price_source, status。

    MIS 的 z 有時候是 '-'；此時嘗試 pz，再嘗試最佳買賣價估算。
    """
    last = safe_float(q.get("z"))
    if last is not None:
        return last, "成交價", "正常"

    pz = safe_float(q.get("pz"))
    if pz is not None:
        return pz, "最近成交價", "最近成交價"

    bid = safe_float(q.get("b"))
    ask = safe_float(q.get("a"))
    if bid is not None and ask is not None:
        return round((bid + ask) / 2, 2), "最佳買賣均價", "最佳買賣估算"
    if bid is not None:
        return bid, "最佳買價", "最佳買價參考"
    if ask is not None:
        return ask, "最佳賣價", "最佳賣價參考"

    # 最後才用昨收當參考，避免表格空白；狀態會明確標記。
    prev_close = safe_float(q.get("y"))
    if prev_close is not None:
        return prev_close, "昨收參考", "無成交價，昨收參考"

    return None, "無", "無有效報價"


def quote_to_row(q: Dict[str, Any], fallback: Dict[str, Dict[str, Any]], snapshot_time: str) -> Optional[Dict[str, Any]]:
    code = normalize_code(q.get("c", ""))
    if not code or not code.isdigit():
        return None

    meta = fallback.get(code, {})
    name = str(q.get("n") or meta.get("名稱") or code).strip()
    market = extract_market_from_quote(q, fallback_market=str(meta.get("市場", "")).strip())

    price, price_source, status = extract_quote_price(q)
    prev_close = safe_float(q.get("y"))
    open_price = safe_float(q.get("o"))
    high = safe_float(q.get("h"))
    low = safe_float(q.get("l"))

    change = None
    change_pct = None
    if price is not None and prev_close not in {None, 0}:
        change = round(price - float(prev_close), 2)
        change_pct = round((price - float(prev_close)) / float(prev_close) * 100, 2)

    volume = safe_int(q.get("v"))
    tv = safe_int(q.get("tv"))

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
        "盤中現價": price,
        "盤中漲跌": change,
        "盤中漲跌幅": change_pct,
        "盤中成交量": volume,
        "最近單量": tv,
        "盤中時間": q.get("t", ""),
        "價格來源": price_source,
        "盤中狀態": status,
        "來源": "TWSE MIS snapshot",
    }


def choose_best_quotes(rows: List[Dict[str, Any]], candidate_order: List[str]) -> pd.DataFrame:
    """同一代號可能同時查到 tse / otc，優先保留較有效者。"""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["代號"] = df["代號"].astype(str).map(normalize_code)
    order_map = {normalize_code(code): idx for idx, code in enumerate(candidate_order)}
    df["_order"] = df["代號"].map(order_map).fillna(99999)

    # 評分：成交價/最近成交價 > 買賣估算 > 昨收參考 > 無。
    source_score = {
        "成交價": 4,
        "最近成交價": 3,
        "最佳買賣均價": 2,
        "最佳買價": 2,
        "最佳賣價": 2,
        "昨收參考": 1,
        "無": 0,
    }
    df["_score"] = df["價格來源"].map(source_score).fillna(0)
    df["_has_volume"] = df["盤中成交量"].notna().astype(int)
    df = df.sort_values(["_order", "_score", "_has_volume"], ascending=[True, False, False])
    df = df.drop_duplicates("代號", keep="first")
    return df.drop(columns=["_order", "_score", "_has_volume"], errors="ignore").reset_index(drop=True)


def write_failed_meta(snapshot_time: str, message: str) -> None:
    meta = {
        "updated_at": snapshot_time,
        "mode": "intraday_snapshot_v2_4_1",
        "status": "failed",
        "message": message,
        "candidate_count": 0,
        "quote_count": 0,
        "quote_success_count": 0,
        "valid_price_count": 0,
        "quote_failed_count": 0,
    }
    INTRADAY_META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=30, help="從 latest_rank.csv 取前 N 檔更新盤中快照")
    parser.add_argument("--batch-size", type=int, default=20, help="MIS 每批查詢 channel 數，越小越穩但越慢")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_dt = now_tw()
    snapshot_time = snapshot_dt.isoformat(timespec="seconds")

    if not RANK_FILE.exists():
        write_failed_meta(snapshot_time, "data/latest_rank.csv 不存在，請先跑 daily scanner。")
        return

    rank_df = read_rank_candidates(args.limit)
    if rank_df.empty:
        write_failed_meta(snapshot_time, "latest_rank.csv 沒有可用股票代號。")
        return

    ex_channels, fallback, candidate_codes = build_ex_channels(rank_df)
    raw_quotes = fetch_mis_quotes(ex_channels, batch_size=max(2, args.batch_size))

    rows = []
    for q in raw_quotes:
        row = quote_to_row(q, fallback=fallback, snapshot_time=snapshot_time)
        if row is not None:
            rows.append(row)

    intraday_df = choose_best_quotes(rows, candidate_codes)

    # 確保即使某些股票沒有回傳，也保留候選股順序與狀態，讓前台不會誤以為候選消失。
    if not intraday_df.empty:
        got_codes = set(intraday_df["代號"].astype(str).map(normalize_code))
    else:
        got_codes = set()
        intraday_df = pd.DataFrame()

    missing_rows = []
    for code in candidate_codes:
        if code in got_codes:
            continue
        meta = fallback.get(code, {})
        missing_rows.append({
            "快照時間": snapshot_time,
            "代號": code,
            "名稱": meta.get("名稱", code),
            "市場": meta.get("市場", "未知") or "未知",
            "產業": meta.get("產業", "未知"),
            "昨收": None,
            "開盤": None,
            "最高": None,
            "最低": None,
            "盤中現價": None,
            "盤中漲跌": None,
            "盤中漲跌幅": None,
            "盤中成交量": None,
            "最近單量": None,
            "盤中時間": "",
            "價格來源": "無",
            "盤中狀態": "MIS 未回傳",
            "來源": "TWSE MIS snapshot",
        })

    if missing_rows:
        intraday_df = pd.concat([intraday_df, pd.DataFrame(missing_rows)], ignore_index=True)
        order_map = {normalize_code(code): idx for idx, code in enumerate(candidate_codes)}
        intraday_df["_order"] = intraday_df["代號"].astype(str).map(normalize_code).map(order_map).fillna(99999)
        intraday_df = intraday_df.sort_values("_order").drop(columns=["_order"], errors="ignore").reset_index(drop=True)

    intraday_df.to_csv(INTRADAY_FILE, index=False, encoding="utf-8-sig")

    quote_count = int((intraday_df["盤中狀態"] != "MIS 未回傳").sum()) if not intraday_df.empty else 0
    valid_price_count = int(intraday_df["盤中現價"].notna().sum()) if not intraday_df.empty else 0
    trade_price_count = int(intraday_df["價格來源"].isin(["成交價", "最近成交價"]).sum()) if not intraday_df.empty else 0
    failed_count = max(0, len(candidate_codes) - quote_count)

    meta = {
        "updated_at": snapshot_time,
        "mode": "intraday_snapshot_v2_4_1",
        "source": "TWSE MIS getStockInfo.jsp",
        "candidate_count": len(candidate_codes),
        "query_channel_count": len(ex_channels),
        "quote_count": quote_count,
        "quote_success_count": valid_price_count,
        "valid_price_count": valid_price_count,
        "trade_price_count": trade_price_count,
        "quote_failed_count": failed_count,
        "note": "盤中快照只更新現價、漲跌幅與成交量；法人與融資融券仍沿用盤後資料。若價格來源為買賣價或昨收參考，請勿視為正式成交價。",
    }
    INTRADAY_META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

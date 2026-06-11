# -*- coding: utf-8 -*-
"""
台股 AI Scanner v2.2.1 - 後台掃描器（上市來源修正版）

用途：
1. 盤後抓熱門股候選清單
2. 計算技術分 / 風險分
3. 只針對排名前 N 檔補法人與融資融券籌碼
4. 產出 Streamlit 前台可直接讀取的 CSV / JSON

執行：
python scanner_daily.py --limit 30 --chip-limit 10

環境變數：
FINMIND_TOKEN 可選，但建議設定，能提高 FinMind API 穩定度。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

API_URL = "https://api.finmindtrade.com/api/v4/data"
TW_TZ = timezone(timedelta(hours=8))
DATA_DIR = Path("data")

# 備援熱門股清單：只在證交所來源失敗時使用，避免前台完全空白。
FALLBACK_CANDIDATES = [
    {"代號": "2330", "名稱": "台積電", "產業": "電子工業"},
    {"代號": "2317", "名稱": "鴻海", "產業": "其他電子業"},
    {"代號": "2382", "名稱": "廣達", "產業": "電子工業"},
    {"代號": "3231", "名稱": "緯創", "產業": "電子工業"},
    {"代號": "3441", "名稱": "聯一光", "產業": "光電業"},
    {"代號": "6285", "名稱": "啟碁", "產業": "通信網路業"},
    {"代號": "2313", "名稱": "華通", "產業": "電子工業"},
    {"代號": "2409", "名稱": "友達", "產業": "光電業"},
    {"代號": "2344", "名稱": "華邦電", "產業": "半導體業"},
    {"代號": "2618", "名稱": "長榮航", "產業": "航運業"},
    {"代號": "2303", "名稱": "聯電", "產業": "半導體業"},
    {"代號": "2454", "名稱": "聯發科", "產業": "半導體業"},
    {"代號": "2603", "名稱": "長榮", "產業": "航運業"},
    {"代號": "2609", "名稱": "陽明", "產業": "航運業"},
    {"代號": "2615", "名稱": "萬海", "產業": "航運業"},
    {"代號": "3706", "名稱": "神達", "產業": "電腦及週邊設備業"},
    {"代號": "3661", "名稱": "世芯-KY", "產業": "半導體業"},
    {"代號": "3017", "名稱": "奇鋐", "產業": "電子工業"},
    {"代號": "3037", "名稱": "欣興", "產業": "電子工業"},
    {"代號": "2881", "名稱": "富邦金", "產業": "金融保險"},
    {"代號": "2882", "名稱": "國泰金", "產業": "金融保險"},
    {"代號": "2883", "名稱": "開發金", "產業": "金融保險"},
    {"代號": "2884", "名稱": "玉山金", "產業": "金融保險"},
    {"代號": "2891", "名稱": "中信金", "產業": "金融保險"},
    {"代號": "2892", "名稱": "第一金", "產業": "金融保險"},
    {"代號": "2356", "名稱": "英業達", "產業": "電腦及週邊設備業"},
    {"代號": "2379", "名稱": "瑞昱", "產業": "半導體業"},
    {"代號": "2345", "名稱": "智邦", "產業": "通信網路業"},
    {"代號": "4938", "名稱": "和碩", "產業": "電子工業"},
    {"代號": "3711", "名稱": "日月光投控", "產業": "半導體業"},
]


# 上櫃備援清單：只作為上櫃來源測試失敗時的輔助候選。
FALLBACK_OTC_CANDIDATES = [
    {"代號": "8021", "名稱": "尖點", "產業": "電子零組件業", "市場": "上櫃"},
    {"代號": "5347", "名稱": "世界", "產業": "半導體業", "市場": "上櫃"},
    {"代號": "6488", "名稱": "環球晶", "產業": "半導體業", "市場": "上櫃"},
    {"代號": "3260", "名稱": "威剛", "產業": "電子通路業", "市場": "上櫃"},
    {"代號": "3293", "名稱": "鈊象", "產業": "文化創意業", "市場": "上櫃"},
    {"代號": "6147", "名稱": "頎邦", "產業": "半導體業", "市場": "上櫃"},
    {"代號": "6244", "名稱": "茂迪", "產業": "光電業", "市場": "上櫃"},
    {"代號": "3105", "名稱": "穩懋", "產業": "半導體業", "市場": "上櫃"},
    {"代號": "8069", "名稱": "元太", "產業": "光電業", "市場": "上櫃"},
    {"代號": "6187", "名稱": "萬潤", "產業": "其他電子業", "市場": "上櫃"},
]


def now_tw() -> datetime:
    return datetime.now(TW_TZ)


def clean_number(value: Any) -> float:
    if value is None:
        return 0.0
    s = str(value).strip().replace(",", "")
    if s in {"", "--", "-", "nan", "None"}:
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def normalize_stock_id(value: Any) -> str:
    return str(value).strip().zfill(4)


def unique_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        item = normalize_stock_id(item)
        if item not in seen and re.match(r"^\d{4}$", item):
            seen.add(item)
            out.append(item)
    return out


def request_json(url: str, timeout: int = 15) -> Optional[Any]:
    """安全抓 JSON。某些 TWSE / TPEx 端點 content-type 不固定，所以用 text 備援解析。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json,text/plain,*/*",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except Exception:
            text = resp.text.strip().lstrip("\ufeff")
            if not text:
                return None
            try:
                return json.loads(text)
            except Exception:
                return None
    except Exception:
        return None




def roc_date_string(day) -> str:
    """轉成櫃買常見民國日期格式，例如 113/09/03。"""
    return f"{day.year - 1911}/{day.month:02d}/{day.day:02d}"


def pick_value(row: Dict[str, Any], keywords: List[str]) -> Any:
    """從不同 OpenAPI 欄位名稱中找出最可能的值。"""
    lower_items = [(str(k), str(k).lower(), v) for k, v in row.items()]
    for key, key_lower, value in lower_items:
        for kw in keywords:
            kw_lower = kw.lower()
            if kw in key or kw_lower in key_lower:
                return value
    return None


def parse_market_rows(data: Any, market: str, limit: int) -> List[Dict[str, Any]]:
    """解析上市 / 上櫃 OpenAPI 回傳，輸出統一格式。"""
    if not isinstance(data, list) or not data:
        return []

    rows: List[Dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue

        code = (
            pick_value(row, ["證券代號", "代號", "SecuritiesCompanyCode", "Code", "code", "stock_id"])
            or row.get("證券代號")
        )
        name = (
            pick_value(row, ["證券名稱", "名稱", "CompanyName", "Name", "name", "stock_name"])
            or code
        )
        money = pick_value(row, ["成交金額", "TradeValue", "trade_value", "Trading_money", "Amount"])
        volume = pick_value(row, ["成交股數", "成交股", "TradeVolume", "trade_volume", "Trading_Volume", "Volume"])
        close = pick_value(row, ["收盤價", "收盤", "Close", "ClosingPrice", "close"])

        if code is None:
            continue
        code = normalize_stock_id(code)
        if not re.match(r"^\d{4}$", code):
            continue

        money_num = clean_number(money)
        close_num = clean_number(close)
        volume_num = clean_number(volume)
        if money_num <= 0 and close_num > 0 and volume_num > 0:
            money_num = close_num * volume_num

        rows.append(
            {
                "代號": code,
                "名稱": str(name).strip() if name else code,
                "產業": "未知",
                "市場": market,
                "成交金額": money_num,
                "收盤價來源": close_num,
            }
        )

    rows = sorted(rows, key=lambda x: x.get("成交金額", 0), reverse=True)
    return rows[:limit]

def parse_twse_mi_index_payload(payload: Any, market: str = "上市", limit: int = 100) -> List[Dict[str, Any]]:
    """解析證交所 MI_INDEX JSON。支援新版 rwd 與舊版 exchangeReport data9/fields9。"""
    if not isinstance(payload, dict):
        return []

    # 舊版 exchangeReport/MI_INDEX 常見格式：fields9 + data9
    fields = payload.get("fields9") or payload.get("fields")
    data = payload.get("data9") or payload.get("data")

    # 新版 rwd 有時會包在 tables 裡。
    if (not fields or not data) and isinstance(payload.get("tables"), list):
        for table in payload.get("tables", []):
            if not isinstance(table, dict):
                continue
            f = table.get("fields") or table.get("fields9")
            d = table.get("data") or table.get("data9")
            if f and d and any("證券代號" in str(x) for x in f):
                fields, data = f, d
                break

    if not fields or not data:
        return []

    rows: List[Dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, list):
            continue
        col_len = min(len(fields), len(raw))
        row = {str(fields[i]): raw[i] for i in range(col_len)}
        code = row.get("證券代號") or row.get("Code") or row.get("code")
        name = row.get("證券名稱") or row.get("Name") or row.get("name") or code
        money = row.get("成交金額") or row.get("TradeValue") or row.get("trade_value")
        close = row.get("收盤價") or row.get("ClosingPrice") or row.get("close")
        volume = row.get("成交股數") or row.get("成交股") or row.get("TradeVolume") or row.get("Trading_Volume")

        if code is None:
            continue
        code = normalize_stock_id(code)
        if not re.match(r"^\d{4}$", code):
            continue

        money_num = clean_number(money)
        close_num = clean_number(close)
        volume_num = clean_number(volume)
        if money_num <= 0 and close_num > 0 and volume_num > 0:
            money_num = close_num * volume_num

        rows.append({
            "代號": code,
            "名稱": str(name).strip() if name else code,
            "產業": "未知",
            "市場": market,
            "成交金額": money_num,
            "收盤價來源": close_num,
        })

    rows = sorted(rows, key=lambda x: clean_number(x.get("成交金額")), reverse=True)
    return rows[:limit]


def fetch_twse_mi_index(limit: int) -> Tuple[List[Dict[str, Any]], str]:
    """MI_INDEX 備援：往前找最近交易日的每日收盤行情。"""
    today = now_tw().date()
    for i in range(0, 15):
        day = today - timedelta(days=i)
        date_str = day.strftime("%Y%m%d")
        urls = [
            f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date_str}&type=ALLBUT0999&response=json",
            f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date_str}&type=ALLBUT0999",
        ]
        for url in urls:
            payload = request_json(url, timeout=15)
            rows = parse_twse_mi_index_payload(payload, market="上市", limit=limit)
            if rows:
                return rows[:limit], f"證交所上市成交金額排行（MI_INDEX {date_str}）"
    return [], "證交所 MI_INDEX 抓取失敗"


def fetch_twse_stock_day_all_openapi(limit: int) -> Tuple[List[Dict[str, Any]], str]:
    """抓證交所上市個股日成交資訊。

    v2.2.1 修正：
    1. 先用原本 v2.1 成功過的 STOCK_DAY_ALL 來源。
    2. 如果該端點短暫失敗，再往前找 MI_INDEX 最近交易日。
    3. 最後才回傳失敗，讓上櫃或備援清單接手。
    """
    urls = [
        "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
        "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=open_data",
        "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=open_data",
    ]

    for url in urls:
        data = request_json(url, timeout=15)
        rows = parse_market_rows(data, market="上市", limit=limit)
        if rows:
            return rows[:limit], "證交所上市成交金額排行（STOCK_DAY_ALL）"

    # STOCK_DAY_ALL 有時會暫時抓不到，改用 MI_INDEX 最近交易日資料。
    mi_rows, mi_source = fetch_twse_mi_index(limit)
    if mi_rows:
        return mi_rows[:limit], mi_source

    return [], "證交所 STOCK_DAY_ALL / MI_INDEX 皆抓取失敗"


def fetch_tpex_mainboard_quotes(limit: int) -> Tuple[List[Dict[str, Any]], str]:
    """抓櫃買中心 OpenAPI 上櫃股票收盤行情。

    優先使用不需日期的 tpex_mainboard_quotes；若失敗，再用每日收盤行情日期格式往前找。
    """
    urls = [
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes",
    ]

    for url in urls:
        data = request_json(url, timeout=15)
        rows = parse_market_rows(data, market="上櫃", limit=limit)
        if rows:
            return rows[:limit], "櫃買中心上櫃成交金額排行（tpex_mainboard_quotes）"

    # 日期版 fallback：往前找最近 10 天，避開假日與資料尚未更新。
    today = now_tw().date()
    for i in range(0, 10):
        d = roc_date_string(today - timedelta(days=i))
        url = f"https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes?l=zh-tw&d={d}&s=0,asc,0"
        data = request_json(url, timeout=15)
        rows = parse_market_rows(data, market="上櫃", limit=limit)
        if rows:
            return rows[:limit], f"櫃買中心上櫃成交金額排行（日收盤行情 {d}）"

    return [], "櫃買中心上櫃來源抓取失敗"


def get_candidates(limit: int) -> Tuple[List[str], Dict[str, Dict[str, str]], str]:
    """抓上市 + 上櫃熱門股候選。

    若上市來源成功：上市與上櫃依成交金額合併排序。
    若上市來源失敗但上櫃成功：保留部分上市備援清單，避免候選股全部變成上櫃。
    """
    twse_rows, twse_source = fetch_twse_stock_day_all_openapi(limit)
    tpex_rows, tpex_source = fetch_tpex_mainboard_quotes(limit)

    if not twse_rows and not tpex_rows:
        candidate_rows = []
        for r in FALLBACK_CANDIDATES[:limit]:
            rr = dict(r)
            rr["市場"] = "上市"
            rr["成交金額"] = 0
            candidate_rows.append(rr)
        source = "備援熱門股清單"

    elif not twse_rows and tpex_rows:
        # 上市來源短暫失敗時，不讓候選清單全部變上櫃；保留一部分原本穩定的上市備援。
        fallback_count = min(max(5, limit // 3), len(FALLBACK_CANDIDATES), limit)
        tpex_count = max(0, limit - fallback_count)
        candidate_rows = []
        candidate_rows.extend(tpex_rows[:tpex_count])
        for r in FALLBACK_CANDIDATES[:fallback_count]:
            rr = dict(r)
            rr["市場"] = "上市"
            rr["成交金額"] = 0
            candidate_rows.append(rr)
        source = f"上櫃成交金額排行 + 上市備援清單｜{twse_source}；{tpex_source}"

    elif twse_rows and not tpex_rows:
        candidate_rows = twse_rows[:limit]
        source = f"上市成交金額排行｜{twse_source}；{tpex_source}"

    else:
        # 上市與上櫃都成功：依成交金額合併排序後取前 limit。
        by_code: Dict[str, Dict[str, Any]] = {}
        for row in [*twse_rows, *tpex_rows]:
            sid = normalize_stock_id(row.get("代號"))
            if not re.match(r"^\d{4}$", sid):
                continue
            row["代號"] = sid
            if sid not in by_code or clean_number(row.get("成交金額")) > clean_number(by_code[sid].get("成交金額")):
                by_code[sid] = row
        candidate_rows = sorted(by_code.values(), key=lambda x: clean_number(x.get("成交金額")), reverse=True)[:limit]
        source = f"上市+上櫃成交金額排行｜{twse_source}；{tpex_source}"

    candidates = unique_keep_order([r["代號"] for r in candidate_rows])[:limit]
    info_map = {
        normalize_stock_id(r["代號"]): {
            "名稱": str(r.get("名稱") or r["代號"]).strip(),
            "產業": str(r.get("產業") or "未知").strip(),
            "市場": str(r.get("市場") or "未知").strip(),
        }
        for r in candidate_rows
    }
    return candidates, info_map, source


def finmind_get(dataset: str, data_id: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None, retries: int = 2, timeout: int = 12) -> pd.DataFrame:
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
            df = pd.DataFrame(payload.get("data", []))
            return df
        except Exception:
            time.sleep(0.3 + i * 0.3)
    return pd.DataFrame()


def get_stock_info(candidate_info: Dict[str, Dict[str, str]]) -> pd.DataFrame:
    df = finmind_get("TaiwanStockInfo", timeout=15)
    rows = []

    if not df.empty:
        cols = df.columns.tolist()
        for _, r in df.iterrows():
            sid = normalize_stock_id(r.get("stock_id", ""))
            if not re.match(r"^\d{4}$", sid):
                continue
            rows.append(
                {
                    "代號": sid,
                    "名稱": r.get("stock_name") if "stock_name" in cols else sid,
                    "產業": r.get("industry_category") if "industry_category" in cols else "未知",
                    "市場": "未知",
                }
            )

    # 候選來源名稱作為 fallback，避免名稱突然變回代號。
    for sid, info in candidate_info.items():
        rows.append({"代號": sid, "名稱": info.get("名稱", sid), "產業": info.get("產業", "未知"), "市場": info.get("市場", "未知")})

    # 常用備援表最後補。
    for r in FALLBACK_CANDIDATES:
        rows.append({"代號": r["代號"], "名稱": r["名稱"], "產業": r["產業"], "市場": r.get("市場", "上市")})

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=["代號", "名稱", "產業", "市場"])

    out["代號"] = out["代號"].astype(str).map(normalize_stock_id)
    out["名稱"] = out["名稱"].fillna(out["代號"]).astype(str)
    out["產業"] = out["產業"].fillna("未知").astype(str)
    if "市場" not in out.columns:
        out["市場"] = "未知"
    out["市場"] = out["市場"].fillna("未知").astype(str)

    # 保留第一個出現的名稱。FinMind 資料優先，其次候選來源，再其次 fallback。
    out = out.drop_duplicates(subset=["代號"], keep="first")
    return out


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "max", "min", "close", "Trading_Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_values("date").reset_index(drop=True)
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["vol_ma5"] = df["Trading_Volume"].rolling(5).mean()
    df["vol_ma20"] = df["Trading_Volume"].rolling(20).mean()
    df["high20_prev"] = df["max"].rolling(20).max().shift(1)
    df["volume_ratio"] = df["Trading_Volume"] / df["vol_ma5"]
    df["bias5"] = (df["close"] - df["ma5"]) / df["ma5"] * 100
    df["bias20"] = (df["close"] - df["ma20"]) / df["ma20"] * 100
    df["daily_range"] = df["max"] - df["min"]
    df["upper_shadow"] = df["max"] - df[["open", "close"]].max(axis=1)
    df["upper_shadow_ratio"] = np.where(df["daily_range"] > 0, df["upper_shadow"] / df["daily_range"], 0)
    df["near_high_ratio"] = np.where(df["daily_range"] > 0, (df["close"] - df["min"]) / df["daily_range"], 0)
    df["breakout_20d"] = df["close"] > df["high20_prev"]
    df["price_change_pct"] = df["close"].pct_change() * 100
    return df


def score_stock(price_df: pd.DataFrame) -> Tuple[Optional[Dict[str, Any]], pd.DataFrame]:
    if price_df.empty or len(price_df) < 60:
        return None, pd.DataFrame()

    df = add_indicators(price_df)
    latest = df.iloc[-1]
    sid = normalize_stock_id(latest.get("stock_id", ""))

    close = float(latest["close"])
    technical_score = 0
    risk_score = 0
    reasons: List[str] = []
    risks: List[str] = []

    for ma_col, label, pts in [("ma5", "站上5日線", 15), ("ma10", "站上10日線", 15), ("ma20", "站上20日線", 15), ("ma60", "站上60日線", 10)]:
        if pd.notna(latest[ma_col]) and close > float(latest[ma_col]):
            technical_score += pts
            reasons.append(label)

    if bool(latest["breakout_20d"]):
        technical_score += 20
        reasons.append("突破近20日高點")
    if pd.notna(latest["volume_ratio"]) and latest["volume_ratio"] >= 1.5:
        technical_score += 15
        reasons.append("成交量放大")
    if pd.notna(latest["near_high_ratio"]) and latest["near_high_ratio"] >= 0.7:
        technical_score += 10
        reasons.append("收盤接近當日高點")
    if pd.notna(latest["price_change_pct"]) and latest["price_change_pct"] > 0:
        technical_score += 5
        reasons.append("今日收漲")

    if latest["upper_shadow_ratio"] >= 0.45 and latest["volume_ratio"] >= 1.5:
        risk_score += 30
        risks.append("爆量長上影")
    if latest["bias5"] >= 10:
        risk_score += 20
        risks.append("短線乖離過大")
    if latest["bias20"] >= 20:
        risk_score += 20
        risks.append("波段乖離過大")
    if latest["volume_ratio"] >= 3 and latest["price_change_pct"] < 1:
        risk_score += 20
        risks.append("爆量但漲不動")
    if pd.notna(latest["ma5"]) and close < latest["ma5"]:
        risk_score += 20
        risks.append("跌破5日線")

    technical_score = min(100, int(technical_score))
    risk_score = min(100, int(risk_score))

    if technical_score >= 75 and risk_score <= 40:
        if bool(latest["breakout_20d"]):
            entry_note = "強勢突破型：不要早盤追高，等回測支撐或尾盤確認"
        else:
            entry_note = "偏多型：可觀察回測5日線或10日線"
    elif technical_score >= 60:
        entry_note = "觀察型：等待更明確突破或量價確認"
    else:
        entry_note = "暫不進場：分數不足或風險偏高"

    stop_loss = latest["ma5"] if pd.notna(latest["ma5"]) else latest["min"]
    pressure = latest["high20_prev"] if pd.notna(latest["high20_prev"]) else latest["max"]

    base = {
        "日期": latest["date"].date().isoformat(),
        "代號": sid,
        "收盤價": round(close, 2),
        "技術分": technical_score,
        "風險分": risk_score,
        "量比": round(float(latest["volume_ratio"]) if pd.notna(latest["volume_ratio"]) else 0, 2),
        "5日乖離率": round(float(latest["bias5"]) if pd.notna(latest["bias5"]) else 0, 2),
        "20日乖離率": round(float(latest["bias20"]) if pd.notna(latest["bias20"]) else 0, 2),
        "突破20日高點": bool(latest["breakout_20d"]),
        "AI進場判斷": entry_note,
        "停損參考": round(float(stop_loss), 2) if pd.notna(stop_loss) else None,
        "壓力參考": round(float(pressure), 2) if pd.notna(pressure) else None,
        "技術面原因": "、".join(reasons) if reasons else "技術面無明顯加分",
        "技術風險": "、".join(risks) if risks else "暫無明顯高風險訊號",
    }

    hist_cols = ["date", "stock_id", "open", "max", "min", "close", "Trading_Volume", "ma5", "ma10", "ma20"]
    hist = df[[c for c in hist_cols if c in df.columns]].copy()
    return base, hist


def get_institutional_summary(stock_id: str, start_date: str, end_date: str) -> Dict[str, int]:
    df = finmind_get("TaiwanStockInstitutionalInvestorsBuySell", data_id=stock_id, start_date=start_date, end_date=end_date, retries=1, timeout=10)
    if df.empty or "buy" not in df.columns or "sell" not in df.columns:
        return {"inst_net_1d": 0, "inst_net_3d": 0}

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["buy"] = pd.to_numeric(df["buy"], errors="coerce").fillna(0)
    df["sell"] = pd.to_numeric(df["sell"], errors="coerce").fillna(0)
    df["net"] = df["buy"] - df["sell"]
    daily = df.groupby("date")["net"].sum().reset_index().sort_values("date")
    if daily.empty:
        return {"inst_net_1d": 0, "inst_net_3d": 0}
    return {"inst_net_1d": int(daily.iloc[-1]["net"]), "inst_net_3d": int(daily.tail(3)["net"].sum())}


def get_margin_summary(stock_id: str, start_date: str, end_date: str) -> Dict[str, int]:
    df = finmind_get("TaiwanStockMarginPurchaseShortSale", data_id=stock_id, start_date=start_date, end_date=end_date, retries=1, timeout=10)
    if df.empty:
        return {"margin_change": 0, "short_change": 0}

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    latest = df.iloc[-1]
    required = ["MarginPurchaseTodayBalance", "MarginPurchaseYesterdayBalance", "ShortSaleTodayBalance", "ShortSaleYesterdayBalance"]
    if not all(c in df.columns for c in required):
        return {"margin_change": 0, "short_change": 0}

    mt = clean_number(latest["MarginPurchaseTodayBalance"])
    my = clean_number(latest["MarginPurchaseYesterdayBalance"])
    st = clean_number(latest["ShortSaleTodayBalance"])
    sy = clean_number(latest["ShortSaleYesterdayBalance"])
    return {"margin_change": int(mt - my), "short_change": int(st - sy)}


def calculate_chip_score(inst_net_1d: int, inst_net_3d: int, margin_change: int, short_change: int) -> Tuple[int, str, str]:
    chip_score = 50
    chip_reasons: List[str] = []
    chip_risks: List[str] = []

    if inst_net_1d > 0:
        chip_score += 15
        chip_reasons.append("法人單日買超")
    elif inst_net_1d < 0:
        chip_score -= 15
        chip_risks.append("法人單日賣超")

    if inst_net_3d > 0:
        chip_score += 15
        chip_reasons.append("法人近3日合計買超")
    elif inst_net_3d < 0:
        chip_score -= 10
        chip_risks.append("法人近3日合計賣超")

    if margin_change > 1000:
        chip_score -= 20
        chip_risks.append("融資大增，散戶追高風險")
    elif margin_change > 300:
        chip_score -= 10
        chip_risks.append("融資增加，籌碼略偏雜")
    elif margin_change < -300:
        chip_score += 10
        chip_reasons.append("融資減少，籌碼較乾淨")

    if short_change > 300:
        chip_score += 5
        chip_reasons.append("融券增加，可能有軋空動能")
    elif short_change < -300:
        chip_score -= 5
        chip_risks.append("融券回補，短線軋空力道可能減弱")

    chip_score = max(0, min(int(chip_score), 100))
    return chip_score, "、".join(chip_reasons) if chip_reasons else "籌碼無明顯加分", "、".join(chip_risks) if chip_risks else "籌碼無明顯風險"


def compute_ai_score(technical_score: float, chip_score: float, risk_score: float) -> float:
    return round(technical_score * 0.55 + chip_score * 0.35 - risk_score * 0.10, 1)


def scan(limit: int = 30, chip_limit: int = 10) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    end = now_tw().date()
    start = end - timedelta(days=220)

    candidates, candidate_info, candidate_source = get_candidates(limit)
    stock_info = get_stock_info(candidate_info)
    info_map = stock_info.set_index("代號")[["名稱", "產業", "市場"]].to_dict("index") if not stock_info.empty else {}

    results: List[Dict[str, Any]] = []
    histories: List[pd.DataFrame] = []
    skipped: List[str] = []

    for idx, stock_id in enumerate(candidates, start=1):
        print(f"[{idx}/{len(candidates)}] 技術分析 {stock_id}")
        price = finmind_get("TaiwanStockPrice", data_id=stock_id, start_date=str(start), end_date=str(end), retries=2, timeout=12)
        base, hist = score_stock(price)
        if base is None:
            skipped.append(stock_id)
            continue

        base["籌碼分"] = 50
        base["法人單日買賣超"] = 0
        base["法人近3日買賣超"] = 0
        base["融資變化"] = 0
        base["融券變化"] = 0
        base["籌碼面原因"] = "快速模式：尚未抓取籌碼細節"
        base["籌碼風險"] = "快速模式：尚未抓取籌碼細節"
        base["籌碼狀態"] = "未抓"
        base["AI總分"] = compute_ai_score(base["技術分"], base["籌碼分"], base["風險分"])

        info = info_map.get(base["代號"], {})
        base["名稱"] = info.get("名稱", base["代號"])
        base["產業"] = info.get("產業", "未知")
        base["市場"] = info.get("市場", candidate_info.get(base["代號"], {}).get("市場", "未知"))
        results.append(base)

        if not hist.empty:
            histories.append(hist)
        time.sleep(0.1)

    result_df = pd.DataFrame(results)
    if result_df.empty:
        meta = {
            "updated_at": now_tw().isoformat(timespec="seconds"),
            "candidate_source": candidate_source,
            "requested_limit": limit,
            "candidate_count": len(candidates),
            "success_count": 0,
            "skipped_count": len(skipped),
            "chip_detail_limit": chip_limit,
            "chip_fetched_count": 0,
            "skipped_stock_ids": skipped,
        }
        (DATA_DIR / "latest_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        pd.DataFrame().to_csv(DATA_DIR / "latest_rank.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame().to_csv(DATA_DIR / "latest_risk.csv", index=False, encoding="utf-8-sig")
        return

    # 先依快速 AI 分數排序，再對前 N 檔補籌碼。
    result_df = result_df.sort_values("AI總分", ascending=False).reset_index(drop=True)
    chip_targets = result_df.head(min(chip_limit, len(result_df)))["代號"].astype(str).tolist()
    chip_fetched = 0

    for stock_id in chip_targets:
        print(f"籌碼補抓 {stock_id}")
        inst = get_institutional_summary(stock_id, str(start), str(end))
        margin = get_margin_summary(stock_id, str(start), str(end))
        chip_score, chip_reasons, chip_risks = calculate_chip_score(
            inst["inst_net_1d"], inst["inst_net_3d"], margin["margin_change"], margin["short_change"]
        )
        mask = result_df["代號"].astype(str) == stock_id
        result_df.loc[mask, "法人單日買賣超"] = inst["inst_net_1d"]
        result_df.loc[mask, "法人近3日買賣超"] = inst["inst_net_3d"]
        result_df.loc[mask, "融資變化"] = margin["margin_change"]
        result_df.loc[mask, "融券變化"] = margin["short_change"]
        result_df.loc[mask, "籌碼分"] = chip_score
        result_df.loc[mask, "籌碼面原因"] = chip_reasons
        result_df.loc[mask, "籌碼風險"] = chip_risks
        result_df.loc[mask, "籌碼狀態"] = "已抓"
        result_df.loc[mask, "AI總分"] = result_df.loc[mask].apply(
            lambda r: compute_ai_score(r["技術分"], r["籌碼分"], r["風險分"]), axis=1
        )
        chip_fetched += 1
        time.sleep(0.15)

    result_df = result_df.sort_values("AI總分", ascending=False).reset_index(drop=True)

    show_order = [
        "日期", "代號", "名稱", "市場", "產業", "收盤價", "AI總分", "技術分", "籌碼分", "風險分", "量比",
        "法人單日買賣超", "法人近3日買賣超", "融資變化", "融券變化", "籌碼狀態",
        "AI進場判斷", "停損參考", "壓力參考", "技術面原因", "技術風險", "籌碼面原因", "籌碼風險",
        "5日乖離率", "20日乖離率", "突破20日高點",
    ]
    result_df = result_df[[c for c in show_order if c in result_df.columns]]

    risk_df = result_df[(result_df["風險分"] >= 20) | (result_df["技術風險"] != "暫無明顯高風險訊號")].copy()
    risk_df = risk_df.sort_values(["風險分", "AI總分"], ascending=[False, False])

    result_df.to_csv(DATA_DIR / "latest_rank.csv", index=False, encoding="utf-8-sig")
    risk_df.to_csv(DATA_DIR / "latest_risk.csv", index=False, encoding="utf-8-sig")

    if histories:
        hist_df = pd.concat(histories, ignore_index=True)
        hist_df["date"] = pd.to_datetime(hist_df["date"]).dt.strftime("%Y-%m-%d")
        hist_df.to_csv(DATA_DIR / "latest_price_history.csv", index=False, encoding="utf-8-sig")
    else:
        pd.DataFrame().to_csv(DATA_DIR / "latest_price_history.csv", index=False, encoding="utf-8-sig")


    market_counts = result_df["市場"].value_counts().to_dict() if "市場" in result_df.columns else {}

    meta = {
        "updated_at": now_tw().isoformat(timespec="seconds"),
        "candidate_source": candidate_source,
        "requested_limit": limit,
        "candidate_count": len(candidates),
        "success_count": int(len(result_df)),
        "skipped_count": int(len(skipped)),
        "chip_detail_limit": chip_limit,
        "chip_fetched_count": int(chip_fetched),
        "skipped_stock_ids": skipped,
        "has_finmind_token": bool(os.getenv("FINMIND_TOKEN", "").strip()),
        "market_counts": market_counts,
    }
    (DATA_DIR / "latest_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print("完成輸出：")
    print(DATA_DIR / "latest_rank.csv")
    print(DATA_DIR / "latest_risk.csv")
    print(DATA_DIR / "latest_price_history.csv")
    print(DATA_DIR / "latest_meta.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=30, help="熱門股候選數量")
    parser.add_argument("--chip-limit", type=int, default=10, help="補抓籌碼細節檔數")
    args = parser.parse_args()
    scan(limit=args.limit, chip_limit=args.chip_limit)


if __name__ == "__main__":
    main()

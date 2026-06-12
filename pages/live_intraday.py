# pages/live_intraday.py
# v2.15.6 Live Intraday Permanent Learning DB + Learning Rate Display Fix
# Purpose:
# - Keep v2.4.x live AI candidate monitoring.
# - Add a safer "market pool scan" mode: TWSE + TPEx turnover pool + live quotes.
# - Persist sidebar settings through URL query params.
# - Manual watch stocks always show in a dedicated section.

from __future__ import annotations

import json
import math
import re
import time
import urllib.request
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="盤中即時看盤", page_icon="⚡", layout="wide")

DATA_DIR = Path("data")
RANK_PATH = DATA_DIR / "latest_rank.csv"
META_PATH = DATA_DIR / "latest_meta.json"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def now_taipei() -> datetime:
    return datetime.now(TAIPEI_TZ)


QUOTE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
TWSE_STOCK_DAY_URLS = [
    "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=open_data",
    "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=open_data",
]
TPEX_QUOTES_URLS = [
    "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes",
]

# A small local fallback map so manual watch stocks have reasonable names.
LOCAL_STOCK_INFO = {
    "2330": ("台積電", "半導體業", "上市"),
    "2313": ("華通", "電子工業", "上市"),
    "2327": ("國巨*", "電子工業", "上市"),
    "2344": ("華邦電", "半導體業", "上市"),
    "2382": ("廣達", "電子工業", "上市"),
    "2383": ("台光電", "電子工業", "上市"),
    "2409": ("友達", "光電業", "上市"),
    "2379": ("瑞昱", "半導體業", "上市"),
    "2881": ("富邦金", "金融保險", "上市"),
    "2884": ("玉山金", "金融保險", "上市"),
    "2892": ("第一金", "金融保險", "上市"),
    "3042": ("晶技", "電子工業", "上市"),
    "3441": ("聯一光", "光電業", "上市"),
    "4938": ("和碩", "電子工業", "上市"),
    "8021": ("尖點", "電子工業", "上市"),
    "3105": ("穩懋", "半導體業", "上櫃"),
    "3362": ("先進光", "光電業", "上櫃"),
    "5274": ("信驊", "半導體業", "上櫃"),
    "6173": ("信昌電", "電子零組件業", "上櫃"),
    "6223": ("旺矽", "半導體業", "上櫃"),
    "8042": ("金山電", "電子零組件業", "上櫃"),
}

# v2.8 core tracking list: the names the user wants to avoid missing.
# They are always appended to the live quote universe, even if not in the daily AI top list.
FOCUS_CODES = ["3441", "2382", "2313"]
FOCUS_LABELS = {"3441": "聯一光", "2382": "廣達", "2313": "華通"}


def _to_float(value, default=np.nan):
    try:
        if value is None:
            return default
        text = str(value).replace(",", "").strip()
        if text in {"", "-", "--", "nan", "None", "除權息"}:
            return default
        try:
            return float(text)
        except Exception:
            # Accept strings like "291～292", "約 291.5", "高", "中" without crashing.
            m = re.search(r"-?\d+(?:\.\d+)?", text)
            if m:
                return float(m.group(0))
            mapping = {"高": 75.0, "中": 50.0, "低": 25.0}
            return mapping.get(text, default)
    except Exception:
        return default


def _clean_number(value: Any) -> float:
    v = _to_float(value, default=0.0)
    if isinstance(v, float) and math.isnan(v):
        return 0.0
    return float(v)


# v2.9.2 compatibility aliases.
# Some earlier generated blocks referenced these names; keep them mapped to
# the safe numeric parser so row.apply() never crashes on missing aliases.
def _to_clean_number(value: Any) -> float:
    return _clean_number(value)


def _to__clean_number(value: Any) -> float:
    return _clean_number(value)


def _normalize_code(value: Any) -> str:
    return "".join(ch for ch in str(value).strip() if ch.isdigit()).zfill(4)


def _unique_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        code = _normalize_code(item)
        if re.match(r"^\d{4}$", code) and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _first_number_from_price_list(value):
    """TWSE MIS bid/ask fields may be formatted as '281.00_280.50_...'."""
    try:
        if value is None:
            return np.nan
        for part in str(value).split("_"):
            n = _to_float(part)
            if not math.isnan(n) and n > 0:
                return n
    except Exception:
        pass
    return np.nan


def _pick_value(row: Dict[str, Any], keywords: List[str]) -> Any:
    lower_items = [(str(k), str(k).lower(), v) for k, v in row.items()]
    for key, key_lower, value in lower_items:
        for kw in keywords:
            kw_lower = kw.lower()
            if kw in key or kw_lower in key_lower:
                return value
    return None


def _request_json(url: str, timeout: int = 12) -> Optional[Any]:
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
            return json.loads(text)
    except Exception:
        return None


def _parse_market_rows(data: Any, market: str) -> List[Dict[str, Any]]:
    if not isinstance(data, list) or not data:
        return []

    rows: List[Dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue

        code = (
            _pick_value(row, ["證券代號", "代號", "SecuritiesCompanyCode", "Code", "code", "stock_id"])
            or row.get("證券代號")
        )
        name = (
            _pick_value(row, ["證券名稱", "名稱", "CompanyName", "Name", "name", "stock_name"])
            or code
        )
        money = _pick_value(row, ["成交金額", "TradeValue", "trade_value", "Trading_money", "Amount"])
        volume = _pick_value(row, ["成交股數", "成交股", "TradeVolume", "trade_volume", "Trading_Volume", "Volume"])
        close = _pick_value(row, ["收盤價", "收盤", "Close", "ClosingPrice", "close"])

        if code is None:
            continue
        code = _normalize_code(code)
        if not re.match(r"^\d{4}$", code):
            continue

        money_num = _clean_number(money)
        close_num = _clean_number(close)
        volume_num = _clean_number(volume)
        if money_num <= 0 and close_num > 0 and volume_num > 0:
            money_num = close_num * volume_num

        rows.append(
            {
                "代號": code,
                "名稱": str(name).strip() if name else code,
                "產業": "未知",
                "市場": market,
                "成交金額": money_num,
                "盤後收盤參考": close_num,
            }
        )

    rows = sorted(rows, key=lambda x: x.get("成交金額", 0), reverse=True)
    return rows


# ---------- Load daily AI rank ----------

def load_rank() -> pd.DataFrame:
    if not RANK_PATH.exists():
        st.error("找不到 data/latest_rank.csv。請先讓 Daily Taiwan Stock AI Scan 成功跑完。")
        st.stop()

    df = pd.read_csv(RANK_PATH)

    rename_map = {}
    for c in df.columns:
        if c.lower() in {"stock_id", "code"}:
            rename_map[c] = "代號"
        elif c.lower() in {"stock_name", "name"}:
            rename_map[c] = "名稱"
        elif c.lower() in {"industry", "industry_category"}:
            rename_map[c] = "產業"
        elif c.lower() in {"ai_score", "final_score"}:
            rename_map[c] = "AI總分"
        elif c.lower() in {"risk_score"}:
            rename_map[c] = "風險分"
    if rename_map:
        df = df.rename(columns=rename_map)

    required = ["代號", "名稱", "AI總分", "風險分"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        st.error(f"latest_rank.csv 缺少欄位：{missing}")
        st.stop()

    df["代號"] = df["代號"].astype(str).str.replace(".0", "", regex=False).str.zfill(4)
    if "市場" not in df.columns:
        df["市場"] = "未知"
    if "產業" not in df.columns:
        df["產業"] = "未知"
    if "資料來源" not in df.columns:
        df["資料來源"] = "盤後AI候選"
    if "手動加入" not in df.columns:
        df["手動加入"] = False
    df["AI來源"] = "盤後AI"
    return df


@st.cache_data(ttl=300, show_spinner=False)
def fetch_market_pool(pool_size: int) -> Tuple[pd.DataFrame, str]:
    """Build a broader intraday universe from TWSE + TPEx turnover lists.

    This is not a full 1800-stock real-time scan. It is a practical market pool built from
    top turnover names, then the page fetches live quotes for that pool.
    """
    per_market_limit = max(100, min(500, pool_size * 2))
    all_rows: List[Dict[str, Any]] = []
    sources: List[str] = []

    twse_rows: List[Dict[str, Any]] = []
    for url in TWSE_STOCK_DAY_URLS:
        data = _request_json(url, timeout=12)
        twse_rows = _parse_market_rows(data, market="上市")
        if twse_rows:
            sources.append("證交所上市成交金額排行")
            break
    if not twse_rows:
        sources.append("證交所上市來源失敗")

    tpex_rows: List[Dict[str, Any]] = []
    for url in TPEX_QUOTES_URLS:
        data = _request_json(url, timeout=12)
        tpex_rows = _parse_market_rows(data, market="上櫃")
        if tpex_rows:
            sources.append("櫃買中心上櫃成交金額排行")
            break
    if not tpex_rows:
        sources.append("櫃買中心上櫃來源失敗")

    all_rows.extend(twse_rows[:per_market_limit])
    all_rows.extend(tpex_rows[:per_market_limit])

    if not all_rows:
        return pd.DataFrame(), "；".join(sources) if sources else "市場池來源抓取失敗"

    by_code: Dict[str, Dict[str, Any]] = {}
    for row in all_rows:
        code = _normalize_code(row.get("代號"))
        if not re.match(r"^\d{4}$", code):
            continue
        row = dict(row)
        row["代號"] = code
        if code not in by_code or _clean_number(row.get("成交金額")) > _clean_number(by_code[code].get("成交金額")):
            by_code[code] = row

    pool = pd.DataFrame(sorted(by_code.values(), key=lambda x: _clean_number(x.get("成交金額")), reverse=True))
    pool = pool.head(pool_size).reset_index(drop=True)
    pool["市場池排名"] = np.arange(1, len(pool) + 1)
    pool["資料來源"] = "盤中市場池"

    # Turnover-base score for names that are not in daily AI candidates.
    # Top turnover names get a modest base score but not a full AI score.
    if len(pool) > 1:
        pool["市場池基礎分"] = (55 - (pool["市場池排名"] - 1) / max(len(pool) - 1, 1) * 10).round(1)
    else:
        pool["市場池基礎分"] = 50.0

    return pool, "；".join(sources)


def build_live_universe(rank_df: pd.DataFrame, mode: str, pool_size: int, manual_codes: List[str], manual_ai_score: int = 50, manual_risk_score: int = 20) -> Tuple[pd.DataFrame, str]:
    rank_df = rank_df.copy()
    rank_df["代號"] = rank_df["代號"].astype(str).str.zfill(4)

    if mode == "盤中市場池掃描":
        pool_df, source = fetch_market_pool(pool_size)
        if pool_df.empty:
            universe = rank_df.copy()
            universe["資料來源"] = universe.get("資料來源", "盤後AI候選")
            return append_manual_codes(universe, manual_codes, manual_ai_score, manual_risk_score), f"市場池抓取失敗，改用盤後AI候選｜{source}"

        ai_cols = ["代號", "AI總分", "風險分", "產業", "市場", "名稱"]
        ai_map = rank_df[[c for c in ai_cols if c in rank_df.columns]].drop_duplicates("代號")
        universe = pool_df.merge(ai_map, on="代號", how="left", suffixes=("_pool", "_ai"))

        # Prefer daily AI name/industry when available, otherwise market pool name.
        universe["名稱"] = universe.get("名稱_ai").fillna(universe.get("名稱_pool")).fillna(universe["代號"])
        universe["產業"] = universe.get("產業_ai").fillna(universe.get("產業_pool")).fillna("未知")
        universe["市場"] = universe.get("市場_ai").fillna(universe.get("市場_pool")).fillna("未知")
        universe["風險分"] = pd.to_numeric(universe.get("風險分"), errors="coerce").fillna(20)
        universe["AI總分"] = pd.to_numeric(universe.get("AI總分"), errors="coerce")
        universe["AI來源"] = np.where(universe["AI總分"].notna(), "盤後AI", "市場池估分")
        universe["AI總分"] = universe["AI總分"].fillna(universe["市場池基礎分"]).round(1)
        universe["資料來源"] = np.where(universe["AI來源"].eq("盤後AI"), "市場池+盤後AI", "盤中市場池")
        universe["手動加入"] = False

        keep = [
            "代號", "名稱", "市場", "產業", "資料來源", "AI來源", "市場池排名", "成交金額", "市場池基礎分",
            "AI總分", "風險分", "手動加入"
        ]
        universe = universe[[c for c in keep if c in universe.columns]].copy()
        universe = append_manual_codes(universe, manual_codes, manual_ai_score, manual_risk_score)
        return universe, f"盤中市場池掃描｜{source}"

    universe = rank_df.copy()
    universe["資料來源"] = universe.get("資料來源", "盤後AI候選")
    universe["AI來源"] = "盤後AI"
    universe = append_manual_codes(universe, manual_codes, manual_ai_score, manual_risk_score)
    return universe, "盤後AI候選 + 手動監控"


# ---------- Live quote ----------

def build_symbols(df: pd.DataFrame) -> List[str]:
    symbols: List[str] = []
    for _, row in df.iterrows():
        code = str(row["代號"]).zfill(4)
        market = str(row.get("市場", "未知"))
        if "上櫃" in market or "OTC" in market.upper():
            candidates = [f"otc_{code}.tw", f"tse_{code}.tw"]
        elif "上市" in market or "TWSE" in market.upper():
            candidates = [f"tse_{code}.tw", f"otc_{code}.tw"]
        else:
            candidates = [f"tse_{code}.tw", f"otc_{code}.tw"]
        symbols.extend(candidates)
    return symbols


@st.cache_data(ttl=10, show_spinner=False)
def fetch_twse_mis_quotes(symbols: List[str]) -> pd.DataFrame:
    """Fetch quotes from TWSE MIS with more defensive retries.

    v2.12.1 fix:
    - Seed cookies by visiting the MIS page first.
    - Use smaller batches because large ex_ch requests sometimes return empty on Streamlit Cloud.
    - Retry once with even smaller batches before giving up.
    - Never crash the page when MIS temporarily returns malformed payloads.
    """
    dedup_symbols = list(dict.fromkeys([str(s).strip() for s in symbols if str(s).strip()]))
    if not dedup_symbols:
        return pd.DataFrame()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw",
        "Connection": "keep-alive",
    }

    def _request_rows(batch_size: int, pause: float) -> List[Dict[str, Any]]:
        session = requests.Session()
        session.headers.update(headers)
        rows: List[Dict[str, Any]] = []
        try:
            session.get("https://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw", timeout=6)
        except Exception:
            pass
        for i in range(0, len(dedup_symbols), batch_size):
            batch = dedup_symbols[i : i + batch_size]
            params = {
                "ex_ch": "|".join(batch),
                "json": "1",
                "delay": "0",
                "_": str(int(time.time() * 1000)),
            }
            try:
                r = session.get(QUOTE_URL, params=params, timeout=9)
                r.encoding = "utf-8"
                payload = r.json()
                msg_array = payload.get("msgArray", []) if isinstance(payload, dict) else []
                if isinstance(msg_array, list):
                    rows.extend([x for x in msg_array if isinstance(x, dict)])
            except Exception:
                pass
            time.sleep(pause)
        return rows

    rows = _request_rows(batch_size=18, pause=0.12)
    if not rows:
        rows = _request_rows(batch_size=8, pause=0.18)

    if not rows:
        return pd.DataFrame()

    out = []
    for q in rows:
        code = str(q.get("c", "")).zfill(4)
        if not re.match(r"^\d{4}$", code):
            continue
        name = q.get("n", "")
        ex = q.get("ex", "")
        market = "上市" if ex == "tse" else "上櫃" if ex == "otc" else ex

        last = _to_float(q.get("z"))
        if math.isnan(last):
            ask = _first_number_from_price_list(q.get("a"))
            bid = _first_number_from_price_list(q.get("b"))
            if not math.isnan(ask) and not math.isnan(bid):
                last = round((ask + bid) / 2, 2)
            elif not math.isnan(ask):
                last = ask
            elif not math.isnan(bid):
                last = bid

        prev_close = _to_float(q.get("y"))
        open_px = _to_float(q.get("o"))
        high = _to_float(q.get("h"))
        low = _to_float(q.get("l"))
        volume = _to_float(q.get("v"), 0)

        change_pct = np.nan
        if not math.isnan(last) and not math.isnan(prev_close) and prev_close > 0:
            change_pct = round((last - prev_close) / prev_close * 100, 2)

        quote_time = ""
        date_part = str(q.get("d", ""))
        time_part = str(q.get("t", ""))
        if date_part or time_part:
            quote_time = f"{date_part} {time_part}".strip()

        out.append(
            {
                "代號": code,
                "即時名稱": name,
                "報價市場": market,
                "盤中現價": last,
                "昨收": prev_close,
                "開盤": open_px,
                "最高": high,
                "最低": low,
                "盤中漲跌幅": change_pct,
                "盤中成交量": volume,
                "報價時間": quote_time,
            }
        )

    quotes = pd.DataFrame(out)
    if quotes.empty:
        return quotes

    quotes["盤中現價"] = pd.to_numeric(quotes.get("盤中現價"), errors="coerce")
    quotes["has_price"] = quotes["盤中現價"].notna().astype(int)
    quotes = quotes.sort_values(["代號", "has_price"], ascending=[True, False])
    quotes = quotes.drop_duplicates("代號", keep="first").drop(columns=["has_price"])
    return quotes


# ---------- Scoring / alerts / entry engine ----------

def compute_live_strength(df: pd.DataFrame, attack_threshold=65, watch_threshold=55, weak_drop=-1.0, chase_pct=7.0) -> pd.DataFrame:
    df = df.copy()
    df["AI總分"] = pd.to_numeric(df["AI總分"], errors="coerce").fillna(0)
    df["風險分"] = pd.to_numeric(df["風險分"], errors="coerce").fillna(0)
    df["盤中漲跌幅"] = pd.to_numeric(df["盤中漲跌幅"], errors="coerce").fillna(0)
    df["盤中成交量"] = pd.to_numeric(df["盤中成交量"], errors="coerce").fillna(0)

    if df["盤中成交量"].max() > 0:
        df["盤中量能分"] = df["盤中成交量"].rank(pct=True) * 100
    else:
        df["盤中量能分"] = 0

    df["盤中漲幅分"] = ((df["盤中漲跌幅"] + 3) / 8 * 100).clip(0, 100)

    # In market-pool mode, AI來源 may be 市場池估分, so this is a hybrid strength score.
    df["即時強度分"] = (
        df["AI總分"] * 0.45
        + df["盤中漲幅分"] * 0.30
        + df["盤中量能分"] * 0.15
        - df["風險分"] * 0.10
    ).round(1)

    def alert(row):
        strength = _clean_number(row.get("即時強度分"))
        ai = _clean_number(row.get("AI總分"))
        risk = _clean_number(row.get("風險分"))
        pct = _clean_number(row.get("盤中漲跌幅"))
        vol_score = _clean_number(row.get("盤中量能分"))

        if pct >= chase_pct and risk >= 30:
            return "不要追高", "漲幅過大且風險偏高，容易震盪或倒貨。"
        if risk >= 40 and pct > 0:
            return "高風險上漲", "有上漲但風險分高，只能觀察，不建議追。"
        if ai >= 60 and pct <= weak_drop:
            return "AI高分轉弱", "盤後AI分數高，但盤中轉弱，先等止跌。"
        if strength >= attack_threshold and pct > 1.5 and vol_score >= 60 and risk < 40:
            return "強勢進攻", "AI/市場池基礎、盤中漲幅、量能同步偏強，可列入重點觀察。"
        if strength >= watch_threshold and pct > 0 and risk < 40:
            return "觀察偏強", "盤中偏強，可觀察量能是否延續。"
        if pct <= -3:
            return "盤中轉弱", "跌幅擴大，避免急著承接。"
        return "中性", "以盤後AI分數、盤中動能與風控為主。"

    alerts = df.apply(alert, axis=1)
    df["盤中警示"] = [a[0] for a in alerts]
    df["即時判斷"] = [a[1] for a in alerts]

    label_map = {
        "強勢進攻": "🟢 強勢進攻",
        "觀察偏強": "🟡 觀察偏強",
        "中性": "🔵 中性",
        "AI高分轉弱": "🟠 AI高分轉弱",
        "不要追高": "🔴 不要追高",
        "高風險上漲": "🔴 高風險上漲",
        "盤中轉弱": "🟠 盤中轉弱",
    }
    df["盤中標籤"] = df["盤中警示"].map(label_map).fillna("🔵 中性")

    priority = {
        "強勢進攻": 1,
        "觀察偏強": 2,
        "AI高分轉弱": 3,
        "不要追高": 4,
        "高風險上漲": 5,
        "盤中轉弱": 6,
        "中性": 9,
    }
    df["警示排序"] = df["盤中警示"].map(priority).fillna(9)
    return df.sort_values(["警示排序", "即時強度分"], ascending=[True, False]).reset_index(drop=True)


def _fmt_price(value):
    try:
        if value is None:
            return "-"
        v = float(value)
        if math.isnan(v) or v <= 0:
            return "-"
        if v >= 1000:
            return f"{v:.0f}"
        if v >= 100:
            return f"{v:.1f}"
        return f"{v:.2f}"
    except Exception:
        return "-"


def _round_tick(value):
    try:
        v = float(value)
        if math.isnan(v) or v <= 0:
            return np.nan
        if v < 50:
            return round(v, 2)
        if v < 100:
            return round(v, 1)
        if v < 500:
            return round(v * 2) / 2
        if v < 1000:
            return round(v)
        return round(v / 5) * 5
    except Exception:
        return np.nan


def add_entry_timing(df: pd.DataFrame, chase_pct=7.0) -> pd.DataFrame:
    df = df.copy()

    def plan(row):
        ai = _clean_number(row.get("AI總分"))
        risk = _clean_number(row.get("風險分"))
        strength = _clean_number(row.get("即時強度分"))
        pct = _clean_number(row.get("盤中漲跌幅"))
        vol_score = _clean_number(row.get("盤中量能分"))
        alert = str(row.get("盤中警示", "中性"))
        ai_source = str(row.get("AI來源", ""))

        px = _to__clean_number(row.get("盤中現價"))
        high = _to__clean_number(row.get("最高"))
        low = _to__clean_number(row.get("最低"))
        open_px = _to__clean_number(row.get("開盤"))
        prev = _to__clean_number(row.get("昨收"))

        if math.isnan(px) or px <= 0:
            return pd.Series({
                "盤中入場判斷": "無報價，暫不判斷",
                "入場型態": "無資料",
                "觸發價": "-",
                "停損參考": "-",
                "壓力參考": "-",
                "不追原因": "盤中報價不足",
                "建議動作": "等下一次報價刷新",
            })

        ref_high = high if not math.isnan(high) and high > 0 else px
        ref_low = low if not math.isnan(low) and low > 0 else min(px, prev if not math.isnan(prev) and prev > 0 else px)
        ref_prev = prev if not math.isnan(prev) and prev > 0 else px
        ref_open = open_px if not math.isnan(open_px) and open_px > 0 else px

        trigger_break = _round_tick(max(px, ref_high) * 1.002)
        trigger_reclaim = _round_tick(max(px, ref_open, ref_prev) * 1.001)
        stop_short = _round_tick(min(ref_low, px * 0.985))
        stop_loose = _round_tick(min(ref_low, ref_prev, px * 0.97))
        pressure = _round_tick(max(ref_high, px * 1.025))

        if pct >= chase_pct and (risk >= 25 or ai < 65):
            return pd.Series({
                "盤中入場判斷": "漲幅偏高，不追",
                "入場型態": "追高風險型",
                "觸發價": "-",
                "停損參考": _fmt_price(stop_short),
                "壓力參考": _fmt_price(pressure),
                "不追原因": f"盤中漲幅 {pct:.2f}% 已偏高，容易震盪",
                "建議動作": "等拉回、回測不破或尾盤確認",
            })

        if risk >= 40:
            return pd.Series({
                "盤中入場判斷": "風險偏高，暫不追",
                "入場型態": "高風險觀察型",
                "觸發價": "-",
                "停損參考": _fmt_price(stop_loose),
                "壓力參考": _fmt_price(pressure),
                "不追原因": f"風險分 {risk:.0f} 偏高",
                "建議動作": "只觀察，不做追價；等風險下降或回測確認",
            })

        if alert == "AI高分轉弱" or (ai >= 60 and pct <= -1.0):
            return pd.Series({
                "盤中入場判斷": "AI高分但盤中轉弱",
                "入場型態": "重新站回型",
                "觸發價": _fmt_price(trigger_reclaim),
                "停損參考": _fmt_price(stop_loose),
                "壓力參考": _fmt_price(pressure),
                "不追原因": "盤中轉弱，尚未止跌確認",
                "建議動作": "等重新站回開盤價/昨收附近，再觀察量能",
            })

        if pct <= -2.0 or strength < 35:
            return pd.Series({
                "盤中入場判斷": "盤中轉弱，避開",
                "入場型態": "轉弱避開型",
                "觸發價": "-",
                "停損參考": _fmt_price(stop_loose),
                "壓力參考": _fmt_price(pressure),
                "不追原因": "盤中強度不足或跌幅擴大",
                "建議動作": "不急著接，等下一輪重新轉強",
            })

        if alert == "強勢進攻" or (ai >= 60 and strength >= 65 and 1.0 <= pct <= chase_pct and vol_score >= 60):
            suffix = "；市場池估分股需額外確認基本面與題材" if ai_source == "市場池估分" else ""
            return pd.Series({
                "盤中入場判斷": "強勢進攻，可盯突破",
                "入場型態": "突破確認型",
                "觸發價": _fmt_price(trigger_break),
                "停損參考": _fmt_price(stop_short),
                "壓力參考": _fmt_price(pressure),
                "不追原因": "若瞬間急拉超過觸發價太多，不用追" + suffix,
                "建議動作": "盯是否帶量突破；突破後未站穩就放棄",
            })

        if alert == "觀察偏強" or (strength >= 55 and pct > 0 and ai >= 45 and risk < 40):
            suffix = "；市場池股僅代表盤中動能，不等於完整盤後AI通過" if ai_source == "市場池估分" else ""
            return pd.Series({
                "盤中入場判斷": "觀察偏強，等回測確認",
                "入場型態": "回測確認型",
                "觸發價": _fmt_price(trigger_reclaim),
                "停損參考": _fmt_price(stop_short),
                "壓力參考": _fmt_price(pressure),
                "不追原因": "尚未達強攻條件，追價勝率不夠好" + suffix,
                "建議動作": "等回測不破或重新放量，再列入優先盯盤",
            })

        return pd.Series({
            "盤中入場判斷": "僅觀察，未達入場條件",
            "入場型態": "中性觀察型",
            "觸發價": _fmt_price(trigger_reclaim),
            "停損參考": _fmt_price(stop_loose),
            "壓力參考": _fmt_price(pressure),
            "不追原因": "AI/市場池分、盤中強度或量能尚未同步",
            "建議動作": "等待下一次刷新或更明確突破/回測訊號",
        })

    plans = df.apply(plan, axis=1)
    for col in plans.columns:
        df[col] = plans[col]

    return df


def add_decision_dashboard(df: pd.DataFrame) -> pd.DataFrame:
    """v2.7: Convert many intraday signals into a simpler decision layer.

    The goal is not to issue an order, but to answer:
    - Which stock should be watched first?
    - Is it a breakout watch, pullback watch, no-chase, or avoid?
    - How far is it from limit-up?
    """
    df = df.copy()

    def decision(row):
        ai = _clean_number(row.get("AI總分"))
        risk = _clean_number(row.get("風險分"))
        strength = _clean_number(row.get("即時強度分"))
        pct = _clean_number(row.get("盤中漲跌幅"))
        vol_score = _clean_number(row.get("盤中量能分"))
        speed = _clean_number(row.get("刷新漲速%"))
        vol_jump = _clean_number(row.get("量能跳升分"))
        px = _to__clean_number(row.get("盤中現價"))
        prev = _to__clean_number(row.get("昨收"))
        high = _to__clean_number(row.get("最高"))
        low = _to__clean_number(row.get("最低"))
        alert = str(row.get("盤中警示", ""))
        entry = str(row.get("盤中入場判斷", ""))
        surge = str(row.get("爆衝警示", ""))
        ai_source = str(row.get("AI來源", ""))

        if math.isnan(px) or px <= 0:
            return pd.Series({
                "決策等級": "⚪ 無報價",
                "決策分": 0.0,
                "漲停參考": "-",
                "漲停距離%": np.nan,
                "漲停雷達": "無報價",
                "是否可入場": "不可判斷",
                "入場建議": "等待下一次即時報價刷新。",
                "第一優先原因": "盤中報價不足",
            })

        limit_up = prev * 1.1 if not math.isnan(prev) and prev > 0 else px * 1.1
        limit_dist = max(0.0, (limit_up - px) / px * 100) if px > 0 else np.nan
        near_limit = (pct >= 8.5) or (not math.isnan(limit_dist) and limit_dist <= 1.8)
        day_high_break = False
        if not math.isnan(high) and high > 0:
            day_high_break = px >= high * 0.998

        # 爆衝分：獨立於盤後AI，專抓短線加速度與接近漲停。
        surge_score = 0.0
        surge_score += max(0.0, min(35.0, pct * 3.0))
        surge_score += max(0.0, min(30.0, speed * 12.0))
        surge_score += min(20.0, vol_jump * 0.20)
        surge_score += 8.0 if day_high_break else 0.0
        surge_score += 10.0 if near_limit else 0.0
        surge_score = round(min(100.0, surge_score), 1)

        # 綜合決策分：盤後AI是品質底，盤中動能是觸發，風險扣分。
        decision_score = (
            ai * 0.30
            + strength * 0.30
            + surge_score * 0.25
            + vol_score * 0.10
            - risk * 0.15
        )
        decision_score = round(max(0.0, min(100.0, decision_score)), 1)

        limit_radar = "🔥 接近漲停" if near_limit else "🚀 爆衝觀察" if surge_score >= 55 else "🟡 動能觀察" if surge_score >= 35 else "一般"

        # Clear, one-line decision; favor safety if already too extended.
        if pct >= 8.0 or (near_limit and speed < 0.6):
            grade = "🔴 D 不追高"
            can_enter = "不建議追價"
            advice = "已進入高漲幅/近漲停區，除非已持有；新進先等拉回、回測不破或尾盤確認。"
            reason = "漲幅已高，追價風險大"
        elif risk >= 40 and pct > 0:
            grade = "🔴 D 高風險"
            can_enter = "不建議追價"
            advice = "風險分偏高，即使上漲也先觀察，不用追。"
            reason = "風險分偏高"
        elif pct <= -1.5 or strength < 35 or alert == "盤中轉弱":
            grade = "⚫ E 轉弱避開"
            can_enter = "不建議"
            advice = "盤中轉弱或強度不足，先避開，等重新站回再看。"
            reason = "盤中轉弱"
        elif alert == "AI高分轉弱" or (ai >= 60 and pct <= -0.5):
            grade = "🟠 C 高分轉弱"
            can_enter = "等重新站回"
            advice = "盤後AI分數不差，但盤中轉弱；等重新站回觸發價且量能回來。"
            reason = "AI高分但盤中弱"
        elif (surge_score >= 60 and 0.8 <= pct < 7.5 and risk < 35) or entry == "強勢進攻，可盯突破" or surge in {"🟢 瞬間爆衝", "🟢 剛起漲"}:
            # 市場池估分股也可以列出，但建議保守。
            grade = "🟢 A 可盯突破"
            can_enter = "等觸發，不市價追"
            extra = "市場池估分股需更小心，先看是否站穩。" if ai_source == "市場池估分" else ""
            advice = f"若帶量站上觸發價並維持 1～2 輪刷新，可列入優先觀察；沒站穩就放棄。{extra}"
            reason = "盤中動能與爆衝分同步"
        elif (strength >= 55 and pct > 0 and risk < 40 and decision_score >= 45) or entry == "觀察偏強，等回測確認":
            grade = "🟡 B 等回測"
            can_enter = "等回測確認"
            advice = "不要追高，等回測昨收/開盤/觸發價附近不破，或重新放量再觀察。"
            reason = "偏強但未達強攻"
        else:
            grade = "🔵 C 僅觀察"
            can_enter = "先不進"
            advice = "條件尚未同步，等待下一次刷新或更明確的突破/回測訊號。"
            reason = "訊號不足"

        return pd.Series({
            "決策等級": grade,
            "決策分": decision_score,
            "爆衝分": surge_score,
            "漲停參考": _fmt_price(limit_up),
            "漲停距離%": round(limit_dist, 2) if not math.isnan(limit_dist) else np.nan,
            "漲停雷達": limit_radar,
            "是否可入場": can_enter,
            "入場建議": advice,
            "第一優先原因": reason,
        })

    decisions = df.apply(decision, axis=1)
    for col in decisions.columns:
        df[col] = decisions[col]

    priority = {
        "🟢 A 可盯突破": 1,
        "🟡 B 等回測": 2,
        "🟠 C 高分轉弱": 3,
        "🔵 C 僅觀察": 4,
        "🔴 D 不追高": 5,
        "🔴 D 高風險": 6,
        "⚫ E 轉弱避開": 7,
        "⚪ 無報價": 9,
    }
    df["決策排序"] = df["決策等級"].map(priority).fillna(9)
    return df


def _parse_price_text(value) -> float:
    """Parse display price text such as '72.5', '72.5～73.0', or '-' into float.

    Some upstream columns may contain labels like '高', '中', '等二次攻擊觸發', or
    ranges. This parser must never crash the Streamlit app.
    """
    try:
        if value is None:
            return np.nan
        text = str(value).replace(",", "").replace("％", "%").strip()
        if text in {"", "-", "--", "nan", "None", "NaN"}:
            return np.nan
        # If the cell is a price range, use the first price as conservative reference.
        text = text.replace("～", "~").replace("至", "~")
        if "~" in text:
            text = text.split("~", 1)[0].strip()
        m = re.search(r"-?\d+(?:\.\d+)?", text)
        return float(m.group(0)) if m else np.nan
    except Exception:
        return np.nan


def add_entry_signal_layer(df: pd.DataFrame, chase_pct: float = 7.0) -> pd.DataFrame:
    """v2.7.1: turn watch/decision fields into a clear entry signal.

    This layer is intentionally stricter than the decision dashboard.
    A/B are watch states; only ✅ means the setup has triggered and survived at least one refresh.
    """
    df = df.copy()

    def signal(row):
        px = _to__clean_number(row.get("盤中現價"))
        prev_px = _to__clean_number(row.get("上一輪價格"))
        trigger = _parse_price_text(row.get("觸發價"))
        pct = _clean_number(row.get("盤中漲跌幅"))
        speed = _clean_number(row.get("刷新漲速%"))
        ai = _clean_number(row.get("AI總分"))
        risk = _clean_number(row.get("風險分"))
        strength = _clean_number(row.get("即時強度分"))
        vol_score = _clean_number(row.get("盤中量能分"))
        vol_jump = _clean_number(row.get("量能跳升分"))
        decision = str(row.get("決策等級", ""))
        entry = str(row.get("盤中入場判斷", ""))
        surge = str(row.get("爆衝警示", ""))
        ai_source = str(row.get("AI來源", ""))
        limit_dist = _to__clean_number(row.get("漲停距離%"))

        if math.isnan(px) or px <= 0:
            return pd.Series({
                "入場訊號": "⚪ 無報價",
                "可否入場": "不可判斷",
                "入場確認": "無即時報價",
                "入場優先級": 9,
                "入場訊號分": 0.0,
                "入場條件檢查": "等下一次報價刷新",
                "建議下單方式": "不動作",
            })

        has_trigger = not math.isnan(trigger) and trigger > 0
        above_trigger = bool(has_trigger and px >= trigger)
        prev_above_trigger = bool(has_trigger and (not math.isnan(prev_px)) and prev_px >= trigger * 0.998)
        stood_one_round = bool(above_trigger and prev_above_trigger)
        not_extended = bool(pct < min(chase_pct, 7.5) and (math.isnan(limit_dist) or limit_dist > 1.2))
        enough_volume = bool(vol_score >= 55 or vol_jump >= 55 or surge in {"🟢 瞬間爆衝", "🟢 剛起漲", "🟡 爆量轉強"})
        safe_risk = bool(risk < 35)
        not_falling_now = bool(speed >= -0.15)
        market_pool_note = "市場池估分股，只能小量觀察，不能重押。" if ai_source == "市場池估分" else ""

        score = 0.0
        score += min(25.0, max(0.0, strength * 0.25))
        score += min(20.0, max(0.0, ai * 0.20))
        score += min(20.0, max(0.0, vol_score * 0.20))
        score += min(15.0, max(0.0, vol_jump * 0.15))
        score += 10.0 if above_trigger else 0.0
        score += 10.0 if stood_one_round else 0.0
        score -= min(25.0, max(0.0, risk * 0.35))
        score -= 18.0 if pct >= chase_pct else 0.0
        score = round(max(0.0, min(100.0, score)), 1)

        if decision in {"🔴 D 不追高", "🔴 D 高風險"} or pct >= chase_pct or (not math.isnan(limit_dist) and limit_dist <= 1.0):
            why = "已漲太高 / 太接近漲停 / 風險偏高"
            return pd.Series({
                "入場訊號": "🔴 不可追",
                "可否入場": "不建議入場",
                "入場確認": why,
                "入場優先級": 5,
                "入場訊號分": score,
                "入場條件檢查": f"{why}；等拉回或尾盤確認",
                "建議下單方式": "不追價",
            })

        if decision in {"⚫ E 轉弱避開", "🟠 C 高分轉弱"} or entry in {"盤中轉弱，避開", "AI高分但盤中轉弱"}:
            return pd.Series({
                "入場訊號": "⚫ 避開",
                "可否入場": "不可入場",
                "入場確認": "盤中轉弱或尚未重新站回",
                "入場優先級": 6,
                "入場訊號分": score,
                "入場條件檢查": "等重新站回觸發價/開盤價且量能回來",
                "建議下單方式": "不動作",
            })

        # The only explicit "can enter" signal.
        if decision == "🟢 A 可盯突破" and stood_one_round and enough_volume and safe_risk and not_extended and not_falling_now and strength >= 62:
            return pd.Series({
                "入場訊號": "✅ 可小量試單",
                "可否入場": "可以小量觀察進場",
                "入場確認": "已觸發並站穩一輪",
                "入場優先級": 1,
                "入場訊號分": score,
                "入場條件檢查": f"現價≥觸發價、上一輪也站上、量能有跟、風險可控。{market_pool_note}",
                "建議下單方式": "只小量試單；跌破停損參考就退出",
            })

        if decision == "🟢 A 可盯突破" and above_trigger and enough_volume and safe_risk and not_extended:
            return pd.Series({
                "入場訊號": "🟢 觸發中，等站穩",
                "可否入場": "尚未確認",
                "入場確認": "剛碰觸觸發價，需再站穩一輪",
                "入場優先級": 2,
                "入場訊號分": score,
                "入場條件檢查": f"已到觸發價，但還需要下一輪刷新確認沒有跌回。{market_pool_note}",
                "建議下單方式": "不要市價追；等下一輪仍站上再看",
            })

        if decision == "🟢 A 可盯突破":
            return pd.Series({
                "入場訊號": "🟢 等突破觸發",
                "可否入場": "先不進",
                "入場確認": "條件偏強，但尚未突破觸發價",
                "入場優先級": 3,
                "入場訊號分": score,
                "入場條件檢查": "等現價突破觸發價，且量能放大、下一輪不跌回",
                "建議下單方式": "掛提醒，不提前追",
            })

        if decision == "🟡 B 等回測":
            return pd.Series({
                "入場訊號": "🟡 等回測確認",
                "可否入場": "先不進",
                "入場確認": "偏強但未達突破進場條件",
                "入場優先級": 4,
                "入場訊號分": score,
                "入場條件檢查": "等拉回不破支撐，或重新放量站回觸發價",
                "建議下單方式": "等回測，不追高",
            })

        return pd.Series({
            "入場訊號": "⚪ 觀察",
            "可否入場": "先不進",
            "入場確認": "沒有明確入場觸發",
            "入場優先級": 7,
            "入場訊號分": score,
            "入場條件檢查": "AI、盤中強度、量能、觸發價尚未同步",
            "建議下單方式": "不動作",
        })

    signals = df.apply(signal, axis=1)
    for col in signals.columns:
        df[col] = signals[col]
    return df


# ---------- v2.8 Limit-up precursor / pullback re-attack engine ----------

def _tick_size(value: float) -> float:
    try:
        v = float(value)
    except Exception:
        return 0.01
    if v < 10:
        return 0.01
    if v < 50:
        return 0.05
    if v < 100:
        return 0.1
    if v < 500:
        return 0.5
    if v < 1000:
        return 1.0
    return 5.0


def _round_up_tick(value: float) -> float:
    try:
        v = float(value)
        if math.isnan(v) or v <= 0:
            return np.nan
        t = _tick_size(v)
        return round(math.ceil(v / t - 1e-9) * t, 2)
    except Exception:
        return np.nan


def _round_down_tick(value: float) -> float:
    try:
        v = float(value)
        if math.isnan(v) or v <= 0:
            return np.nan
        t = _tick_size(v)
        return round(math.floor(v / t + 1e-9) * t, 2)
    except Exception:
        return np.nan


def add_limitup_reattack_engine(df: pd.DataFrame, chase_pct: float = 7.0) -> pd.DataFrame:
    """v2.8: estimate limit-up precursor and pullback re-attack setups.

    This is intentionally separate from the daily AI score. A stock can have a weak daily AI
    estimate but still deserves radar attention if it is accelerating toward limit-up.
    """
    df = df.copy()

    def calc(row):
        code = _normalize_code(row.get("代號"))
        px = _to__clean_number(row.get("盤中現價"))
        prev = _to__clean_number(row.get("昨收"))
        open_px = _to__clean_number(row.get("開盤"))
        high = _to__clean_number(row.get("最高"))
        low = _to__clean_number(row.get("最低"))
        pct = _clean_number(row.get("盤中漲跌幅"))
        speed = _clean_number(row.get("刷新漲速%"))
        vol_score = _clean_number(row.get("盤中量能分"))
        vol_jump = _clean_number(row.get("量能跳升分"))
        strength = _clean_number(row.get("即時強度分"))
        ai = _clean_number(row.get("AI總分"))
        risk = _clean_number(row.get("風險分"))
        surge_score = _clean_number(row.get("爆衝分"))
        is_focus = code in FOCUS_CODES

        if math.isnan(px) or px <= 0:
            return pd.Series({
                "漲停前兆分": 0.0,
                "漲停前兆狀態": "⚪ 無報價",
                "再攻機率": "不可判斷",
                "回檔幅度%": np.nan,
                "日內高點漲幅%": np.nan,
                "二次攻擊觸發價": "-",
                "回測支撐價": "-",
                "防守停損價": "-",
                "建議進場區間": "無報價",
                "回檔再攻狀態": "⚪ 無報價",
                "回檔再攻判斷": "盤中報價不足，暫不判斷。",
                "v28核心追蹤": "是" if is_focus else "否",
            })

        ref_prev = prev if not math.isnan(prev) and prev > 0 else px
        ref_open = open_px if not math.isnan(open_px) and open_px > 0 else px
        ref_high = high if not math.isnan(high) and high > 0 else px
        ref_low = low if not math.isnan(low) and low > 0 else min(px, ref_prev)

        limit_up = _round_up_tick(ref_prev * 1.10)
        limit_dist = max(0.0, (limit_up - px) / px * 100) if px > 0 and not math.isnan(limit_up) else np.nan
        high_pct = (ref_high - ref_prev) / ref_prev * 100 if ref_prev > 0 else 0.0
        pullback = max(0.0, (ref_high - px) / ref_high * 100) if ref_high > 0 else 0.0
        rebound_from_low = max(0.0, (px - ref_low) / ref_low * 100) if ref_low > 0 else 0.0

        # Triggers: do not chase every green candle. Re-attack trigger is near the intraday high;
        # support is the stronger of open/previous close/near-high pullback zone.
        if pullback >= 0.6:
            reattack_trigger = _round_up_tick(ref_high * 0.995)
        else:
            reattack_trigger = _round_up_tick(max(ref_high, px) * 1.001)
        pullback_support = _round_down_tick(max(ref_prev, ref_open, ref_high * 0.965, ref_low))
        defensive_stop = _round_down_tick(min(px * 0.985, pullback_support * 0.995 if not math.isnan(pullback_support) else px * 0.985))
        early_trigger = _round_up_tick(max(px * 1.003, ref_open * 1.002, ref_prev * 1.002))

        ideal_pullback = 0.8 <= pullback <= 4.2
        too_extended = bool(pct >= max(8.5, chase_pct) or (not math.isnan(limit_dist) and limit_dist <= 1.2))
        strong_day = bool(high_pct >= 5.0 or pct >= 4.0)
        near_day_high = bool(px >= ref_high * 0.992)
        has_volume = bool(vol_score >= 55 or vol_jump >= 55)
        reattack_context = bool(strong_day and ideal_pullback and px > ref_prev and risk < 45)
        trigger_hit = bool(not math.isnan(reattack_trigger) and px >= reattack_trigger * 0.998)

        # Limit-up precursor score: acceleration first, daily AI second.
        precursor = 0.0
        precursor += min(28.0, max(0.0, pct * 3.0))
        precursor += min(22.0, max(0.0, high_pct * 2.2))
        precursor += min(18.0, max(0.0, speed * 10.0))
        precursor += min(15.0, max(0.0, vol_jump * 0.15))
        precursor += min(10.0, max(0.0, vol_score * 0.10))
        precursor += 10.0 if near_day_high else 0.0
        precursor += 12.0 if reattack_context else 0.0
        precursor += 8.0 if not math.isnan(limit_dist) and limit_dist <= 3.0 else 0.0
        precursor += 5.0 if is_focus else 0.0
        precursor += min(6.0, max(0.0, ai * 0.06))
        precursor -= min(18.0, max(0.0, risk * 0.25))
        precursor = round(max(0.0, min(100.0, precursor)), 1)

        if precursor >= 78:
            precursor_state = "🔥 漲停前兆強"
            chance = "高"
        elif precursor >= 62:
            precursor_state = "🚀 漲停前兆升溫"
            chance = "中高"
        elif precursor >= 45:
            precursor_state = "👀 早期雷達"
            chance = "中"
        else:
            precursor_state = "一般"
            chance = "低"

        if too_extended:
            reattack_state = "🔴 接近漲停不追"
            entry_zone = f"不追；等回測 { _fmt_price(pullback_support) } 附近不破"
            judgment = "漲幅已高或距離漲停太近，新進追價風險大；已有部位才看是否鎖住。"
        elif reattack_context and trigger_hit and speed >= -0.05 and has_volume and strength >= 55 and risk < 38:
            reattack_state = "✅ 二次攻擊可小量試單"
            entry_zone = f"{_fmt_price(px)}～{_fmt_price(reattack_trigger)}；跌破 {_fmt_price(defensive_stop)} 退出"
            judgment = "急拉後回檔沒有破壞結構，現價重新貼近/站回二次攻擊觸發價，量能與強度仍可接受。"
        elif reattack_context and px < reattack_trigger and speed >= -0.3:
            reattack_state = "🟢 等二次攻擊觸發"
            entry_zone = f"突破並站穩 {_fmt_price(reattack_trigger)}；或回測 {_fmt_price(pullback_support)} 不破再看"
            judgment = "急拉後正在回檔整理，仍有二次攻擊條件；不要提前追，等重新站回觸發價。"
        elif strong_day and pullback > 4.2:
            reattack_state = "🟡 回檔較深，等止跌"
            entry_zone = f"先看 {_fmt_price(pullback_support)} 是否守住，再等站回 {_fmt_price(early_trigger)}"
            judgment = "日內曾經強，但回檔較深，二次攻擊前必須先止跌與重新放量。"
        elif precursor >= 62 and speed >= 0.3 and has_volume and not too_extended:
            reattack_state = "🚀 爆衝早期可盯"
            entry_zone = f"站上 {_fmt_price(early_trigger)} 且下一輪不跌回，再考慮小量"
            judgment = "漲停前兆升溫，但還不是穩定入場點；先等觸發價確認。"
        elif precursor >= 45:
            reattack_state = "👀 早期雷達"
            entry_zone = f"等突破 {_fmt_price(early_trigger)} 或回測 {_fmt_price(pullback_support)} 不破"
            judgment = "開始有轉強跡象，但入場條件還不完整。"
        else:
            reattack_state = "⚪ 無再攻訊號"
            entry_zone = "不動作"
            judgment = "目前沒有明確漲停前兆或回檔再攻條件。"

        return pd.Series({
            "漲停前兆分": precursor,
            "漲停前兆狀態": precursor_state,
            "再攻機率": chance,
            "回檔幅度%": round(pullback, 2),
            "日內高點漲幅%": round(high_pct, 2),
            "二次攻擊觸發價": _fmt_price(reattack_trigger),
            "回測支撐價": _fmt_price(pullback_support),
            "防守停損價": _fmt_price(defensive_stop),
            "建議進場區間": entry_zone,
            "回檔再攻狀態": reattack_state,
            "回檔再攻判斷": judgment,
            "v28核心追蹤": "是" if is_focus else "否",
        })

    out = df.apply(calc, axis=1)
    for col in out.columns:
        df[col] = out[col]
    return df



# ---------- v2.8.1 Three-zone entry price engine ----------

def _price_zone_text(lo: float, hi: float) -> str:
    if math.isnan(lo) or lo <= 0:
        return "-"
    if math.isnan(hi) or hi <= 0:
        return _fmt_price(lo)
    if abs(hi - lo) < 1e-9:
        return _fmt_price(lo)
    return f"{_fmt_price(lo)}～{_fmt_price(hi)}"


def add_v281_three_zone_entry(df: pd.DataFrame) -> pd.DataFrame:
    """v2.8.1: avoid the 'wait for confirmation then buy too high' problem.

    Instead of showing only one trigger price, each stock gets:
    - 左側低吸區: near support, only if it is holding and stop is tight.
    - 右側確認價: safer confirmation, but not a blind chase.
    - 追價上限: above this price, the system should say 'too high, wait pullback'.
    """
    df = df.copy()

    def calc(row):
        px = _to__clean_number(row.get("盤中現價"))
        pct = _clean_number(row.get("盤中漲跌幅"))
        speed = _clean_number(row.get("刷新漲速%"))
        vol_score = _clean_number(row.get("盤中量能分"))
        vol_jump = _clean_number(row.get("量能跳升分"))
        risk = _clean_number(row.get("風險分"))
        precursor = _clean_number(row.get("漲停前兆分"))
        state = _safe_text(row.get("回檔再攻狀態"), "")

        support = _parse_price_text(row.get("回測支撐價"))
        trigger2 = _parse_price_text(row.get("二次攻擊觸發價"))
        trigger1 = _parse_price_text(row.get("觸發價"))
        stop = _parse_price_text(row.get("防守停損價"))
        if math.isnan(trigger2):
            trigger2 = trigger1

        if math.isnan(px) or px <= 0:
            return pd.Series({
                "左側低吸區": "-",
                "右側確認價": "-",
                "追價上限": "-",
                "入場價位策略": "無報價，不動作",
                "買高警示": "無報價",
                "三段式進場建議": "無報價",
            })

        # If support/trigger is not available, build conservative references from current price.
        if math.isnan(support) or support <= 0:
            support = _round_down_tick(px * 0.992)
        if math.isnan(trigger2) or trigger2 <= 0:
            trigger2 = _round_up_tick(px * 1.003)
        if math.isnan(stop) or stop <= 0:
            stop = _round_down_tick(support * 0.992)

        t = _tick_size(px)
        # Left-side zone should be below the confirmation trigger. For 291 support / 292 trigger,
        # this usually becomes 291.0~291.5, which is the practical entry zone the user expects.
        left_lo = _round_down_tick(support)
        left_hi_raw = min(trigger2 - t, support * 1.004)
        if left_hi_raw < left_lo:
            left_hi_raw = left_lo
        left_hi = _round_down_tick(left_hi_raw)

        confirm = _round_up_tick(trigger2)
        chase_cap_raw = max(confirm + t, confirm * 1.003)
        chase_cap = _round_up_tick(chase_cap_raw)

        in_left_zone = bool(px >= left_lo * 0.998 and px <= max(left_hi, left_lo) * 1.002)
        below_support = bool(px < left_lo * 0.998)
        above_confirm = bool(px >= confirm)
        above_cap = bool(px > chase_cap)
        has_volume = bool(vol_score >= 50 or vol_jump >= 45)
        holding = bool(speed >= -0.25 and not below_support)
        safe_risk = bool(risk < 42)
        too_hot = bool(pct >= 8.5 or above_cap)

        left_zone = _price_zone_text(left_lo, left_hi)
        confirm_txt = _fmt_price(confirm)
        cap_txt = _fmt_price(chase_cap)

        if too_hot:
            strategy = "🔴 已高於合理追價區，不追；等回測低吸區或尾盤確認"
            warning = f"高於追價上限 {cap_txt}，容易買高。"
            suggestion = f"低吸：{left_zone}；確認：站穩 {confirm_txt}；高於 {cap_txt} 不追。"
        elif in_left_zone and holding and has_volume and safe_risk and precursor >= 45:
            strategy = "✅ 左側低吸可小量試單"
            warning = "不是追價，是靠近支撐的小量試單；跌破防守價要退出。"
            suggestion = f"低吸：{left_zone} 小量；站穩 {confirm_txt} 才加強；跌破 {_fmt_price(stop)} 退出；高於 {cap_txt} 不追。"
        elif above_confirm and holding and has_volume and safe_risk:
            strategy = "✅ 右側確認可小量試單"
            warning = f"已過確認價，不能追過 {cap_txt}。"
            suggestion = f"確認：{confirm_txt} 附近小量；追價上限 {cap_txt}；跌破 {_fmt_price(stop)} 退出。"
        elif below_support:
            strategy = "🟡 跌破支撐，先等止跌"
            warning = "支撐沒守住，不能因為便宜就接。"
            suggestion = f"先等重新站回 {left_lo}，再看 {confirm_txt} 是否能站穩。"
        else:
            strategy = "🟢 等低吸或等確認，不要卡在中間追"
            warning = "目前不是最佳買點；中間價容易上不上、下不下。"
            suggestion = f"低吸：{left_zone}；確認：站穩 {confirm_txt}；高於 {cap_txt} 不追。"

        # Make the old single text field less misleading.
        if state in {"🟢 等二次攻擊觸發", "🟡 回檔較深，等止跌", "👀 早期雷達", "🚀 爆衝早期可盯"}:
            old_style = suggestion
        elif state == "✅ 二次攻擊可小量試單":
            old_style = suggestion
        elif state == "🔴 接近漲停不追":
            old_style = f"不追；等回測 {left_zone}，或站穩 {confirm_txt} 但不得高於 {cap_txt}。"
        else:
            old_style = suggestion if precursor >= 40 else _safe_text(row.get("建議進場區間"), suggestion)

        return pd.Series({
            "左側低吸區": left_zone,
            "右側確認價": confirm_txt,
            "追價上限": cap_txt,
            "入場價位策略": strategy,
            "買高警示": warning,
            "三段式進場建議": suggestion,
            "建議進場區間": old_style,
        })

    out = df.apply(calc, axis=1)
    for col in out.columns:
        df[col] = out[col]
    return df

def apply_v28_entry_signal_overrides(df: pd.DataFrame) -> pd.DataFrame:
    """Make the v2.8 entry signal more direct for limit-up precursor / re-attack cases."""
    df = df.copy()
    for idx, row in df.iterrows():
        state = _safe_text(row.get("回檔再攻狀態"), "")
        precursor = _clean_number(row.get("漲停前兆分"))
        risk = _clean_number(row.get("風險分"))
        pct = _clean_number(row.get("盤中漲跌幅"))
        limit_dist = _to__clean_number(row.get("漲停距離%"))
        near_limit = bool(pct >= 8.8 or (not math.isnan(limit_dist) and limit_dist <= 1.2))

        if state == "✅ 二次攻擊可小量試單" and risk < 38 and not near_limit:
            df.at[idx, "入場訊號"] = "✅ 可小量試單"
            df.at[idx, "可否入場"] = "可以小量觀察進場"
            df.at[idx, "入場確認"] = "回檔後二次攻擊觸發"
            df.at[idx, "入場優先級"] = 1
            df.at[idx, "入場訊號分"] = max(_clean_number(row.get("入場訊號分")), precursor)
            df.at[idx, "入場條件檢查"] = _safe_text(row.get("回檔再攻判斷"), "")
            df.at[idx, "建議下單方式"] = "只小量；不要市價追過觸發價太多；跌破防守停損價退出"
        elif state == "🟢 等二次攻擊觸發":
            df.at[idx, "入場訊號"] = "🟢 等二次攻擊觸發"
            df.at[idx, "可否入場"] = "先不進"
            df.at[idx, "入場確認"] = "等站回二次攻擊觸發價"
            df.at[idx, "入場優先級"] = min(int(row.get("入場優先級", 9) or 9), 2)
            df.at[idx, "入場條件檢查"] = _safe_text(row.get("回檔再攻判斷"), "")
            df.at[idx, "建議下單方式"] = "掛提醒；突破觸發價並下一輪不跌回才考慮"
        elif state in {"🚀 爆衝早期可盯", "👀 早期雷達"} and precursor >= 45:
            if _safe_text(row.get("入場訊號"), "") in {"⚪ 觀察", "🟡 等回測確認"}:
                df.at[idx, "入場訊號"] = "👀 早期雷達"
                df.at[idx, "可否入場"] = "先不進"
                df.at[idx, "入場確認"] = "有前兆但還沒觸發"
                df.at[idx, "入場優先級"] = min(int(row.get("入場優先級", 9) or 9), 3)
                df.at[idx, "入場條件檢查"] = _safe_text(row.get("回檔再攻判斷"), "")
                df.at[idx, "建議下單方式"] = "只盯盤不進；等觸發價"
        elif state == "🔴 接近漲停不追":
            df.at[idx, "入場訊號"] = "🔴 不可追"
            df.at[idx, "可否入場"] = "不建議入場"
            df.at[idx, "入場確認"] = "距離漲停太近或漲幅過高"
            df.at[idx, "入場優先級"] = 5
            df.at[idx, "建議下單方式"] = "不追價；等回檔或尾盤確認"
    return df



# ---------- Sidebar persistence and manual watch ----------

def _get_query_value(name: str, default: str = "") -> str:
    try:
        value = st.query_params.get(name, default)
        if isinstance(value, list):
            return str(value[0]) if value else default
        return str(value)
    except Exception:
        return default


def _get_query_int(name: str, default: int, min_value: int, max_value: int, step: int = 1) -> int:
    try:
        value = int(float(_get_query_value(name, str(default))))
    except Exception:
        value = default
    value = max(min_value, min(max_value, value))
    if step > 1:
        value = int(round(value / step) * step)
        value = max(min_value, min(max_value, value))
    return value


def _get_query_float(name: str, default: float, min_value: float, max_value: float, step: float = 0.5) -> float:
    try:
        value = float(_get_query_value(name, str(default)))
    except Exception:
        value = default
    value = max(min_value, min(max_value, value))
    if step:
        value = round(value / step) * step
        value = max(min_value, min(max_value, value))
    return float(value)


def _set_query_if_changed(values: Dict[str, str]) -> None:
    try:
        changed = any(_get_query_value(k, "") != str(v) for k, v in values.items())
        if changed:
            for key, value in values.items():
                st.query_params[key] = str(value)
    except Exception:
        pass


def parse_extra_codes(text: str) -> List[str]:
    if not text:
        return []
    cleaned = (
        str(text)
        .replace("，", ",")
        .replace("、", ",")
        .replace("\n", ",")
        .replace("\t", ",")
        .replace(" ", ",")
    )
    codes = []
    for part in cleaned.split(","):
        code = "".join(ch for ch in part.strip() if ch.isdigit())
        if len(code) == 4 and code not in codes:
            codes.append(code)
    return codes


def append_manual_codes(df: pd.DataFrame, codes: List[str], manual_ai_score: int = 50, manual_risk_score: int = 20) -> pd.DataFrame:
    df = df.copy()
    existing = set(df["代號"].astype(str).str.zfill(4)) if "代號" in df.columns else set()
    rows = []

    for code in codes:
        if code in existing:
            df.loc[df["代號"].astype(str).str.zfill(4) == code, "手動加入"] = True
            continue

        info = LOCAL_STOCK_INFO.get(code, (code, "未知", "未知"))
        name, industry, market = info if len(info) == 3 else (info[0], info[1], "未知")
        rows.append(
            {
                "代號": code,
                "名稱": name,
                "市場": market,
                "產業": industry,
                "AI總分": manual_ai_score,
                "風險分": manual_risk_score,
                "資料來源": "手動監控",
                "AI來源": "手動中性分",
                "手動加入": True,
            }
        )

    if rows:
        df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)

    df["代號"] = df["代號"].astype(str).str.zfill(4)
    return df



# ---------- Intraday signal tracking ----------

SIGNAL_LOG_PATH = DATA_DIR / "intraday_signal_log_runtime.csv"
SURGE_SNAPSHOT_PATH = DATA_DIR / "intraday_last_snapshot_runtime.csv"
SURGE_EVENT_LOG_PATH = DATA_DIR / "intraday_surge_events_runtime.csv"
IMPORTANT_ALERTS = {"強勢進攻", "觀察偏強", "AI高分轉弱", "不要追高", "高風險上漲", "盤中轉弱"}
IMPORTANT_ENTRIES = {
    "強勢進攻，可盯突破",
    "觀察偏強，等回測確認",
    "AI高分但盤中轉弱",
    "漲幅偏高，不追",
    "風險偏高，暫不追",
    "盤中轉弱，避開",
}


def _safe_text(value: Any, default: str = "") -> str:
    try:
        if value is None:
            return default
        if isinstance(value, float) and math.isnan(value):
            return default
        return str(value)
    except Exception:
        return default


def _load_runtime_signal_log() -> pd.DataFrame:
    if SIGNAL_LOG_PATH.exists():
        try:
            df = pd.read_csv(SIGNAL_LOG_PATH, dtype={"代號": str})
            if "代號" in df.columns:
                df["代號"] = df["代號"].astype(str).str.zfill(4)
            return _v213_make_object_df(df)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def _save_runtime_signal_log(df: pd.DataFrame) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(SIGNAL_LOG_PATH, index=False, encoding="utf-8-sig")
    except Exception:
        pass



def should_track_signal(row: pd.Series, alert_name: str, entry_name: str) -> bool:
    """v2.6.1: tighten signal quality so the log does not record every neutral name.

    We keep high-value alerts, but only log 觀察偏強 when it has enough strength,
    positive momentum and volume support. This makes the tracking table useful in practice.
    """
    try:
        strength = _clean_number(row.get("即時強度分"))
        pct = _clean_number(row.get("盤中漲跌幅"))
        vol_score = _clean_number(row.get("盤中量能分"))
        risk = _clean_number(row.get("風險分"))
        ai = _clean_number(row.get("AI總分"))
        ai_source = _safe_text(row.get("AI來源"), "")
    except Exception:
        return False

    if alert_name in {"強勢進攻", "AI高分轉弱", "不要追高", "高風險上漲", "盤中轉弱"}:
        return True

    if entry_name == "強勢進攻，可盯突破":
        return True

    if alert_name == "觀察偏強" or entry_name == "觀察偏強，等回測確認":
        # Market-pool estimates need stronger evidence than fully scored AI names.
        if ai_source == "盤後AI":
            return strength >= 58 and pct >= 0.8 and vol_score >= 45 and risk < 40 and ai >= 45
        return strength >= 62 and pct >= 1.2 and vol_score >= 60 and risk < 35 and ai >= 55

    return False


def classify_signal_group(alert_name: str, entry_name: str) -> str:
    if alert_name == "強勢進攻" or entry_name == "強勢進攻，可盯突破":
        return "有效進攻"
    if alert_name == "觀察偏強" or entry_name == "觀察偏強，等回測確認":
        return "觀察訊號"
    if alert_name in {"AI高分轉弱", "盤中轉弱"} or entry_name in {"AI高分但盤中轉弱", "盤中轉弱，避開"}:
        return "轉弱訊號"
    if alert_name in {"不要追高", "高風險上漲"} or entry_name in {"漲幅偏高，不追", "風險偏高，暫不追"}:
        return "風險訊號"
    return "其他"


def update_runtime_signal_log(live_df: pd.DataFrame) -> pd.DataFrame:
    """Keep a local runtime signal log.

    This file is written by the Streamlit app instance. It survives browser refreshes,
    but it is not committed to GitHub and may reset after Streamlit reboot/redeploy.
    """
    now = now_taipei()
    today = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M:%S")

    log_df = _load_runtime_signal_log()
    if log_df.empty:
        log_df = pd.DataFrame()

    # Keep today's records only to avoid the runtime CSV growing forever.
    if not log_df.empty and "日期" in log_df.columns:
        log_df = log_df[log_df["日期"].astype(str) == today].copy()

    existing_keys = set(log_df["訊號Key"].astype(str)) if "訊號Key" in log_df.columns else set()
    new_rows: List[Dict[str, Any]] = []

    current_by_code: Dict[str, Dict[str, Any]] = {}
    for _, row in live_df.iterrows():
        code = _normalize_code(row.get("代號"))
        px = _to__clean_number(row.get("盤中現價"))
        if not re.match(r"^\d{4}$", code):
            continue
        current_by_code[code] = row.to_dict() | {"_current_px": px}

        alert_name = _safe_text(row.get("盤中警示"), "中性")
        entry_name = _safe_text(row.get("盤中入場判斷"), "僅觀察，未達入場條件")

        should_log = should_track_signal(row, alert_name, entry_name)
        if not should_log or math.isnan(px) or px <= 0:
            continue

        signal_group = classify_signal_group(alert_name, entry_name)
        signal_key = f"{today}|{code}|{signal_group}|{alert_name}|{entry_name}"
        if signal_key in existing_keys:
            continue

        new_rows.append(
            {
                "訊號Key": signal_key,
                "日期": today,
                "首次時間": now_time,
                "代號": code,
                "名稱": _safe_text(row.get("名稱"), code),
                "市場": _safe_text(row.get("市場"), "未知"),
                "產業": _safe_text(row.get("產業"), "未知"),
                "AI來源": _safe_text(row.get("AI來源"), ""),
                "資料來源": _safe_text(row.get("資料來源"), ""),
                "訊號類型": signal_group,
                "首次訊號": alert_name,
                "首次入場判斷": entry_name,
                "首次標籤": _safe_text(row.get("盤中標籤"), alert_name),
                "首次訊號價": round(px, 2),
                "目前價格": round(px, 2),
                "最高價格": round(px, 2),
                "最低價格": round(px, 2),
                "目前報酬%": 0.0,
                "最高報酬%": 0.0,
                "最大回撤%": 0.0,
                "AI總分": round(_clean_number(row.get("AI總分")), 1),
                "風險分": round(_clean_number(row.get("風險分")), 1),
                "首次即時強度分": round(_clean_number(row.get("即時強度分")), 1),
                "最新即時強度分": round(_clean_number(row.get("即時強度分")), 1),
                "首次盤中漲跌幅": round(_clean_number(row.get("盤中漲跌幅")), 2),
                "最新盤中漲跌幅": round(_clean_number(row.get("盤中漲跌幅")), 2),
                "盤中成交量": round(_clean_number(row.get("盤中成交量")), 0),
                "觸發價": _safe_text(row.get("觸發價"), "-"),
                "停損參考": _safe_text(row.get("停損參考"), "-"),
                "壓力參考": _safe_text(row.get("壓力參考"), "-"),
                "建議動作": _safe_text(row.get("建議動作"), ""),
                "最新時間": now_time,
            }
        )
        existing_keys.add(signal_key)

    if new_rows:
        log_df = pd.concat([log_df, pd.DataFrame(new_rows)], ignore_index=True)

    # Update current price and running max/min for all logged signals.
    if not log_df.empty:
        for idx, record in log_df.iterrows():
            code = _normalize_code(record.get("代號"))
            if code not in current_by_code:
                continue
            current_row = current_by_code[code]
            px = current_row.get("_current_px", np.nan)
            if math.isnan(px) or px <= 0:
                continue

            first_px = _to_float(record.get("首次訊號價"))
            if math.isnan(first_px) or first_px <= 0:
                continue

            prev_high = _to_float(record.get("最高價格"), px)
            prev_low = _to_float(record.get("最低價格"), px)
            high_px = max(prev_high if not math.isnan(prev_high) else px, px)
            low_px = min(prev_low if not math.isnan(prev_low) else px, px)

            current_ret = (px - first_px) / first_px * 100
            high_ret = (high_px - first_px) / first_px * 100
            low_ret = (low_px - first_px) / first_px * 100

            _v211_set_cell(log_df, idx, "目前價格", round(px, 2))
            _v211_set_cell(log_df, idx, "最高價格", round(high_px, 2))
            _v211_set_cell(log_df, idx, "最低價格", round(low_px, 2))
            _v211_set_cell(log_df, idx, "目前報酬%", round(current_ret, 2))
            _v211_set_cell(log_df, idx, "最高報酬%", round(high_ret, 2))
            _v211_set_cell(log_df, idx, "最大回撤%", round(low_ret, 2))
            log_df.loc[idx, "最新即時強度分"] = round(float(current_row.get("即時強度分", 0) or 0), 1)
            log_df.loc[idx, "最新盤中漲跌幅"] = round(float(current_row.get("盤中漲跌幅", 0) or 0), 2)
            log_df.loc[idx, "盤中成交量"] = round(float(current_row.get("盤中成交量", 0) or 0), 0)
            _v211_set_cell(log_df, idx, "最新時間", now_time)

        def status(row):
            ret = _clean_number(row.get("目前報酬%"))
            high_ret = _clean_number(row.get("最高報酬%"))
            drawdown = _clean_number(row.get("最大回撤%"))
            sig = _safe_text(row.get("首次訊號"), "")
            if ret >= 1.5:
                return "✅ 訊號有效"
            if high_ret >= 2.0 and ret < 0.5:
                return "⚠️ 衝高回落"
            if ret <= -1.5:
                return "❌ 訊號失效"
            if sig in {"AI高分轉弱", "盤中轉弱", "不要追高", "高風險上漲"}:
                return "🛡️ 風險提醒中"
            if drawdown <= -1.0 and ret <= 0:
                return "⚠️ 觀察轉弱"
            return "⏳ 追蹤中"

        log_df["目前狀態"] = log_df.apply(status, axis=1)

    _save_runtime_signal_log(log_df)
    return log_df


def clear_runtime_signal_log() -> None:
    try:
        if SIGNAL_LOG_PATH.exists():
            SIGNAL_LOG_PATH.unlink()
    except Exception:
        pass



# ---------- v2.11 AI signal learning engine ----------

V211_SIGNAL_LOG_PATH = DATA_DIR / "v211_ai_signal_learning_runtime.csv"
V211_TRACK_SIGNALS = {
    "✅ 左側可小量試單",
    "✅ 到價可小量試單",
    "⏳ 已到價，等止跌確認",
    "👀 前兆出現，等低吸",
    "🟡 等左側回測",
    "🟢 右側確認，只能加碼",
    "🔴 已錯過，不追",
    "⚫ 不買，結構不穩",
    "🟡 跌到低吸區下方，等止跌",
    "⚫ 買點失效，等止跌",
}
V211_ACTIONABLE_SIGNALS = {"✅ 左側可小量試單", "✅ 到價可小量試單"}
V211_EARLY_SIGNALS = {"👀 前兆出現，等低吸", "🟡 等左側回測", "⏳ 已到價，等止跌確認", "🟡 跌到低吸區下方，等止跌"}
V211_RIGHT_SIDE_SIGNALS = {"🟢 右側確認，只能加碼"}
V211_NO_BUY_SIGNALS = {"🔴 已錯過，不追", "⚫ 不買，結構不穩", "⚫ 買點失效，等止跌", "⚫ 跌破防守，不買"}


def _load_v211_learning_log() -> pd.DataFrame:
    if V211_SIGNAL_LOG_PATH.exists():
        try:
            df = pd.read_csv(V211_SIGNAL_LOG_PATH, dtype={"代號": str})
            if "代號" in df.columns:
                df["代號"] = df["代號"].astype(str).str.zfill(4)
            return _v213_make_object_df(df)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def _save_v211_learning_log(df: pd.DataFrame) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(V211_SIGNAL_LOG_PATH, index=False, encoding="utf-8-sig")
    except Exception:
        pass


def clear_v211_learning_log() -> None:
    try:
        if V211_SIGNAL_LOG_PATH.exists():
            V211_SIGNAL_LOG_PATH.unlink()
    except Exception:
        pass


# v2.11.4: runtime hardening for pandas scalar assignment.
# Streamlit reruns and old CSV logs can create duplicate columns / Series values.
# The learning engine must never crash because one cell is not a scalar.
def _v211_dedup_columns(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return df
        if df.columns.duplicated().any():
            return df.loc[:, ~df.columns.duplicated(keep="last")].copy()
        return df
    except Exception:
        return df


def _v211_scalar(value: Any, default: Any = "") -> Any:
    try:
        if isinstance(value, pd.Series):
            if value.empty:
                return default
            vals = value.dropna()
            if vals.empty:
                return default
            return _v211_scalar(vals.iloc[-1], default)
        if isinstance(value, np.ndarray):
            flat = value.ravel().tolist()
            return _v211_scalar(flat[-1] if flat else default, default)
        if isinstance(value, (list, tuple)):
            return _v211_scalar(value[-1] if value else default, default)
        if isinstance(value, (set, dict)):
            return str(value)
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, float) and math.isnan(value):
            return default
        return value
    except Exception:
        return default


def _v211_get_cell(df: pd.DataFrame, idx: Any, col: str, default: Any = "") -> Any:
    try:
        if col not in df.columns:
            return default
        return _v211_scalar(df.loc[idx, col], default)
    except Exception:
        return default


def _v211_set_cell(df: pd.DataFrame, idx: Any, col: str, value: Any) -> None:
    val = _v211_scalar(value, "")
    try:
        # Keep object dtype for mixed Chinese text / numeric fields to avoid pandas coercion crashes.
        if col not in df.columns:
            df[col] = np.nan
        try:
            df[col] = df[col].astype("object")
        except Exception:
            pass
        df.at[idx, col] = val
    except Exception:
        try:
            df.loc[idx, col] = str(val)
        except Exception:
            pass


def _v211_stock_type(row: pd.Series) -> str:
    code = _normalize_code(row.get("代號"))
    industry = _safe_text(row.get("產業"), "")
    pct = _clean_number(row.get("盤中漲跌幅"))
    precursor = _clean_number(row.get("v29漲停前兆分") or row.get("漲停前兆分"))
    source = _safe_text(row.get("AI來源"), "")
    if code == "3441" or precursor >= 65 or pct >= 6.0:
        return "小型強攻 / 漲停前兆股"
    if code in {"2382", "2313"}:
        return "中大型資金延續股"
    if "金融" in industry:
        return "金融穩推股"
    if source == "市場池估分":
        return "市場池動能股"
    if pct >= 3.0:
        return "題材急拉股"
    return "一般觀察股"


def _v211_signal_group(signal: str) -> str:
    if signal in V211_ACTIONABLE_SIGNALS:
        return "左側試單"
    if signal in V211_EARLY_SIGNALS:
        return "早期 / 等低吸"
    if signal in V211_RIGHT_SIDE_SIGNALS:
        return "右側確認"
    if signal in V211_NO_BUY_SIGNALS:
        return "不買 / 風險"
    return "其他"


def _v211_missing_confirmation(row: pd.Series) -> str:
    checks = []
    if _clean_number(row.get("盤中資金分")) < 55:
        checks.append("資金分不足")
    if _clean_number(row.get("左側低吸分")) < 55:
        checks.append("左側低吸分不足")
    if _clean_number(row.get("左側距停損%")) > 2.2:
        checks.append("停損距離偏遠")
    if _clean_number(row.get("盤中漲跌幅")) >= 7.5:
        checks.append("漲幅偏高")
    if _clean_number(row.get("風險分")) >= 40:
        checks.append("風險分偏高")
    if not checks:
        checks.append("等待下一輪價格/量能確認")
    return "、".join(checks)



def _v211_signal_key(today: str, code: str, signal: str = "") -> str:
    # v2.11.3: learning log should be one live state per stock per day.
    # The signal itself must NOT be part of the key, otherwise the same stock
    # can appear as "等低吸 / 可小量 / 不追" at the same time.
    return f"{today}|{code}"


def _v211_signal_priority(signal: str) -> int:
    """Lower number = stronger / more important current state."""
    signal = _safe_text(signal, "")
    order = {
        "✅ 到價可小量試單": 1,
        "✅ 左側可小量試單": 2,
        "⏳ 已到價，等止跌確認": 3,
        "👀 前兆出現，等低吸": 4,
        "🟡 等左側回測": 5,
        "🟢 右側確認，只能加碼": 6,
        "🟡 跌到低吸區下方，等止跌": 7,
        "🔴 已錯過，不追": 8,
        "⚫ 不買，結構不穩": 9,
        "⚫ 買點失效，等止跌": 10,
        "⚫ 跌破防守，不買": 11,
    }
    return order.get(signal, 99)


def _v211_collapse_learning_log(log_df: pd.DataFrame, today: str) -> pd.DataFrame:
    """v2.11.3: collapse old duplicate rows into one row per code.

    Previous versions used 日期+代號+訊號 as key, so the same stock could show
    contradictory states. Keep the earliest first-time row, then merge the most
    recent/current signal fields into that row.
    """
    if log_df.empty or "代號" not in log_df.columns:
        return log_df
    df = log_df.copy()
    if "日期" in df.columns:
        df = df[df["日期"].astype(str) == today].copy()
    if df.empty:
        return df
    df["代號"] = df["代號"].astype(str).str.zfill(4)
    if "最新時間" not in df.columns:
        df["最新時間"] = df.get("首次時間", "")
    if "首次時間" not in df.columns:
        df["首次時間"] = df.get("最新時間", "")
    keep_rows = []
    for code, g in df.groupby("代號", sort=False):
        g = g.copy()
        # Earliest row preserves the first price/time for learning performance.
        g_first = g.sort_values("首次時間", ascending=True).iloc[0].copy()
        # Latest row represents the current decision state shown to the user.
        g_latest = g.sort_values("最新時間", ascending=True).iloc[-1].copy()
        history_vals = []
        for _, r in g.sort_values(["首次時間", "最新時間"], ascending=True).iterrows():
            sig = _safe_text(r.get("交易員訊號"), "")
            if sig and (not history_vals or history_vals[-1] != sig):
                history_vals.append(sig)
        latest_signal = _safe_text(g_latest.get("交易員訊號"), _safe_text(g_first.get("交易員訊號"), ""))
        g_first["學習Key"] = _v211_signal_key(today, code)
        g_first["交易員訊號"] = latest_signal
        g_first["最新交易員訊號"] = latest_signal
        g_first["訊號分類"] = _v211_signal_group(latest_signal)
        g_first["訊號歷程"] = " → ".join(history_vals[-6:])
        g_first["訊號變更次數"] = max(0, len(history_vals) - 1)
        for col in ["目前價格", "最高價格", "最低價格", "目前報酬%", "最高報酬%", "最大回撤%", "最新盤中漲跌幅", "最新時間", "是否接近漲停", "是否碰停損", "學習狀態", "錯誤歸因"]:
            if col in g_latest.index:
                g_first[col] = g_latest.get(col)
        keep_rows.append(g_first.to_dict())
    return pd.DataFrame(keep_rows)


def update_v211_signal_learning(live_df: pd.DataFrame) -> pd.DataFrame:
    """Record v2.10 trader decisions and learn if each signal worked.

    v2.11.3 fix: a stock can have only ONE current learning row per day.
    When a signal changes, we update the existing row instead of appending a
    new contradictory row. The row keeps first price/time for performance, and
    latest signal/status for current decision quality.
    """
    now = now_taipei()
    today = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M:%S")

    # v2.11.4: normalize duplicate columns before any row.get()/loc assignment.
    live_df = _v211_dedup_columns(live_df.copy())
    log_df = _v211_dedup_columns(_load_v211_learning_log())
    if not log_df.empty and "日期" in log_df.columns:
        log_df = log_df[log_df["日期"].astype(str) == today].copy()
    if log_df.empty:
        log_df = pd.DataFrame()
    else:
        log_df = _v211_dedup_columns(_v211_collapse_learning_log(log_df, today))

    current_by_code: Dict[str, Dict[str, Any]] = {}
    if not log_df.empty and "代號" in log_df.columns:
        log_df["代號"] = log_df["代號"].astype(str).str.zfill(4)
    existing_codes = set(log_df["代號"].astype(str).str.zfill(4)) if not log_df.empty and "代號" in log_df.columns else set()
    new_rows: List[Dict[str, Any]] = []

    for _, row in live_df.iterrows():
        code = _normalize_code(row.get("代號"))
        px = _clean_number(row.get("盤中現價"))
        if not re.match(r"^\d{4}$", code) or px <= 0:
            continue
        current_by_code[code] = row.to_dict() | {"_current_px": px}

        signal = _safe_text(row.get("交易員訊號"), "")
        decision_score = _clean_number(row.get("v210決策分"))
        is_focus = code in FOCUS_CODES
        is_hot = _clean_number(row.get("v29漲停前兆分") or row.get("漲停前兆分")) >= 60
        if signal not in V211_TRACK_SIGNALS:
            continue
        if signal in V211_NO_BUY_SIGNALS and not (is_focus or is_hot or decision_score >= 55):
            continue

        limit_dist = _to_float(row.get("漲停距離%"), default=np.nan)
        near_limit = _clean_number(row.get("盤中漲跌幅")) >= 9.0 or (not math.isnan(limit_dist) and 0 < limit_dist <= 1.0)
        first_buy = _safe_text(row.get("第一買點") or row.get("左側試單價") or row.get("左側試單區"), "-")
        stop_text = _safe_text(row.get("防守停損") or row.get("左側停損價") or row.get("停損參考"), "-")
        right_add = _safe_text(row.get("右側加碼價") or row.get("右側確認價"), "-")
        max_chase = _safe_text(row.get("追價上限") or row.get("AI追價上限"), "-")
        group = _v211_signal_group(signal)

        if code in existing_codes:
            # Update the existing one-row-per-stock state instead of adding a duplicate.
            mask = log_df["代號"].astype(str).str.zfill(4) == code
            idxs = list(log_df.index[mask])
            if idxs:
                idx = idxs[0]
                old_signal = _safe_text(_v211_get_cell(log_df, idx, "交易員訊號", ""), "")
                history = _safe_text(_v211_get_cell(log_df, idx, "訊號歷程", old_signal), "")
                hist_parts = [h.strip() for h in history.split("→") if h.strip()]
                if not hist_parts and old_signal:
                    hist_parts = [old_signal]
                if signal and (not hist_parts or hist_parts[-1] != signal):
                    hist_parts.append(signal)
                _v211_set_cell(log_df, idx, "學習Key", _v211_signal_key(today, code))
                _v211_set_cell(log_df, idx, "交易員訊號", signal)
                _v211_set_cell(log_df, idx, "最新交易員訊號", signal)
                _v211_set_cell(log_df, idx, "訊號分類", group)
                _v211_set_cell(log_df, idx, "訊號歷程", " → ".join(hist_parts[-6:]))
                _v211_set_cell(log_df, idx, "訊號變更次數", max(0, len(hist_parts) - 1))
                # Refresh current decision context, but preserve 首次價格/首次時間.
                for col, val in {
                    "股票型態": _v211_stock_type(row),
                    "AI來源": _safe_text(row.get("AI來源"), ""),
                    "資料來源": _safe_text(row.get("資料來源"), ""),
                    "目前價格": round(px, 2),
                    "是否接近漲停": "是" if near_limit else _v211_get_cell(log_df, idx, "是否接近漲停", "否"),
                    "第一買點": first_buy,
                    "防守停損": stop_text,
                    "右側加碼價": right_add,
                    "追價上限": max_chase,
                    "v210決策分": round(decision_score, 1),
                    "即時入場分": round(_clean_number(row.get("即時入場分")), 1),
                    "左側低吸分": round(_clean_number(row.get("左側低吸分")), 1),
                    "盤中資金分": round(_clean_number(row.get("盤中資金分")), 1),
                    "漲停前兆分": round(_clean_number(row.get("v29漲停前兆分") or row.get("漲停前兆分")), 1),
                    "AI總分": round(_clean_number(row.get("AI總分")), 1),
                    "風險分": round(_clean_number(row.get("風險分")), 1),
                    "最新盤中漲跌幅": round(_clean_number(row.get("盤中漲跌幅")), 2),
                    "1分漲速%": round(_clean_number(row.get("1分漲速%")), 2),
                    "3分漲速%": round(_clean_number(row.get("3分漲速%")), 2),
                    "我會怎麼做": _safe_text(row.get("我會怎麼做"), ""),
                    "還缺什麼確認": _safe_text(row.get("還缺什麼確認"), _v211_missing_confirmation(row)),
                    "最新時間": now_time,
                }.items():
                    _v211_set_cell(log_df, idx, col, val)
            continue

        key = _v211_signal_key(today, code)
        new_rows.append({
            "學習Key": key,
            "日期": today,
            "首次時間": now_time,
            "代號": code,
            "名稱": _safe_text(row.get("名稱"), code),
            "股票型態": _v211_stock_type(row),
            "AI來源": _safe_text(row.get("AI來源"), ""),
            "資料來源": _safe_text(row.get("資料來源"), ""),
            "訊號分類": group,
            "交易員訊號": signal,
            "最新交易員訊號": signal,
            "訊號歷程": signal,
            "訊號變更次數": 0,
            "首次價格": round(px, 2),
            "目前價格": round(px, 2),
            "最高價格": round(px, 2),
            "最低價格": round(px, 2),
            "5分鐘後報酬%": np.nan,
            "15分鐘後報酬%": np.nan,
            "30分鐘後報酬%": np.nan,
            "60分鐘後報酬%": np.nan,
            "目前報酬%": 0.0,
            "最高報酬%": 0.0,
            "最大回撤%": 0.0,
            "是否碰停損": "否",
            "是否接近漲停": "是" if near_limit else "否",
            "學習狀態": "⏳ 追蹤中",
            "錯誤歸因": "",
            "第一買點": first_buy,
            "防守停損": stop_text,
            "右側加碼價": right_add,
            "追價上限": max_chase,
            "v210決策分": round(decision_score, 1),
            "即時入場分": round(_clean_number(row.get("即時入場分")), 1),
            "左側低吸分": round(_clean_number(row.get("左側低吸分")), 1),
            "盤中資金分": round(_clean_number(row.get("盤中資金分")), 1),
            "漲停前兆分": round(_clean_number(row.get("v29漲停前兆分") or row.get("漲停前兆分")), 1),
            "AI總分": round(_clean_number(row.get("AI總分")), 1),
            "風險分": round(_clean_number(row.get("風險分")), 1),
            "首次盤中漲跌幅": round(_clean_number(row.get("盤中漲跌幅")), 2),
            "最新盤中漲跌幅": round(_clean_number(row.get("盤中漲跌幅")), 2),
            "1分漲速%": round(_clean_number(row.get("1分漲速%")), 2),
            "3分漲速%": round(_clean_number(row.get("3分漲速%")), 2),
            "我會怎麼做": _safe_text(row.get("我會怎麼做"), ""),
            "還缺什麼確認": _safe_text(row.get("還缺什麼確認"), _v211_missing_confirmation(row)),
            "最新時間": now_time,
        })
        existing_codes.add(code)

    if new_rows:
        log_df = _v211_dedup_columns(pd.concat([log_df, pd.DataFrame(new_rows)], ignore_index=True))

    if not log_df.empty:
        # Final safety: one current row per stock before performance update.
        log_df = _v211_dedup_columns(_v211_collapse_learning_log(log_df, today))
        for idx, record in log_df.iterrows():
            code = _normalize_code(record.get("代號"))
            if code not in current_by_code:
                continue
            row = current_by_code[code]
            px = _clean_number(row.get("_current_px"))
            first_px = _clean_number(record.get("首次價格"))
            if px <= 0 or first_px <= 0:
                continue
            prev_high = _clean_number(record.get("最高價格")) or first_px
            prev_low = _clean_number(record.get("最低價格")) or first_px
            high_px = max(prev_high, px)
            low_px = min(prev_low, px)
            current_ret = (px - first_px) / first_px * 100
            high_ret = (high_px - first_px) / first_px * 100
            low_ret = (low_px - first_px) / first_px * 100

            _v211_set_cell(log_df, idx, "目前價格", round(px, 2))
            _v211_set_cell(log_df, idx, "最高價格", round(high_px, 2))
            _v211_set_cell(log_df, idx, "最低價格", round(low_px, 2))
            _v211_set_cell(log_df, idx, "目前報酬%", round(current_ret, 2))
            _v211_set_cell(log_df, idx, "最高報酬%", round(high_ret, 2))
            _v211_set_cell(log_df, idx, "最大回撤%", round(low_ret, 2))
            _v211_set_cell(log_df, idx, "最新盤中漲跌幅", round(_clean_number(row.get("盤中漲跌幅")), 2))
            _v211_set_cell(log_df, idx, "最新時間", now_time)

            limit_dist_now = _to_float(row.get("漲停距離%"), default=np.nan)
            if _clean_number(row.get("盤中漲跌幅")) >= 9.0 or (not math.isnan(limit_dist_now) and 0 < limit_dist_now <= 1.0):
                _v211_set_cell(log_df, idx, "是否接近漲停", "是")
            stop_num = _clean_number(record.get("防守停損"))
            if stop_num > 0 and low_px <= stop_num:
                _v211_set_cell(log_df, idx, "是否碰停損", "是")

            try:
                first_time = datetime.strptime(str(record.get("首次時間")), "%H:%M:%S").replace(year=now.year, month=now.month, day=now.day, tzinfo=TAIPEI_TZ)
                elapsed_min = (now - first_time).total_seconds() / 60
            except Exception:
                elapsed_min = 0
            for minutes, col in [(5, "5分鐘後報酬%"), (15, "15分鐘後報酬%"), (30, "30分鐘後報酬%"), (60, "60分鐘後報酬%")]:
                existing = _to_float(record.get(col), default=np.nan)
                if elapsed_min >= minutes and math.isnan(existing):
                    _v211_set_cell(log_df, idx, col, round(current_ret, 2))

            signal = _safe_text(_v211_get_cell(log_df, idx, "交易員訊號", record.get("交易員訊號")), "")
            hit_stop = _safe_text(_v211_get_cell(log_df, idx, "是否碰停損", "否"), "否") == "是"
            if signal in V211_ACTIONABLE_SIGNALS:
                if hit_stop or current_ret <= -1.2:
                    status, cause = "❌ 左側失敗", "碰停損或跌幅超過容忍"
                elif high_ret >= 2.0 and current_ret < 0.5:
                    status, cause = "⚠️ 衝高回落", "最高有利但未延續"
                elif current_ret >= 1.0 or high_ret >= 1.8:
                    status, cause = "✅ 左側有效", "訊號後有利延伸"
                else:
                    status, cause = "⏳ 追蹤中", "尚未分出勝負"
            elif signal in V211_NO_BUY_SIGNALS:
                if current_ret <= -1.0:
                    status, cause = "🛡️ 避開成功", "不買後走弱"
                elif high_ret >= 2.0:
                    status, cause = "⚠️ 可能錯過", "不買後仍上攻"
                else:
                    status, cause = "⏳ 風險追蹤", "尚未分出勝負"
            else:
                if current_ret >= 1.2 or high_ret >= 2.0:
                    status, cause = "✅ 觀察有效", "早期訊號後有上攻"
                elif current_ret <= -1.2 or hit_stop:
                    status, cause = "❌ 觀察失敗", "早期訊號後轉弱"
                else:
                    status, cause = "⏳ 追蹤中", "尚未分出勝負"
            _v211_set_cell(log_df, idx, "學習狀態", status)
            _v211_set_cell(log_df, idx, "錯誤歸因", cause)

    _save_v211_learning_log(log_df)
    return log_df

def build_v211_missed_limit_report(live_df: pd.DataFrame, learn_log: pd.DataFrame) -> pd.DataFrame:
    if live_df.empty:
        return pd.DataFrame()
    had_left = set()
    if not learn_log.empty:
        try:
            left_df = learn_log[learn_log.get("交易員訊號", pd.Series(dtype=str)).astype(str).isin(V211_ACTIONABLE_SIGNALS | V211_EARLY_SIGNALS)]
            had_left = set(left_df.get("代號", pd.Series(dtype=str)).astype(str).str.zfill(4))
        except Exception:
            had_left = set()
    rows = []
    for _, row in live_df.iterrows():
        code = _normalize_code(row.get("代號"))
        pct = _clean_number(row.get("盤中漲跌幅"))
        limit_dist = _to_float(row.get("漲停距離%"), default=np.nan)
        near_limit = pct >= 9.0 or (not math.isnan(limit_dist) and 0 < limit_dist <= 1.0)
        if not near_limit:
            continue
        sig = _safe_text(row.get("交易員訊號"), "")
        reasons = []
        if _safe_text(row.get("AI來源"), "") == "市場池估分": reasons.append("只有市場池估分")
        if _clean_number(row.get("左側低吸分")) < 55: reasons.append("左側低吸分不足")
        if _clean_number(row.get("盤中資金分")) < 55: reasons.append("盤中資金分不足")
        if _clean_number(row.get("風險分")) >= 40: reasons.append("風險分偏高")
        if sig in V211_NO_BUY_SIGNALS: reasons.append("交易員層判定不買")
        if not reasons: reasons.append("可能是漲速突然跳升，等待記憶層累積")
        rows.append({
            "代號": code,
            "名稱": _safe_text(row.get("名稱"), code),
            "目前價": _fmt_price(_clean_number(row.get("盤中現價"))),
            "盤中漲跌幅": round(pct, 2),
            "漲停距離%": np.nan if math.isnan(limit_dist) else round(limit_dist, 2),
            "交易員訊號": sig,
            "檢查結果": "已進過雷達" if code in had_left else "可能錯過",
            "可能原因": "、".join(reasons),
            "左側低吸分": round(_clean_number(row.get("左側低吸分")), 1),
            "盤中資金分": round(_clean_number(row.get("盤中資金分")), 1),
            "漲停前兆分": round(_clean_number(row.get("v29漲停前兆分") or row.get("漲停前兆分")), 1),
            "AI來源": _safe_text(row.get("AI來源"), ""),
        })
    return pd.DataFrame(rows)


def build_v211_learning_summary(log_df: pd.DataFrame) -> Dict[str, Any]:
    if log_df.empty:
        return {"total": 0, "actionable": 0, "effective": 0, "failed": 0, "false_break": 0, "left_success_rate": np.nan, "avg_high": 0.0, "avg_drawdown": 0.0, "best_type": "-", "worst_type": "-"}
    states = log_df.get("學習狀態", pd.Series(dtype=str)).astype(str)
    groups = log_df.get("訊號分類", pd.Series(dtype=str)).astype(str)
    actionable_mask = groups.eq("左側試單")
    effective_mask = states.str.contains("有效|避開成功", regex=True)
    failed_mask = states.str.contains("失敗|可能錯過", regex=True)
    false_break_mask = states.str.contains("衝高回落", regex=False)
    actionable_total = int(actionable_mask.sum())
    left_effective = int((actionable_mask & states.str.contains("有效", regex=False)).sum())
    left_success_rate = (left_effective / actionable_total * 100) if actionable_total else np.nan
    avg_high = float(pd.to_numeric(log_df.get("最高報酬%", 0), errors="coerce").fillna(0).mean())
    avg_drawdown = float(pd.to_numeric(log_df.get("最大回撤%", 0), errors="coerce").fillna(0).mean())
    best_type, worst_type = "-", "-"
    try:
        tmp = log_df.copy()
        tmp["成功"] = tmp["學習狀態"].astype(str).str.contains("有效|避開成功", regex=True).astype(int)
        by_type = tmp.groupby("訊號分類")["成功"].mean().sort_values(ascending=False)
        if not by_type.empty:
            best_type = f"{by_type.index[0]} {by_type.iloc[0]*100:.0f}%"
            worst_type = f"{by_type.index[-1]} {by_type.iloc[-1]*100:.0f}%"
    except Exception:
        pass
    return {"total": int(len(log_df)), "actionable": actionable_total, "effective": int(effective_mask.sum()), "failed": int(failed_mask.sum()), "false_break": int(false_break_mask.sum()), "left_success_rate": left_success_rate, "avg_high": avg_high, "avg_drawdown": avg_drawdown, "best_type": best_type, "worst_type": worst_type}


# ---------- Surge radar ----------

def _load_last_snapshot() -> pd.DataFrame:
    if SURGE_SNAPSHOT_PATH.exists():
        try:
            df = pd.read_csv(SURGE_SNAPSHOT_PATH, dtype={"代號": str})
            if "代號" in df.columns:
                df["代號"] = df["代號"].astype(str).str.zfill(4)
            return _v213_make_object_df(df)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def _save_last_snapshot(live_df: pd.DataFrame) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        now = now_taipei()
        cols = ["代號", "名稱", "市場", "盤中現價", "盤中成交量", "盤中漲跌幅", "最高", "最低", "報價時間"]
        cols = [c for c in cols if c in live_df.columns]
        snap = live_df[cols].copy()
        snap["快照日期"] = now.strftime("%Y-%m-%d")
        snap["快照時間"] = now.strftime("%H:%M:%S")
        snap.to_csv(SURGE_SNAPSHOT_PATH, index=False, encoding="utf-8-sig")
    except Exception:
        pass


def update_surge_radar(live_df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, bool]:
    """v2.6.2: detect short-window acceleration by comparing current quotes with previous refresh.

    This is a front-end/runtime radar. It detects *sudden change* between refreshes,
    not just the current strongest names.
    """
    df = live_df.copy()
    prev = _load_last_snapshot()
    has_prev = not prev.empty and {"代號", "盤中現價"}.issubset(prev.columns)

    for col, default in {
        "上一輪價格": np.nan,
        "上一輪成交量": np.nan,
        "上一輪漲跌幅": np.nan,
        "刷新漲速%": 0.0,
        "量能增量": 0.0,
        "量能跳升分": 0.0,
        "突破日內高": False,
        "爆衝警示": "",
        "爆衝建議": "",
    }.items():
        df[col] = default

    if has_prev:
        prev_cols = ["代號", "盤中現價", "盤中成交量", "盤中漲跌幅", "快照時間"]
        prev_cols = [c for c in prev_cols if c in prev.columns]
        pm = prev[prev_cols].copy().rename(
            columns={
                "盤中現價": "上一輪價格",
                "盤中成交量": "上一輪成交量",
                "盤中漲跌幅": "上一輪漲跌幅",
                "快照時間": "上一輪時間",
            }
        )
        df = df.drop(columns=[c for c in ["上一輪價格", "上一輪成交量", "上一輪漲跌幅"] if c in df.columns])
        df = df.merge(pm, on="代號", how="left")

        current_px = pd.to_numeric(df.get("盤中現價"), errors="coerce")
        prev_px = pd.to_numeric(df.get("上一輪價格"), errors="coerce")
        current_vol = pd.to_numeric(df.get("盤中成交量"), errors="coerce").fillna(0)
        prev_vol = pd.to_numeric(df.get("上一輪成交量"), errors="coerce").fillna(0)
        day_high = pd.to_numeric(df.get("最高"), errors="coerce")

        df["刷新漲速%"] = np.where(
            (current_px > 0) & (prev_px > 0),
            ((current_px - prev_px) / prev_px * 100).round(2),
            0.0,
        )
        df["量能增量"] = (current_vol - prev_vol).clip(lower=0).round(0)
        if df["量能增量"].max() > 0:
            df["量能跳升分"] = (df["量能增量"].rank(pct=True) * 100).round(1)
        else:
            df["量能跳升分"] = 0.0
        df["突破日內高"] = np.where((current_px > 0) & (day_high > 0), current_px >= day_high * 0.998, False)

        def surge_label(row):
            speed = _clean_number(row.get("刷新漲速%"))
            vol_jump = _clean_number(row.get("量能跳升分"))
            pct = _clean_number(row.get("盤中漲跌幅"))
            risk = _clean_number(row.get("風險分"))
            strength = _clean_number(row.get("即時強度分"))
            is_manual = bool(row.get("手動加入", False))
            high_break = bool(row.get("突破日內高", False))

            if speed <= -1.2:
                return "⚫ 急轉弱", "短時間價格下滑，先避開，不急著接。"
            if pct >= 7.0 and speed >= 0.3:
                return "🔴 已漲偏高", "已接近高漲幅區，先不追，等拉回或尾盤確認。"
            if speed >= 2.0:
                return "🟢 瞬間爆衝", "短線加速度明顯；只盯突破後是否站穩，勿市價亂追。"
            if speed >= 1.0 and (vol_jump >= 55 or high_break or is_manual):
                return "🟢 剛起漲", "短線價格轉強，可盯量能是否延續與回測不破。"
            if speed >= 0.5 and pct > 0 and (vol_jump >= 60 or high_break) and strength >= 45:
                return "🟡 爆量轉強", "量價同步轉強，等突破或回測確認。"
            return "", ""

        surge = df.apply(surge_label, axis=1)
        df["爆衝警示"] = [x[0] for x in surge]
        df["爆衝建議"] = [x[1] for x in surge]

    surge_df = df[df["爆衝警示"].astype(str).str.len() > 0].copy()
    if not surge_df.empty:
        priority = {"🟢 瞬間爆衝": 1, "🟢 剛起漲": 2, "🟡 爆量轉強": 3, "🔴 已漲偏高": 4, "⚫ 急轉弱": 5}
        surge_df["爆衝排序"] = surge_df["爆衝警示"].map(priority).fillna(9)
        surge_df = surge_df.sort_values(["爆衝排序", "刷新漲速%", "量能跳升分"], ascending=[True, False, False])

    _save_last_snapshot(df)
    return df, surge_df, has_prev



# ---------- v2.9 Intraday memory + left-side predictive AI engine ----------

MEMORY_PATH = DATA_DIR / "intraday_memory_runtime.csv"


def _load_intraday_memory() -> pd.DataFrame:
    if MEMORY_PATH.exists():
        try:
            df = pd.read_csv(MEMORY_PATH, dtype={"代號": str})
            if "代號" in df.columns:
                df["代號"] = df["代號"].astype(str).str.zfill(4)
            if "時間戳" in df.columns:
                df["時間戳"] = pd.to_datetime(df["時間戳"], errors="coerce")
            return df.dropna(subset=["時間戳"]) if "時間戳" in df.columns else pd.DataFrame()
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def _save_intraday_memory(df: pd.DataFrame) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(MEMORY_PATH, index=False, encoding="utf-8-sig")
    except Exception:
        pass


def _nearest_past_values(group: pd.DataFrame, now_ts: pd.Timestamp, minutes: int) -> Tuple[float, float, float]:
    """Return price, volume and pct near or before now-minutes."""
    try:
        target = now_ts - pd.Timedelta(minutes=minutes)
        g = group[group["時間戳"] <= target]
        if g.empty:
            return np.nan, np.nan, np.nan
        row = g.iloc[-1]
        return _to__clean_number(row.get("盤中現價")), _to__clean_number(row.get("盤中成交量")), _to__clean_number(row.get("盤中漲跌幅"))
    except Exception:
        return np.nan, np.nan, np.nan


def update_intraday_memory_features(live_df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """v2.9: Keep a short runtime memory so the system can judge acceleration and pullback.

    Without memory, the app can only judge right-side strength. With memory, it can ask:
    - Was money already coming in before price ran?
    - Did the pullback hold above support?
    - Is the stop distance short enough for a left-side test?
    """
    df = live_df.copy()
    now_dt = now_taipei()
    now_ts = pd.Timestamp(now_dt.replace(tzinfo=None))
    today = now_dt.strftime("%Y-%m-%d")
    now_time = now_dt.strftime("%H:%M:%S")

    hist = _load_intraday_memory()
    if not hist.empty and "日期" in hist.columns:
        hist = hist[hist["日期"].astype(str) == today].copy()
    if not hist.empty and "時間戳" in hist.columns:
        # Keep recent data only. This keeps the file small while preserving enough context.
        hist = hist[hist["時間戳"] >= now_ts - pd.Timedelta(minutes=150)].copy()

    snap_cols = ["代號", "名稱", "市場", "產業", "盤中現價", "盤中成交量", "盤中漲跌幅", "最高", "最低", "開盤", "昨收"]
    snap_cols = [c for c in snap_cols if c in df.columns]
    snap = df[snap_cols].copy()
    snap["代號"] = snap["代號"].astype(str).str.zfill(4)
    snap["日期"] = today
    snap["時間"] = now_time
    snap["時間戳"] = now_ts
    for col in ["盤中現價", "盤中成交量", "盤中漲跌幅", "最高", "最低", "開盤", "昨收"]:
        if col in snap.columns:
            snap[col] = pd.to_numeric(snap[col], errors="coerce")
    snap = snap[pd.to_numeric(snap.get("盤中現價"), errors="coerce").fillna(0) > 0].copy()

    if not snap.empty:
        # Avoid writing multiple identical rows for same refresh second.
        hist = pd.concat([hist, snap], ignore_index=True) if not hist.empty else snap.copy()
        hist = hist.drop_duplicates(subset=["代號", "時間戳"], keep="last")
        hist = hist.sort_values(["代號", "時間戳"]).reset_index(drop=True)
        _save_intraday_memory(hist)

    defaults = {
        "1分漲速%": 0.0,
        "3分漲速%": 0.0,
        "5分漲速%": 0.0,
        "10分漲速%": 0.0,
        "3分量增%": 0.0,
        "5分量增%": 0.0,
        "10分量增%": 0.0,
        "記憶高點": np.nan,
        "記憶低點": np.nan,
        "記憶回檔幅度%": np.nan,
        "盤中記憶筆數": 0,
    }
    for col, default in defaults.items():
        df[col] = default

    if hist.empty:
        return df, hist

    hist = hist.sort_values(["代號", "時間戳"])
    feature_rows: List[Dict[str, Any]] = []
    for code, group in hist.groupby("代號", sort=False):
        group = group.dropna(subset=["盤中現價"]).sort_values("時間戳")
        if group.empty:
            continue
        latest = group.iloc[-1]
        px_now = _to_float(latest.get("盤中現價"))
        vol_now = _to_float(latest.get("盤中成交量"), default=0.0)
        if math.isnan(px_now) or px_now <= 0:
            continue
        row: Dict[str, Any] = {"代號": str(code).zfill(4), "盤中記憶筆數": int(len(group))}
        for m in [1, 3, 5, 10]:
            px_past, vol_past, _ = _nearest_past_values(group, now_ts, m)
            speed = 0.0
            vol_inc = 0.0
            if not math.isnan(px_past) and px_past > 0:
                speed = (px_now - px_past) / px_past * 100
            if not math.isnan(vol_past) and vol_past > 0:
                vol_inc = (vol_now - vol_past) / vol_past * 100
            elif vol_now > 0:
                vol_inc = 0.0
            row[f"{m}分漲速%"] = round(speed, 2)
            if m in [3, 5, 10]:
                row[f"{m}分量增%"] = round(max(0.0, vol_inc), 1)

        recent = group[group["時間戳"] >= now_ts - pd.Timedelta(minutes=30)]
        if recent.empty:
            recent = group
        high = float(pd.to_numeric(recent.get("盤中現價"), errors="coerce").max())
        low = float(pd.to_numeric(recent.get("盤中現價"), errors="coerce").min())
        pullback = (high - px_now) / high * 100 if high > 0 else np.nan
        row["記憶高點"] = round(high, 2) if high > 0 else np.nan
        row["記憶低點"] = round(low, 2) if low > 0 else np.nan
        row["記憶回檔幅度%"] = round(pullback, 2) if not math.isnan(pullback) else np.nan
        feature_rows.append(row)

    if feature_rows:
        features = pd.DataFrame(feature_rows)
        df = df.drop(columns=[c for c in defaults if c in df.columns], errors="ignore")
        df = df.merge(features, on="代號", how="left")
        for col, default in defaults.items():
            if col not in df.columns:
                df[col] = default
            df[col] = df[col].fillna(default)

    return df, hist


def _parse_zone_low_high(text: Any) -> Tuple[float, float]:
    s = _safe_text(text, "")
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if not nums:
        return np.nan, np.nan
    vals = [_to_float(x) for x in nums]
    vals = [v for v in vals if not math.isnan(v) and v > 0]
    if not vals:
        return np.nan, np.nan
    if len(vals) == 1:
        return vals[0], vals[0]
    return min(vals[0], vals[1]), max(vals[0], vals[1])


def add_v29_left_predictive_ai(df: pd.DataFrame, chase_pct: float = 7.0) -> pd.DataFrame:
    """v2.9: Decide the way a trader would decide before buying.

    The model asks five practical questions:
    1. Is money coming in before the crowd sees it?
    2. Is price still near a support/low-risk area instead of already extended?
    3. Is the stop close enough to make the trade worth testing?
    4. Is there limit-up / re-attack potential?
    5. Is there a clear reason NOT to enter?
    """
    df = df.copy()

    def calc(row):
        code = _normalize_code(row.get("代號"))
        px = _to__clean_number(row.get("盤中現價"))
        prev = _to__clean_number(row.get("昨收"))
        open_px = _to__clean_number(row.get("開盤"))
        day_high = _to__clean_number(row.get("最高"))
        day_low = _to__clean_number(row.get("最低"))
        ai = _clean_number(row.get("AI總分"))
        risk = _clean_number(row.get("風險分"))
        strength = _clean_number(row.get("即時強度分"))
        pct = _clean_number(row.get("盤中漲跌幅"))
        vol_score = _clean_number(row.get("盤中量能分"))
        vol_jump = _clean_number(row.get("量能跳升分"))
        speed1 = _clean_number(row.get("1分漲速%"))
        speed3 = _clean_number(row.get("3分漲速%"))
        speed5 = _clean_number(row.get("5分漲速%"))
        speed10 = _clean_number(row.get("10分漲速%"))
        vol3 = _clean_number(row.get("3分量增%"))
        vol5 = _clean_number(row.get("5分量增%"))
        memory_pullback = _to_float(row.get("記憶回檔幅度%"), default=np.nan)
        precursor = _clean_number(row.get("漲停前兆分"))
        reattack_prob = _clean_number(row.get("再攻機率"))
        ai_source = _safe_text(row.get("AI來源"), "")
        entry_strategy = _safe_text(row.get("入場價位策略"), "")
        old_signal = _safe_text(row.get("入場訊號"), "")
        reattack_state = _safe_text(row.get("回檔再攻狀態"), "")

        if math.isnan(px) or px <= 0:
            return pd.Series({
                "盤後AI分": round(ai, 1),
                "盤中資金分": 0.0,
                "左側低吸分": 0.0,
                "即時入場分": 0.0,
                "AI即時入場訊號": "⚪ 無報價",
                "左側試單區": "-",
                "左側停損價": "-",
                "右側加碼價": "-",
                "AI追價上限": "-",
                "如果是我會確認": "等下一輪報價，不用猜。",
                "AI不進原因": "盤中報價不足",
                "AI建議操作": "不動作",
                "信心等級": "低",
                "左側距停損%": np.nan,
                "左側型態": "無報價",
            })

        support = _parse_price_text(row.get("回測支撐價"))
        if math.isnan(support) or support <= 0:
            support = _parse_price_text(row.get("停損參考"))
        if math.isnan(support) or support <= 0:
            refs = [x for x in [day_low, open_px, prev, px * 0.992] if not math.isnan(x) and x > 0]
            support = max(min(refs), px * 0.985) if refs else px * 0.992

        stop = _parse_price_text(row.get("防守停損價"))
        if math.isnan(stop) or stop <= 0:
            stop = _round_down_tick(support * 0.992)
        confirm = _parse_price_text(row.get("右側確認價"))
        if math.isnan(confirm) or confirm <= 0:
            confirm = _parse_price_text(row.get("二次攻擊觸發價"))
        if math.isnan(confirm) or confirm <= 0:
            confirm = _round_up_tick(max(px, support * 1.006))
        cap = _parse_price_text(row.get("追價上限"))
        if math.isnan(cap) or cap <= 0:
            cap = _round_up_tick(confirm * 1.004)

        # Build a true left-side zone near support, not near the right-side confirmation price.
        t = _tick_size(px)
        left_lo = _round_down_tick(max(stop + t, support * 0.998))
        left_hi = _round_down_tick(min(confirm - t, support * 1.006))
        if math.isnan(left_hi) or left_hi < left_lo:
            left_hi = _round_down_tick(support * 1.004)
        left_zone = _price_zone_text(left_lo, left_hi)

        stop_distance = (px - stop) / px * 100 if px > 0 and stop > 0 else np.nan
        near_support = bool(px >= left_lo * 0.998 and px <= max(left_hi, left_lo) * 1.006)
        above_support = bool(px >= support * 0.998)
        not_extended = bool(pct < max(6.8, chase_pct - 0.5) and px <= cap * 1.001)
        very_extended = bool(pct >= 8.2 or px > cap * 1.005)
        tight_stop = bool(not math.isnan(stop_distance) and 0.25 <= stop_distance <= 2.2)
        holding = bool(speed1 >= -0.25 and speed3 >= -0.6 and above_support)
        money_building = bool((vol_score >= 55 or vol_jump >= 60 or vol3 >= 12 or vol5 >= 20) and (speed3 >= -0.2 or speed5 >= 0.0))
        has_pullback = bool((not math.isnan(memory_pullback) and 0.6 <= memory_pullback <= 4.5) or reattack_state in {"🟢 等二次攻擊觸發", "✅ 二次攻擊可小量試單"})
        day_structure_ok = bool((math.isnan(day_low) or px >= day_low * 1.002) and (math.isnan(prev) or px >= prev * 0.985))
        market_pool_penalty = 7 if ai_source == "市場池估分" else 0

        # 盤中資金分: money flow and acceleration, not just price already high.
        fund_score = 0.0
        fund_score += min(28.0, max(0.0, vol_score) * 0.28)
        fund_score += min(22.0, max(0.0, vol_jump) * 0.22)
        fund_score += min(18.0, max(0.0, vol3) * 0.45)
        fund_score += min(12.0, max(0.0, vol5) * 0.25)
        fund_score += min(15.0, max(0.0, speed3) * 5.0)
        fund_score += 5.0 if strength >= 60 else 0.0
        fund_score = round(max(0.0, min(100.0, fund_score)), 1)

        # 左側低吸分: only high when price is near support and risk/reward is acceptable.
        left_score = 0.0
        left_score += 22.0 if near_support else 0.0
        left_score += 18.0 if tight_stop else 0.0
        left_score += 16.0 if holding else 0.0
        left_score += 14.0 if money_building else 0.0
        left_score += 10.0 if has_pullback else 0.0
        left_score += 8.0 if day_structure_ok else 0.0
        left_score += 7.0 if risk < 30 else 3.0 if risk < 40 else -10.0
        left_score += 5.0 if ai >= 55 else 2.0 if ai >= 45 else -5.0
        left_score -= 20.0 if very_extended else 0.0
        left_score -= market_pool_penalty
        left_score = round(max(0.0, min(100.0, left_score)), 1)

        # Keep limit-up potential independent: a stock can be an early radar even if left entry is not ready.
        limit_score = max(precursor, min(100.0, 35 + max(0, speed3) * 7 + max(0, speed5) * 4 + min(20, vol_jump * 0.2) + max(0, pct) * 2))
        limit_score = round(max(0.0, min(100.0, limit_score)), 1)

        entry_score = (
            left_score * 0.42
            + fund_score * 0.26
            + limit_score * 0.18
            + ai * 0.12
            - risk * 0.12
        )
        entry_score = round(max(0.0, min(100.0, entry_score)), 1)

        check_items = []
        check_items.append("資金有進來" if money_building else "資金還沒確認")
        check_items.append("靠近支撐" if near_support else "沒有在低風險區")
        check_items.append("停損距離短" if tight_stop else "停損距離不夠漂亮")
        check_items.append("回檔守住" if holding else "回檔還沒守穩")
        check_items.append("未過熱" if not very_extended else "已過熱")
        check_text = "、".join(check_items)

        # Different personality by stock type: small attack names vs large caps.
        is_focus_attack = code in {"3441", "3362", "3105", "6223"}
        is_large_cap = code in {"2382", "2313", "2330", "2379", "4938"}
        if is_focus_attack:
            left_type = "小型強攻股：看急拉回檔守支撐、量縮不破、二次攻擊"
        elif is_large_cap:
            left_type = "中大型資金股：看資金延續、回測均價/支撐不破、慢推"
        else:
            left_type = "一般動能股：先看資金分，再看支撐與停損距離"

        if very_extended:
            signal = "🔴 錯過不追"
            reason = "已經高於合理追價區或漲幅過高，左側已經消失。"
            action = f"不追；只等回測 {left_zone} 附近，或尾盤確認。"
            confidence = "中"
            priority = 6
        elif risk >= 45 or pct <= -2.2 or strength < 32:
            signal = "⚫ 避開"
            reason = "風險、盤中強度或價格結構不支持左側試單。"
            action = "不動作，等重新轉強。"
            confidence = "低"
            priority = 7
        elif left_score >= 68 and fund_score >= 48 and tight_stop and near_support and holding and not_extended:
            signal = "✅ 左側可小量試單"
            reason = "資金進來、回檔守住、價格在低風險區，且停損距離短。"
            action = f"可小量在 {left_zone} 試單；跌破 {_fmt_price(stop)} 退出；站穩 {_fmt_price(confirm)} 才考慮加碼。"
            confidence = "高" if entry_score >= 72 and ai_source == "盤後AI" else "中"
            priority = 1
        elif old_signal == "✅ 可小量試單" and px <= cap and holding and fund_score >= 45:
            signal = "🟢 右側突破可加碼"
            reason = "已偏右側確認，適合已持有者加碼觀察，不是最佳第一買點。"
            action = f"若沒有底倉，不要追過 {_fmt_price(cap)}；等回測 {left_zone} 更漂亮。"
            confidence = "中"
            priority = 2
        elif fund_score >= 62 and limit_score >= 55 and pct < 6.5:
            signal = "👀 資金提前佈局"
            reason = "量能與資金分升溫，但價格還沒到理想低吸條件。"
            action = f"先掛雷達；理想低吸區 {left_zone}，右側加碼價 {_fmt_price(confirm)}。"
            confidence = "中"
            priority = 3
        elif left_score >= 55 and not near_support:
            signal = "🟡 等左側回測"
            reason = "條件有一部分成立，但現在不在低吸區，容易買在中間。"
            action = f"等回到 {left_zone} 且不破 {_fmt_price(stop)}。"
            confidence = "中"
            priority = 4
        elif limit_score >= 60 and not very_extended:
            signal = "🚀 漲停前兆升溫"
            reason = "有爆衝/漲停前兆，但左側買點尚未成立。"
            action = f"只盯不追；等回測 {left_zone} 或站穩 {_fmt_price(confirm)} 後再評估。"
            confidence = "中"
            priority = 4
        else:
            signal = "⚪ 觀察"
            reason = "資金、價格位置、停損距離還沒有同時成立。"
            action = "先觀察，不急著進。"
            confidence = "低"
            priority = 5

        return pd.Series({
            "盤後AI分": round(ai, 1),
            "盤中資金分": fund_score,
            "左側低吸分": left_score,
            "即時入場分": entry_score,
            "v29漲停前兆分": limit_score,
            "AI即時入場訊號": signal,
            "AI入場優先級": priority,
            "左側試單區": left_zone,
            "左側停損價": _fmt_price(stop),
            "右側加碼價": _fmt_price(confirm),
            "AI追價上限": _fmt_price(cap),
            "如果是我會確認": check_text,
            "AI不進原因": reason,
            "AI建議操作": action,
            "信心等級": confidence,
            "左側距停損%": round(stop_distance, 2) if not math.isnan(stop_distance) else np.nan,
            "左側型態": left_type,
        })

    out = df.apply(calc, axis=1)
    for col in out.columns:
        df[col] = out[col]
    return df


def _safe_price_text(value: Any) -> str:
    try:
        if value is None:
            return "-"
        s = str(value).strip()
        return s if s and s.lower() not in {"nan", "none"} else "-"
    except Exception:
        return "-"


def _price_or_nan(value: Any) -> float:
    try:
        v = _to_float(value, default=np.nan)
        if isinstance(v, float) and math.isnan(v):
            return np.nan
        return float(v)
    except Exception:
        return np.nan


def _price_range_from_text(value: Any) -> Tuple[float, float]:
    try:
        s = _safe_price_text(value)
        nums = re.findall(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
        vals = []
        for n in nums[:2]:
            v = _price_or_nan(n)
            if not math.isnan(v) and v > 0:
                vals.append(v)
        if not vals:
            return np.nan, np.nan
        if len(vals) == 1:
            return vals[0], vals[0]
        return min(vals), max(vals)
    except Exception:
        return np.nan, np.nan


def add_v210_trader_decision(df: pd.DataFrame, chase_pct: float = 7.0) -> pd.DataFrame:
    """v2.11.2: trader decision layer with price-arrival confirmation.

    Key fix:
    A planned left-side buy zone is NOT invalid just because current price is below it.
    It is invalid only when price breaks the defensive stop / structure.
    """
    df = df.copy()

    def calc(row: pd.Series) -> pd.Series:
        code = _normalize_code(row.get("代號"))
        px = _price_or_nan(row.get("盤中現價"))
        prev = _price_or_nan(row.get("昨收"))
        open_px = _price_or_nan(row.get("開盤"))
        day_high = _price_or_nan(row.get("最高"))
        day_low = _price_or_nan(row.get("最低"))
        ai = _clean_number(row.get("AI總分"))
        risk = _clean_number(row.get("風險分"))
        strength = _clean_number(row.get("即時強度分"))
        pct = _clean_number(row.get("盤中漲跌幅"))
        vol_score = _clean_number(row.get("盤中量能分"))
        fund_score = _clean_number(row.get("盤中資金分"))
        left_score = _clean_number(row.get("左側低吸分"))
        entry_score = _clean_number(row.get("即時入場分"))
        limit_score = _clean_number(row.get("v29漲停前兆分", row.get("漲停前兆分")))
        speed1 = _clean_number(row.get("1分漲速%", row.get("刷新漲速%")))
        speed3 = _clean_number(row.get("3分漲速%"))
        ai_source = str(row.get("AI來源", ""))
        v29_signal = str(row.get("AI即時入場訊號", ""))

        if math.isnan(px) or px <= 0:
            return pd.Series({
                "交易員訊號": "⚪ 無報價", "我會不會買": "不判斷", "v210優先級": 9,
                "v210決策分": 0.0, "第一買點": "-", "現在位置": "無報價", "到價狀態": "無報價",
                "左側試單價": "-", "防守停損": "-", "右側加碼價": "-", "追價上限": "-",
                "我會怎麼做": "等下一次報價刷新。", "不能買原因": "盤中報價不足",
                "還缺什麼確認": "需要先有現價、漲跌幅、量能。", "交易型態": "無資料",
            })

        zone_lo, zone_hi = _price_range_from_text(row.get("左側試單區", row.get("左側低吸區")))
        if math.isnan(zone_lo) or zone_lo <= 0:
            candidates = [x for x in [day_low, prev, open_px, px * 0.985] if not math.isnan(x) and x > 0]
            base = min(candidates) if candidates else px * 0.985
            zone_lo = _round_tick(base)
            zone_hi = _round_tick(min(px, base * 1.006))
            if zone_hi < zone_lo:
                zone_lo, zone_hi = zone_hi, zone_lo

        stop = _price_or_nan(row.get("左側停損價", row.get("防守停損價", row.get("停損參考"))))
        if math.isnan(stop) or stop <= 0:
            stop = _round_tick(zone_lo * 0.99)
        confirm = _price_or_nan(row.get("右側加碼價", row.get("右側確認價", row.get("觸發價"))))
        if math.isnan(confirm) or confirm <= 0:
            base_high = day_high if not math.isnan(day_high) and day_high > 0 else px
            confirm = _round_tick(max(px * 1.006, base_high * 1.001))
        cap = _price_or_nan(row.get("AI追價上限", row.get("追價上限")))
        if math.isnan(cap) or cap <= 0:
            cap = _round_tick(max(zone_hi * 1.018, confirm * 1.01, px * 1.018))

        stop_dist = (px - stop) / px * 100 if px > 0 and stop > 0 else np.nan
        under_cap = px <= cap * 1.001
        too_hot = pct >= chase_pct or px > cap * 1.004 or speed1 >= 2.8

        in_zone = zone_lo * 0.998 <= px <= zone_hi * 1.006
        below_zone_but_above_stop = px < zone_lo and px > stop * 1.002
        price_arrived = (px <= zone_hi * 1.006) and (px > stop * 1.002)
        stop_broken = px <= stop * 1.002
        above_zone = px > zone_hi * 1.006
        stop_tight = not math.isnan(stop_dist) and 0.25 <= stop_dist <= 3.0
        stop_too_far = not math.isnan(stop_dist) and stop_dist > 3.0

        holding_support = True
        if not math.isnan(day_low) and day_low > 0:
            holding_support = px >= max(day_low * 1.001, stop * 1.002)
        if not math.isnan(prev) and prev > 0 and pct < -3.5:
            holding_support = False

        money_in = (fund_score >= 55) or (vol_score >= 60 and strength >= 55) or (speed3 > 0.4 and limit_score >= 55)
        selling_slowing = (speed1 >= -0.25 and strength >= 38) or (money_in and speed1 >= -0.55) or (pct > 0 and strength >= 42)
        market_pool_discount = 4 if ai_source == "市場池估分" else 0

        decision_score = (
            min(100, left_score) * 0.26
            + min(100, fund_score) * 0.24
            + min(100, entry_score) * 0.18
            + min(100, limit_score) * 0.12
            + min(100, ai) * 0.08
            + min(100, strength) * 0.08
            - min(100, risk) * 0.14
        ) - market_pool_discount
        if price_arrived:
            decision_score += 10
        if below_zone_but_above_stop and stop_tight:
            decision_score += 6
        if stop_tight:
            decision_score += 8
        if selling_slowing:
            decision_score += 6
        if too_hot:
            decision_score -= 25
        if stop_broken:
            decision_score -= 35
        if not holding_support:
            decision_score -= 14
        if stop_too_far:
            decision_score -= 8
        decision_score = round(max(0.0, min(100.0, decision_score)), 1)

        if code in {"3441", "3362", "3105", "6223"}:
            trade_type = "強攻股：重點看急拉回檔是否守住、量能是否縮後再放大"
        elif code in {"2382", "2313", "2330", "2379", "4938"}:
            trade_type = "資金股：重點看回測支撐、量能延續，不用追極短線急拉"
        else:
            trade_type = "動能股：先看資金分，再看位置與停損距離"

        if below_zone_but_above_stop:
            dyn_lo = _round_tick(max(stop * 1.004, px * 0.996))
            dyn_hi = _round_tick(min(zone_lo, px * 1.006))
            active_zone_text = f"重算：{_fmt_price(dyn_lo)}～{_fmt_price(dyn_hi)}"
            first_buy_active = f"{_fmt_price(dyn_lo)}～{_fmt_price(dyn_hi)}"
            position_state = "低於原區但仍在防守上方"
        else:
            active_zone_text = f"{_fmt_price(zone_lo)}～{_fmt_price(zone_hi)}"
            first_buy_active = f"{_fmt_price(zone_lo)}～{_fmt_price(zone_hi)}"
            position_state = "到價區" if in_zone else "等待區" if above_zone else "防守區"

        missing = []
        if not price_arrived:
            missing.append("價格還沒到左側試單區")
        if price_arrived and not selling_slowing:
            missing.append("已到價，但還沒看到止跌/賣壓收斂")
        if not money_in:
            missing.append("資金/量能還不夠明確")
        if not stop_tight:
            missing.append("停損距離不夠漂亮")
        if not holding_support:
            missing.append("支撐尚未守穩")
        if too_hot:
            missing.append("短線已過熱")
        if risk >= 42:
            missing.append("風險分偏高")
        missing_text = "條件大致同步，重點是只小量，不重倉。" if not missing else "；".join(missing[:4])

        if stop_broken:
            signal, can_buy, priority = "⚫ 跌破防守，不買", "不可買", 9
            action = f"現價 {_fmt_price(px)} 已碰到/跌破防守停損 {_fmt_price(stop)}，這不是便宜，是結構破壞；等重新站回 {_fmt_price(zone_lo)} 並止跌後再看。"
            reason, first_buy, pos, arrive_state = "防守價已破，左側試單邏輯失效。", "不成立", "跌破防守", "跌破防守"
        elif price_arrived and stop_tight and holding_support and selling_slowing and risk < 42 and not too_hot and decision_score >= 55:
            signal, can_buy, priority = "✅ 到價可小量試單", "可小量", 1
            action = f"已到可試單位置。只用小量在 {first_buy_active} 試單；跌破 {_fmt_price(stop)} 立刻退出；站回 {_fmt_price(confirm)} 才看加碼。"
            reason, first_buy, pos, arrive_state = "價格已到低風險區，且防守距離短、支撐尚未破壞。", first_buy_active, "到價可試單", "已到價且確認中"
        elif price_arrived and stop_tight and not stop_broken and risk < 45 and not too_hot:
            signal, can_buy, priority = "⏳ 已到價，等止跌確認", "等確認", 2
            action = f"價格已到低吸區，但還缺止跌/承接確認。下一輪若不破 {_fmt_price(stop)}，且刷新漲速轉正或量能回來，才小量試單。"
            reason, first_buy, pos, arrive_state = "到價是必要條件，不是唯一條件；目前還差止跌確認。", first_buy_active, position_state, "已到價但未確認"
        elif risk >= 48 or strength < 30 or pct <= -3.5 or not holding_support:
            signal, can_buy, priority = "⚫ 不買，結構不穩", "不可買", 8
            action = f"不接刀；等重新站回 {_fmt_price(confirm)} 或回到 {active_zone_text} 後出現止跌。"
            reason, first_buy, pos, arrive_state = "支撐/強度/風險其中一項不合格。", "不成立", "轉弱或結構不穩", "未確認"
        elif too_hot:
            signal, can_buy, priority = "🔴 已錯過，不追", "不可買", 7
            action = f"不追；等回測 {active_zone_text}，或尾盤重新確認。"
            reason, first_buy, pos, arrive_state = "離左側區太遠或漲速過快，追高容易買在尖端。", "已錯過", "過熱區", "高於買點"
        elif money_in and limit_score >= 62 and pct < chase_pct and under_cap:
            signal, can_buy, priority = "👀 前兆出現，等低吸", "先不買", 3
            action = f"放進雷達，不追現價；等到 {active_zone_text} 且不破 {_fmt_price(stop)}。"
            reason, first_buy, pos, arrive_state = "有資金/漲停前兆，但第一買點還沒出現。", f"等 {active_zone_text}", "前兆區，還不是買點", "未到價"
        elif px >= confirm and under_cap and money_in and risk < 42:
            signal, can_buy, priority = "🟢 右側確認，只能加碼", "空手不追", 4
            action = f"這不是第一買點；有底倉才考慮加碼，空手等回測 {active_zone_text}。"
            reason, first_buy, pos, arrive_state = "已經右側確認，勝率來自底倉優勢，不適合空手追。", "非第一買點", "右側確認區", "右側"
        elif left_score >= 52 or v29_signal in {"🟡 等左側回測", "⚪ 觀察"}:
            signal, can_buy, priority = "🟡 等左側回測", "等待", 5
            action = f"等價格靠近 {active_zone_text}，且量縮不破，再評估小試。"
            reason, first_buy, pos, arrive_state = "還沒到漂亮位置，現在買容易卡中間。", active_zone_text, "等待區", "未到價"
        else:
            signal, can_buy, priority = "⚪ 觀察，不急", "不急", 6
            action = "沒有同時出現資金、位置、停損優勢；先等下一輪。"
            reason, first_buy, pos, arrive_state = "分數或條件尚未同步。", "尚未出現", "普通觀察區", "未到價"

        return pd.Series({
            "交易員訊號": signal, "我會不會買": can_buy, "v210優先級": priority,
            "v210決策分": decision_score, "第一買點": first_buy, "現在位置": pos, "到價狀態": arrive_state,
            "左側試單價": active_zone_text, "防守停損": _fmt_price(stop), "右側加碼價": _fmt_price(confirm),
            "追價上限": _fmt_price(cap), "我會怎麼做": action, "不能買原因": reason,
            "還缺什麼確認": missing_text, "交易型態": trade_type,
        })

    try:
        out = df.apply(calc, axis=1)
        for col in out.columns:
            df[col] = out[col]
    except Exception as e:
        df["交易員訊號"] = "⚪ 決策層暫停"
        df["我會不會買"] = "不判斷"
        df["v210優先級"] = 9
        df["v210決策分"] = 0.0
        df["第一買點"] = "-"
        df["現在位置"] = "-"
        df["到價狀態"] = "-"
        df["左側試單價"] = "-"
        df["防守停損"] = "-"
        df["右側加碼價"] = "-"
        df["追價上限"] = "-"
        df["我會怎麼做"] = f"決策層防呆啟動：{type(e).__name__}"
        df["不能買原因"] = "決策層錯誤已攔截，主表不當機。"
        df["還缺什麼確認"] = "請查看原始分數欄位。"
        df["交易型態"] = "防呆"
    return df


def clear_intraday_memory() -> None:
    try:
        if MEMORY_PATH.exists():
            MEMORY_PATH.unlink()
    except Exception:
        pass


# ---------- v2.12 UI / Lifecycle Engine ----------

def _v212_parse_price_range(value: Any) -> Tuple[float, float]:
    """Parse strings like '72.60～73.20', '等 291', '非第一買點'."""
    text = _safe_text(value, "")
    nums = re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not nums:
        return (np.nan, np.nan)
    vals = [float(x) for x in nums]
    if len(vals) == 1:
        return (vals[0], vals[0])
    return (min(vals[0], vals[1]), max(vals[0], vals[1]))


def _v212_signal_stage(row: pd.Series) -> pd.Series:
    """One-current-state lifecycle decision. No duplicate/conflicting signals."""
    px = _clean_number(row.get("盤中現價"))
    stop = _clean_number(row.get("防守停損") or row.get("左側停損價") or row.get("停損參考"))
    first_buy_text = _safe_text(row.get("第一買點") or row.get("左側試單價") or row.get("左側試單區"), "-")
    buy_low, buy_high = _v212_parse_price_range(first_buy_text)
    sig = _safe_text(row.get("交易員訊號"), "")
    learn_state = _safe_text(row.get("學習狀態"), "")
    hist = _safe_text(row.get("訊號歷程"), "")
    risk = _clean_number(row.get("風險分"))
    money = _clean_number(row.get("盤中資金分"))
    left = _clean_number(row.get("左側低吸分"))
    limit_score = _clean_number(row.get("v29漲停前兆分") or row.get("漲停前兆分"))
    pct = _clean_number(row.get("盤中漲跌幅"))
    ret = _clean_number(row.get("目前報酬%"))
    high_ret = _clean_number(row.get("最高報酬%"))
    drawdown = _clean_number(row.get("最大回撤%"))

    if px <= 0:
        return pd.Series({
            "v212生命週期狀態": "⚪ 無報價",
            "v212目前決策": "等待報價",
            "v212位置判斷": "無法判斷",
            "v212下一步": "等下一輪報價，不做決策。",
            "v212優先級": 99,
        })

    # Position relative to left-side zone.
    if not math.isnan(buy_low) and not math.isnan(buy_high) and buy_high > 0:
        if px < buy_low:
            if stop > 0 and px > stop:
                pos = "低於左側區，但仍守防守"
            elif stop > 0 and px <= stop:
                pos = "跌破防守"
            else:
                pos = "低於左側區，需重算結構"
        elif buy_low <= px <= buy_high:
            pos = "已到左側試單區"
        elif px <= buy_high * 1.01:
            pos = "貼近左側區上緣"
        else:
            pos = "高於左側區"
    else:
        pos = "買點未定義"

    # Lifecycle state machine.
    if stop > 0 and px <= stop:
        stage = "⚫ 取消 / 跌破防守"
        decision = "不買"
        next_step = "已低於防守價，這不是便宜，是結構破壞；等重新築底。"
        priority = 90
    elif learn_state.startswith("✅") or ("有效" in learn_state and ret >= 0):
        stage = "✅ 訊號有效追蹤"
        decision = "續追蹤"
        next_step = "訊號後有利延伸，若已有試單看防守停損與量能延續；空手不追高。"
        priority = 7
    elif sig in {"✅ 到價可小量試單", "✅ 左側可小量試單"}:
        stage = "✅ 到價確認 / 可試單"
        decision = "可小量"
        next_step = "只適合小量試單；跌破防守停損立刻退出，不用等右側確認。"
        priority = 1
    elif sig == "⏳ 已到價，等止跌確認" or ("已到左側" in pos and left >= 50):
        stage = "⏳ 到價等確認"
        decision = "等一輪止跌"
        next_step = "已到位置，但還缺止跌/賣壓收斂；下一輪若價格不破低、資金分不掉，才轉可小量。"
        priority = 2
    elif "前兆" in sig or (limit_score >= 65 and money >= 55):
        stage = "👀 前兆出現"
        decision = "等低吸"
        next_step = "有資金或漲停前兆，但不要追；等回到左側區或量縮守住。"
        priority = 3
    elif "等左側" in sig or "等低吸" in sig:
        if "已到左側" in pos or "低於左側" in pos:
            stage = "⏳ 到價等確認"
            decision = "等止跌確認"
            next_step = "價格已到/低於原本左側區，重點不是繼續等回測，而是確認有沒有止跌守防守。"
            priority = 2
        else:
            stage = "🟡 候選 / 等回測"
            decision = "等待"
            next_step = "還沒到低風險區，不要買在中間；等價格接近第一買點。"
            priority = 5
    elif "右側" in sig:
        stage = "🟢 右側確認 / 加碼點"
        decision = "空手不追"
        next_step = "這不是第一買點；有底倉才考慮加碼，空手等回測。"
        priority = 6
    elif "錯過" in sig or pct >= 7.5:
        stage = "🔴 錯過 / 不追"
        decision = "不追"
        next_step = "左側機會已過或漲幅偏高；等回檔重新出現左側區。"
        priority = 80
    elif "不買" in sig or "結構不穩" in sig:
        stage = "⚫ 不買 / 結構不穩"
        decision = "不買"
        next_step = "資金、位置或風險條件不同步；先排除。"
        priority = 85
    elif money >= 58 and left >= 55 and risk < 45:
        stage = "👀 候選升溫"
        decision = "等到價"
        next_step = "資金與左側條件不錯，等價格進入第一買點再判斷。"
        priority = 4
    else:
        stage = "⚪ 候選觀察"
        decision = "不急"
        next_step = "還不是交易點；不要為了怕錯過而提前買。"
        priority = 60

    # Extra note for possible whipsaw.
    if high_ret >= 2.0 and ret < 0.5 and "追蹤" in learn_state:
        stage = "⚠️ 衝高回落追蹤"
        decision = "保守"
        next_step = "曾經有利但回落，先看是否守住防守價，不要加碼。"
        priority = min(priority, 8)
    if drawdown <= -1.5 and decision in {"可小量", "續追蹤"}:
        next_step += " 目前回撤偏大，部位要更小。"

    return pd.Series({
        "v212生命週期狀態": stage,
        "v212目前決策": decision,
        "v212位置判斷": pos,
        "v212下一步": next_step,
        "v212優先級": priority,
    })


def build_v212_lifecycle(live_df: pd.DataFrame, learning_log: pd.DataFrame) -> pd.DataFrame:
    df = live_df.copy()
    if df.empty:
        return df
    df["代號"] = df["代號"].astype(str).str.zfill(4)
    if learning_log is not None and not learning_log.empty and "代號" in learning_log.columns:
        keep = [
            "代號", "首次時間", "首次價格", "目前價格", "目前報酬%", "最高報酬%", "最大回撤%",
            "學習狀態", "訊號歷程", "訊號變更次數", "錯誤歸因", "最新時間"
        ]
        keep = [c for c in keep if c in learning_log.columns]
        lg = learning_log[keep].copy()
        lg["代號"] = lg["代號"].astype(str).str.zfill(4)
        # Deduplicate defensively: one row per stock.
        lg = lg.drop_duplicates(subset=["代號"], keep="last")
        df = df.merge(lg, on="代號", how="left", suffixes=("", "_學習"))
    stage_out = df.apply(_v212_signal_stage, axis=1)
    for col in stage_out.columns:
        df[col] = stage_out[col]
    df["v212排序分"] = pd.to_numeric(df.get("v210決策分", 0), errors="coerce").fillna(0) + pd.to_numeric(df.get("即時入場分", 0), errors="coerce").fillna(0) * 0.25
    return df.sort_values(["v212優先級", "v212排序分", "即時強度分"], ascending=[True, False, False])


def _cols_exist(df: pd.DataFrame, cols: List[str]) -> List[str]:
    return [c for c in cols if c in df.columns]

def _ensure_columns(df: pd.DataFrame, defaults: Dict[str, Any]) -> pd.DataFrame:
    """Add missing columns before display/sort so UI never crashes on sparse data."""
    if df is None:
        return pd.DataFrame(defaults)
    df = df.copy()
    for col, default in defaults.items():
        if col not in df.columns:
            df[col] = default
    return df


def _safe_sort(df: pd.DataFrame, by: List[str], ascending=True) -> pd.DataFrame:
    """Sort only after missing sort columns are created with neutral defaults."""
    if df is None or df.empty:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    df = df.copy()
    for col in by:
        if col not in df.columns:
            if "優先級" in col:
                df[col] = 99
            elif "時間" in col:
                df[col] = ""
            else:
                df[col] = 0
    return df.sort_values(by, ascending=ascending)


def _v212_style_signal(v: Any) -> str:
    text = str(v)
    if "✅" in text:
        return "background-color: #e8f7ee; color: #166534; font-weight: 700;"
    if "⏳" in text or "👀" in text:
        return "background-color: #fff7ed; color: #9a3412; font-weight: 650;"
    if "🔴" in text or "⚫" in text:
        return "background-color: #fee2e2; color: #991b1b; font-weight: 650;"
    if "🟢" in text:
        return "background-color: #ecfdf5; color: #047857;"
    return ""




# ---------- v2.13 / v2.14 Permanent Journal + Auto Weight Engine ----------

V213_SIGNAL_JOURNAL_PATH = DATA_DIR / "v213_signal_journal.csv"
V214_WEIGHT_PROFILE_PATH = DATA_DIR / "v214_weight_profile.json"
V215_VERIFIED_JOURNAL_PATH = DATA_DIR / "v215_verified_signal_journal.csv"
V215_SYNC_LOG_PATH = DATA_DIR / "v215_google_sheet_sync_log.csv"
V215_CONFIG_PATH = DATA_DIR / "v215_google_sheet_config.json"


def _v213_today() -> str:
    return now_taipei().strftime("%Y-%m-%d")


def _v213_now_str() -> str:
    return now_taipei().strftime("%H:%M:%S")


def _v213_scalar(value: Any) -> Any:
    """Return a CSV-safe scalar so Pandas assignment never crashes."""
    try:
        if isinstance(value, pd.DataFrame):
            if value.empty:
                return ""
            return value.to_json(force_ascii=False)
        if isinstance(value, pd.Series):
            if value.empty:
                return ""
            value = value.dropna().iloc[0] if not value.dropna().empty else ""
        if isinstance(value, np.ndarray):
            arr = value.flatten()
            if arr.size == 0:
                return ""
            if arr.size == 1:
                value = arr[0]
            else:
                return " → ".join([_safe_text(x) for x in arr[:20]])
        if isinstance(value, (list, tuple, set)):
            return " → ".join([_safe_text(x) for x in list(value)[:20]])
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        if value is None:
            return ""
        try:
            if pd.isna(value):
                return ""
        except Exception:
            pass
        if isinstance(value, (np.integer,)):
            return int(value)
        if isinstance(value, (np.floating,)):
            v = float(value)
            return "" if math.isnan(v) else v
        if isinstance(value, (pd.Timestamp,)):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value
    except Exception:
        return _safe_text(value, "")


def _v213_make_object_df(df: pd.DataFrame) -> pd.DataFrame:
    """Pandas 3+ may reject writing text into numeric columns. Force object dtype before journal updates."""
    try:
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.loc[:, ~df.columns.duplicated()].copy()
        return df.astype(object)
    except Exception:
        try:
            return df.copy()
        except Exception:
            return pd.DataFrame()


def _v213_safe_set(df: pd.DataFrame, idx: Any, col: str, val: Any) -> pd.DataFrame:
    """Set one cell without letting dtype/list-like values crash the Streamlit page."""
    try:
        scalar = _v213_scalar(val)
        if col not in df.columns:
            df[col] = pd.Series([""] * len(df), index=df.index, dtype=object)
        else:
            try:
                df[col] = df[col].astype(object)
            except Exception:
                pass
        if idx not in df.index:
            df.loc[idx, col] = ""
        df.at[idx, col] = scalar
    except Exception:
        try:
            df.loc[idx, col] = _safe_text(val, "")
        except Exception:
            pass
    return df

def _v213_load_journal() -> pd.DataFrame:
    if V213_SIGNAL_JOURNAL_PATH.exists():
        try:
            df = pd.read_csv(V213_SIGNAL_JOURNAL_PATH, dtype={"代號": str})
            if "代號" in df.columns:
                df["代號"] = df["代號"].astype(str).str.zfill(4)
            return _v213_make_object_df(df)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def _v213_save_journal(df: pd.DataFrame) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if df is None:
            df = pd.DataFrame()
        df = df.copy()
        if "代號" in df.columns:
            df["代號"] = df["代號"].astype(str).str.zfill(4)
        df.to_csv(V213_SIGNAL_JOURNAL_PATH, index=False, encoding="utf-8-sig")
    except Exception:
        pass


def clear_v213_signal_journal() -> None:
    try:
        if V213_SIGNAL_JOURNAL_PATH.exists():
            V213_SIGNAL_JOURNAL_PATH.unlink()
    except Exception:
        pass


def _v213_success_label(current_ret: float, high_ret: float, drawdown: float, stage: str) -> str:
    if "取消" in stage or "跌破" in stage or drawdown <= -2.0:
        return "❌ 失敗 / 破防"
    if high_ret >= 2.0 and drawdown > -1.5:
        return "✅ 有效 / 最高報酬達標"
    if current_ret >= 1.0 and drawdown > -1.2:
        return "✅ 有效 / 目前仍正"
    if high_ret >= 1.0 and current_ret < 0.2:
        return "⚠️ 衝高回落"
    if "錯過" in stage or "不追" in stage:
        return "🟡 避開追高"
    return "⏳ 追蹤中"


def _v213_classify_trade_type(row: pd.Series) -> str:
    code = _safe_text(row.get("代號"), "").zfill(4)
    pct = _clean_number(row.get("盤中漲跌幅"))
    limit_score = _clean_number(row.get("v29漲停前兆分") or row.get("漲停前兆分"))
    name = _safe_text(row.get("名稱"), "")
    if code in {"3441", "3362"} or pct >= 6 or limit_score >= 70:
        return "小型強攻 / 漲停前兆"
    if code in {"2382", "2313", "2330"}:
        return "中大型資金股"
    if "ETF" in name or code.startswith("00"):
        return "ETF / 市場動能"
    return _safe_text(row.get("交易型態"), "一般候選")


def update_v213_signal_journal(lifecycle_df: pd.DataFrame) -> pd.DataFrame:
    """
    v2.13: One row per stock per day, stored in data/v213_signal_journal.csv.
    This is a robust local journal. On Streamlit Cloud it persists during the app runtime;
    true cross-redeploy permanence still needs GitHub/DB write-back.
    """
    today = _v213_today()
    now_s = _v213_now_str()
    log_df = _v213_load_journal()
    if log_df is None or log_df.empty:
        log_df = pd.DataFrame()
    else:
        log_df = _v213_make_object_df(log_df)
        if "代號" in log_df.columns:
            log_df["代號"] = log_df["代號"].astype(str).str.zfill(4)

    if lifecycle_df is None or lifecycle_df.empty or "代號" not in lifecycle_df.columns:
        return log_df

    rows = lifecycle_df.copy()
    rows["代號"] = rows["代號"].astype(str).str.zfill(4)

    # Do not journal rows without any quote/decision information.
    if "盤中現價" in rows.columns:
        rows = rows[pd.to_numeric(rows["盤中現價"], errors="coerce").notna()].copy()
    if rows.empty:
        return log_df

    # Existing index by date-code.
    if "日期" not in log_df.columns:
        log_df["日期"] = ""
    if "代號" not in log_df.columns:
        log_df["代號"] = ""

    for _, row in rows.iterrows():
        code = _safe_text(row.get("代號"), "").zfill(4)
        if not code or code == "0000":
            continue
        stage = _safe_text(row.get("v212生命週期狀態"), "⚪ 候選觀察")
        decision = _safe_text(row.get("v212目前決策"), "等待")
        signal = _safe_text(row.get("交易員訊號"), stage)
        px = _clean_number(row.get("盤中現價"))
        if px <= 0:
            continue

        mask = (log_df.get("日期", pd.Series(dtype=str)).astype(str) == today) & (log_df.get("代號", pd.Series(dtype=str)).astype(str).str.zfill(4) == code)
        match_idx = list(log_df.index[mask]) if len(log_df) else []

        if match_idx:
            idx = match_idx[-1]
            first_price = _clean_number(log_df.at[idx, "首次價格"] if "首次價格" in log_df.columns else px) or px
            high_ret_old = _clean_number(log_df.at[idx, "最高報酬%"] if "最高報酬%" in log_df.columns else 0)
            drawdown_old = _clean_number(log_df.at[idx, "最大回撤%"] if "最大回撤%" in log_df.columns else 0)
            prev_stage = _safe_text(log_df.at[idx, "目前狀態"] if "目前狀態" in log_df.columns else "")
            prev_hist = _safe_text(log_df.at[idx, "狀態歷程"] if "狀態歷程" in log_df.columns else "")
            change_count = int(_clean_number(log_df.at[idx, "狀態變更次數"] if "狀態變更次數" in log_df.columns else 0))
        else:
            idx = len(log_df)
            first_price = px
            high_ret_old = 0.0
            drawdown_old = 0.0
            prev_stage = ""
            prev_hist = ""
            change_count = 0
            # Create a blank row first so .at can address it safely.
            log_df = _v213_safe_set(log_df, idx, "日期", today)
            log_df = _v213_safe_set(log_df, idx, "代號", code)
            log_df = _v213_safe_set(log_df, idx, "首次時間", now_s)
            log_df = _v213_safe_set(log_df, idx, "首次價格", round(px, 4))

        current_ret = (px - first_price) / first_price * 100 if first_price > 0 else 0.0
        high_ret = max(high_ret_old, current_ret)
        drawdown = min(drawdown_old, current_ret)
        if stage != prev_stage:
            change_count += 1
            hist_piece = f"{now_s} {stage}"
            hist = hist_piece if not prev_hist else f"{prev_hist} → {hist_piece}"
        else:
            hist = prev_hist or f"{now_s} {stage}"

        stop = _clean_number(row.get("防守停損"))
        hit_stop = bool(stop > 0 and px <= stop)
        near_limit = bool(_clean_number(row.get("盤中漲跌幅")) >= 8.5 or _clean_number(row.get("漲停距離%")) <= 1.5)
        result = _v213_success_label(current_ret, high_ret, drawdown, stage)

        updates = {
            "日期": today,
            "代號": code,
            "名稱": row.get("名稱", ""),
            "市場": row.get("市場", ""),
            "產業": row.get("產業", ""),
            "股票型態": _v213_classify_trade_type(row),
            "目前狀態": stage,
            "目前決策": decision,
            "交易員訊號": signal,
            "第一買點": row.get("第一買點", ""),
            "防守停損": row.get("防守停損", ""),
            "右側加碼價": row.get("右側加碼價", ""),
            "追價上限": row.get("追價上限", ""),
            "首次價格": round(first_price, 4),
            "目前價格": round(px, 4),
            "目前報酬%": round(current_ret, 3),
            "最高報酬%": round(high_ret, 3),
            "最大回撤%": round(drawdown, 3),
            "是否碰停損": "是" if hit_stop else "否",
            "是否接近漲停": "是" if near_limit else "否",
            "結果分類": result,
            "左側低吸分": round(_clean_number(row.get("左側低吸分")), 2),
            "盤中資金分": round(_clean_number(row.get("盤中資金分")), 2),
            "漲停前兆分": round(_clean_number(row.get("v29漲停前兆分") or row.get("漲停前兆分")), 2),
            "即時入場分": round(_clean_number(row.get("即時入場分")), 2),
            "AI總分": round(_clean_number(row.get("AI總分")), 2),
            "風險分": round(_clean_number(row.get("風險分")), 2),
            "盤中漲跌幅": round(_clean_number(row.get("盤中漲跌幅")), 3),
            "刷新漲速%": round(_clean_number(row.get("刷新漲速%")), 3),
            "狀態歷程": hist[-900:],
            "狀態變更次數": change_count,
            "最新時間": now_s,
            "下一步": row.get("v212下一步", ""),
            "不能買原因": row.get("不能買原因", ""),
        }
        if "首次時間" not in log_df.columns or not _safe_text(log_df.at[idx, "首次時間"] if idx in log_df.index and "首次時間" in log_df.columns else ""):
            updates["首次時間"] = now_s
        for col, val in updates.items():
            log_df = _v213_safe_set(log_df, idx, col, val)

    # Keep file compact: current day plus recent prior records if any.
    try:
        if "日期" in log_df.columns:
            log_df = log_df.sort_values(["日期", "最新時間"], ascending=[False, False]).head(2000).copy()
    except Exception:
        pass
    _v213_save_journal(log_df)
    return log_df


def build_v214_weight_profile(journal_df: pd.DataFrame) -> Dict[str, Any]:
    """v2.14 conservative auto-weight suggestions based on the local journal."""
    base = {
        "sample_size": 0,
        "success_rate": 0.0,
        "left_weight": 1.0,
        "money_weight": 1.0,
        "limit_weight": 1.0,
        "ai_weight": 1.0,
        "risk_penalty": 1.0,
        "confidence": "資料不足，使用保守權重",
        "note": "至少累積 30 筆以上才會明顯調整。",
    }
    try:
        if journal_df is None or journal_df.empty:
            return base
        df = journal_df.copy()
        for col in ["左側低吸分", "盤中資金分", "漲停前兆分", "AI總分", "風險分", "最高報酬%", "最大回撤%", "目前報酬%"]:
            if col not in df.columns:
                df[col] = 0
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        df["成功"] = ((df["最高報酬%"] >= 1.2) & (df["最大回撤%"] > -1.8)) | (df["目前報酬%"] >= 0.8)
        df["失敗"] = (df["最大回撤%"] <= -2.0) | (df.get("是否碰停損", "否").astype(str) == "是")
        actionable = df[df.get("目前狀態", "").astype(str).str.contains("可試單|到價確認|前兆|候選升溫", regex=True, na=False)].copy()
        if actionable.empty:
            actionable = df.copy()
        n = int(len(actionable))
        sr = float(actionable["成功"].mean() * 100) if n else 0.0
        profile = base.copy()
        profile["sample_size"] = n
        profile["success_rate"] = round(sr, 1)
        if n < 30:
            profile["confidence"] = f"樣本 {n} 筆，先保守，不大幅調權"
            return profile

        def factor_for(col: str, inverse: bool = False) -> float:
            med = actionable[col].median()
            hi = actionable[actionable[col] >= med]
            lo = actionable[actionable[col] < med]
            if len(hi) < 8 or len(lo) < 8:
                return 1.0
            hi_sr = hi["成功"].mean()
            lo_sr = lo["成功"].mean()
            diff = (hi_sr - lo_sr)
            if inverse:
                diff = -diff
            # Conservative cap to avoid overfitting.
            if diff > 0.18:
                return 1.15
            if diff > 0.08:
                return 1.08
            if diff < -0.18:
                return 0.85
            if diff < -0.08:
                return 0.92
            return 1.0

        profile["left_weight"] = factor_for("左側低吸分")
        profile["money_weight"] = factor_for("盤中資金分")
        profile["limit_weight"] = factor_for("漲停前兆分")
        profile["ai_weight"] = factor_for("AI總分")
        profile["risk_penalty"] = factor_for("風險分", inverse=True)
        profile["confidence"] = f"樣本 {n} 筆，已啟用保守自動調權"
        profile["note"] = "權重上限僅 ±15%，避免少量樣本過度擬合。"
        try:
            V214_WEIGHT_PROFILE_PATH.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return profile
    except Exception as e:
        p = base.copy()
        p["note"] = f"調權防呆：{type(e).__name__}"
        return p


def apply_v214_auto_weights(lifecycle_df: pd.DataFrame, profile: Dict[str, Any]) -> pd.DataFrame:
    df = lifecycle_df.copy()
    if df.empty:
        return df
    lw = float(profile.get("left_weight", 1.0) or 1.0)
    mw = float(profile.get("money_weight", 1.0) or 1.0)
    limw = float(profile.get("limit_weight", 1.0) or 1.0)
    aiw = float(profile.get("ai_weight", 1.0) or 1.0)
    rpw = float(profile.get("risk_penalty", 1.0) or 1.0)

    left = pd.to_numeric(df.get("左側低吸分", 0), errors="coerce").fillna(0)
    money = pd.to_numeric(df.get("盤中資金分", 0), errors="coerce").fillna(0)
    limit_s = pd.to_numeric(df.get("v29漲停前兆分", df.get("漲停前兆分", 0)), errors="coerce").fillna(0)
    ai = pd.to_numeric(df.get("AI總分", 0), errors="coerce").fillna(0)
    risk = pd.to_numeric(df.get("風險分", 0), errors="coerce").fillna(0)
    stop = pd.to_numeric(df.get("防守停損", 0), errors="coerce").fillna(0)
    px = pd.to_numeric(df.get("盤中現價", 0), errors="coerce").fillna(0)
    stop_dist = np.where((px > 0) & (stop > 0), (px - stop) / px * 100, np.nan)

    score = (
        left * 0.34 * lw +
        money * 0.30 * mw +
        limit_s * 0.18 * limw +
        ai * 0.18 * aiw -
        risk * 0.22 * rpw
    )
    score = np.clip(score, 0, 100)
    df["v214調權後分"] = np.round(score, 1)
    df["v214停損距離%"] = np.round(stop_dist, 2)

    stage = df.get("v212生命週期狀態", pd.Series(index=df.index, dtype=str)).astype(str)
    no_buy_reason = df.get("不能買原因", pd.Series(index=df.index, dtype=str)).astype(str)
    pct = pd.to_numeric(df.get("盤中漲跌幅", 0), errors="coerce").fillna(0)

    conditions_high = (
        stage.str.contains("可試單|到價確認", regex=True, na=False) &
        (df["v214調權後分"] >= 72) &
        (risk < 45) &
        ((df["v214停損距離%"].isna()) | (df["v214停損距離%"] <= 1.8)) &
        (pct < 7.0)
    )
    conditions_try = (
        stage.str.contains("可試單|到價確認", regex=True, na=False) &
        (df["v214調權後分"] >= 62) &
        (risk < 55) &
        (pct < 8.0)
    )
    wait = stage.str.contains("前兆|到價等確認|候選升溫", regex=True, na=False)
    no_chase = stage.str.contains("錯過|不買|取消|跌破", regex=True, na=False) | no_buy_reason.str.contains("追|風險|破", regex=True, na=False)

    df["v214信心閘門"] = "⚪ 觀察"
    df.loc[wait, "v214信心閘門"] = "👀 等確認"
    df.loc[conditions_try, "v214信心閘門"] = "🟡 嚴格小量"
    df.loc[conditions_high, "v214信心閘門"] = "🟢 高信心小量"
    df.loc[no_chase, "v214信心閘門"] = "🔴 不可無腦"

    df["v214下一步"] = np.select(
        [conditions_high, conditions_try, wait, no_chase],
        [
            "只允許小量；防守價跌破立即退。不是無腦重倉。",
            "可小量但還要看下一輪是否守住；不加碼。",
            "還缺止跌/量能/位置確認；不要提前買。",
            "不追、不攤平；等重新形成左側結構。",
        ],
        default="觀察，不做交易。",
    )
    # Use v2.14 score as secondary sorting only; lifecycle priority remains first.
    old_sort = pd.to_numeric(df.get("v212排序分", 0), errors="coerce").fillna(0)
    df["v214優先排序分"] = old_sort * 0.45 + df["v214調權後分"] * 0.55
    df["v212排序分"] = df["v214優先排序分"]
    return df


def build_v213_journal_summary(journal_df: pd.DataFrame) -> Dict[str, Any]:
    out = {"total": 0, "today": 0, "success_rate": 0.0, "false_break": 0, "high_conf": 0}
    try:
        if journal_df is None or journal_df.empty:
            return out
        df = journal_df.copy()
        today = _v213_today()
        out["total"] = int(len(df))
        out["today"] = int((df.get("日期", "").astype(str) == today).sum()) if "日期" in df.columns else 0
        result = df.get("結果分類", pd.Series(dtype=str)).astype(str)
        actionable = df.get("目前狀態", pd.Series(dtype=str)).astype(str).str.contains("可試單|到價確認|前兆|候選升溫", regex=True, na=False)
        denom = max(int(actionable.sum()), 1)
        out["success_rate"] = round(float(result[actionable].str.contains("✅", regex=False).sum()) / denom * 100, 1)
        out["false_break"] = int(result.str.contains("衝高回落|失敗", regex=True).sum())
        out["high_conf"] = int(df.get("目前狀態", pd.Series(dtype=str)).astype(str).str.contains("可試單|到價確認", regex=True, na=False).sum())
    except Exception:
        pass
    return out


def _v215_secret_value(*keys: str, default: str = "") -> str:
    """Read optional Streamlit secrets without crashing when secrets are absent."""
    try:
        for key in keys:
            try:
                val = st.secrets.get(key)
                if val:
                    return str(val).strip()
            except Exception:
                pass
            try:
                if "." in key:
                    cur = st.secrets
                    for part in key.split("."):
                        cur = cur[part]
                    if cur:
                        return str(cur).strip()
            except Exception:
                pass
    except Exception:
        pass
    return default


def _v215_json_safe(value: Any) -> Any:
    value = _v213_scalar(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        if not np.isfinite(value):
            return None
        return float(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if value is None:
        return None
    return str(value) if not isinstance(value, (int, float, bool, list, dict)) else value


def _v215_row_key(date_s: str, code: str) -> str:
    return f"{str(date_s)}_{str(code).zfill(4)}"


def build_v215_postclose_verification(journal_df: pd.DataFrame, live_df: pd.DataFrame) -> pd.DataFrame:
    """v2.15: merge local signal journal with current/closing-like quotes for post-close verification.

    During the trading session this is a provisional verification; after 13:30 Taipei
    time it becomes a close-like verification based on the latest quote available.
    """
    try:
        if journal_df is None or journal_df.empty:
            return pd.DataFrame()
        out = journal_df.copy()
        out["代號"] = out.get("代號", "").astype(str).str.replace(".0", "", regex=False).str.zfill(4)
        today = _v213_today()
        now_dt = now_taipei()
        close_like = (now_dt.hour, now_dt.minute) >= (13, 30)
        status_text = "收盤近似驗證" if close_like else "盤中暫估驗證"

        live_cols = [c for c in ["代號", "盤中現價", "報價時間", "最高", "最低", "盤中漲跌幅", "刷新漲速%", "v212生命週期狀態", "v214信心閘門"] if c in live_df.columns]
        if live_cols:
            q = live_df[live_cols].copy()
            q["代號"] = q["代號"].astype(str).str.replace(".0", "", regex=False).str.zfill(4)
            q = q.drop_duplicates("代號", keep="last")
            out = out.merge(q, on="代號", how="left", suffixes=("", "_驗證"))

        first_price = pd.to_numeric(out.get("首次價格"), errors="coerce")
        cur_price = pd.to_numeric(out.get("盤中現價"), errors="coerce").combine_first(pd.to_numeric(out.get("目前價格"), errors="coerce"))
        high_ret_old = pd.to_numeric(out.get("最高報酬%"), errors="coerce").fillna(0)
        low_dd_old = pd.to_numeric(out.get("最大回撤%"), errors="coerce").fillna(0)
        verify_ret = np.where(first_price > 0, (cur_price - first_price) / first_price * 100, np.nan)
        verify_ret_s = pd.Series(verify_ret, index=out.index)
        out["驗證Key"] = [_v215_row_key(d, c) for d, c in zip(out.get("日期", today).astype(str), out["代號"].astype(str))]
        out["驗證狀態"] = np.where(out.get("日期", "").astype(str) == today, status_text, out.get("驗證狀態", "待後續驗證"))
        out["驗證時間"] = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        out["驗證價格"] = np.round(cur_price, 2)
        out["驗證報酬%"] = np.round(verify_ret_s, 2)
        out["驗證最高報酬%"] = np.round(np.maximum(high_ret_old, verify_ret_s.fillna(0)), 2)
        out["驗證最大回撤%"] = np.round(np.minimum(low_dd_old, verify_ret_s.fillna(0)), 2)

        stage = out.get("目前狀態", pd.Series(index=out.index, dtype=str)).astype(str)
        stop_hit = out.get("是否碰停損", pd.Series(False, index=out.index)).astype(str).str.contains("True|1|是", regex=True, na=False)
        near_limit = out.get("是否接近漲停", pd.Series(False, index=out.index)).astype(str).str.contains("True|1|是", regex=True, na=False)
        result = []
        for i in out.index:
            r = float(verify_ret_s.loc[i]) if pd.notna(verify_ret_s.loc[i]) else 0.0
            stg = str(stage.loc[i])
            if stop_hit.loc[i]:
                result.append("❌ 觸停損")
            elif near_limit.loc[i] or r >= 6.5:
                result.append("🚀 接近漲停/大漲")
            elif r >= 2.0:
                result.append("✅ 有效上漲")
            elif r >= 0.5:
                result.append("🟢 小幅有效")
            elif r <= -2.0 and re.search("可試單|到價確認|高信心|嚴格小量", stg):
                result.append("❌ 試單失敗")
            elif r <= -1.0:
                result.append("⚠️ 偏弱")
            else:
                result.append("⏳ 待觀察")
        out["盤後驗證結果"] = result
        out = out.drop_duplicates("驗證Key", keep="last")
        return out
    except Exception as e:
        tmp = journal_df.copy() if isinstance(journal_df, pd.DataFrame) else pd.DataFrame()
        tmp["驗證狀態"] = f"驗證防呆：{type(e).__name__}"
        return tmp


def _v215_save_verified_journal(df: pd.DataFrame) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if df is None or df.empty:
            return
        df.to_csv(V215_VERIFIED_JOURNAL_PATH, index=False, encoding="utf-8-sig")
    except Exception:
        pass


def build_v215_stats(verified_df: pd.DataFrame) -> Dict[str, Any]:
    out = {"samples": 0, "verified": 0, "win_rate": 0.0, "avg_ret": 0.0, "best_type": "樣本不足", "weak_type": "樣本不足", "near_limit": 0}
    try:
        if verified_df is None or verified_df.empty:
            return out
        df = verified_df.copy()
        out["samples"] = int(len(df))
        res = df.get("盤後驗證結果", pd.Series(dtype=str)).astype(str)
        ret = pd.to_numeric(df.get("驗證報酬%"), errors="coerce")
        valid = ret.notna()
        out["verified"] = int(valid.sum())
        if valid.any():
            out["avg_ret"] = round(float(ret[valid].mean()), 2)
            # v2.15.6: Google Sheet / journal labels are not always emoji-prefixed.
            # Count practical successful outcomes by text label + return threshold, not only ✅ emoji.
            success_label = res.str.contains("有效上漲|小幅有效|接近漲停|大漲|避開成功|成功|高信心", regex=True, na=False)
            failure_label = res.str.contains("假突破|衝高回落|跌破停損|試單失敗|偏弱|失敗", regex=True, na=False)
            win = success_label | ((ret >= 0.5) & (~failure_label))
            out["win_rate"] = round(float(win[valid].mean() * 100), 1)
        out["near_limit"] = int(res.str.contains("🚀|漲停|大漲", regex=True, na=False).sum())
        if "股票型態" in df.columns and valid.any():
            g = df.loc[valid].assign(_ret=ret[valid]).groupby("股票型態")['_ret'].agg(['count','mean']).reset_index()
            g2 = g[g['count'] >= 2] if len(g) else g
            if not g2.empty:
                best = g2.sort_values('mean', ascending=False).iloc[0]
                weak = g2.sort_values('mean', ascending=True).iloc[0]
                out["best_type"] = f"{best['股票型態']} / {best['mean']:.2f}%"
                out["weak_type"] = f"{weak['股票型態']} / {weak['mean']:.2f}%"
    except Exception:
        pass
    return out


def _v215_sync_log(status: str, rows: int, message: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        item = pd.DataFrame([{
            "時間": now_taipei().strftime("%Y-%m-%d %H:%M:%S"),
            "狀態": status,
            "筆數": rows,
            "訊息": message[:500],
        }])
        if V215_SYNC_LOG_PATH.exists():
            old = pd.read_csv(V215_SYNC_LOG_PATH)
            item = pd.concat([old, item], ignore_index=True)
        item.tail(200).to_csv(V215_SYNC_LOG_PATH, index=False, encoding="utf-8-sig")
    except Exception:
        pass


def push_v215_to_google_sheet(verified_df: pd.DataFrame, webhook_url: str, max_rows: int = 500, chunk_size: int = 25) -> Tuple[bool, str]:
    """Push verified journal to Google Sheet in small chunks.

    v2.15.3 fix:
    - Google Apps Script / Google Sheet can timeout when one POST contains 100+ rows.
    - Split records into smaller chunks.
    - Log actual attempted row count, not 0, when timeout happens.
    - Keep the app alive even when one chunk fails.
    """
    if verified_df is None or verified_df.empty:
        _v215_sync_log("略過", 0, "沒有可同步的紀錄")
        return False, "沒有可同步的紀錄"
    webhook_url = str(webhook_url or "").strip()
    if not webhook_url.startswith("http"):
        _v215_sync_log("失敗", 0, "尚未設定 Google Sheet Webhook URL")
        return False, "尚未設定 Google Sheet Webhook URL"

    try:
        chunk_size = max(5, min(int(chunk_size or 25), 50))
    except Exception:
        chunk_size = 25

    try:
        df = verified_df.tail(max_rows).copy()
        # Make all columns safe before JSON serialization.
        for c in df.columns:
            try:
                df[c] = df[c].map(_v215_json_safe)
            except Exception:
                df[c] = df[c].astype(str)

        all_rows = []
        for rec in df.to_dict(orient="records"):
            all_rows.append({str(k): _v215_json_safe(v) for k, v in rec.items()})

        if not all_rows:
            _v215_sync_log("略過", 0, "沒有可同步的有效紀錄")
            return False, "沒有可同步的有效紀錄"

        total_ok_rows = 0
        messages = []
        total_chunks = (len(all_rows) + chunk_size - 1) // chunk_size

        for i in range(0, len(all_rows), chunk_size):
            chunk = all_rows[i:i + chunk_size]
            chunk_no = i // chunk_size + 1
            payload = {
                "source": "TW_Stock_AI_Scanner",
                "version": "v2.15.6",
                "sent_at": now_taipei().strftime("%Y-%m-%d %H:%M:%S"),
                "chunk_no": chunk_no,
                "total_chunks": total_chunks,
                "rows": chunk,
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=data,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=35) as resp:
                    body = resp.read().decode("utf-8", errors="ignore")
                    status_code = int(getattr(resp, "status", 0) or 0)
                    ok = 200 <= status_code < 300
                if ok:
                    total_ok_rows += len(chunk)
                messages.append(f"第{chunk_no}/{total_chunks}批 HTTP {status_code}｜{body[:160]}")
            except Exception as e:
                # Stop on timeout/failure to avoid repeated hammering, but keep detailed log.
                err_msg = f"第{chunk_no}/{total_chunks}批失敗｜{type(e).__name__}: {e}"
                messages.append(err_msg)
                final_msg = "；".join(messages)[-900:]
                _v215_sync_log("部分成功" if total_ok_rows else "失敗", total_ok_rows, final_msg)
                return False, final_msg

        final_msg = "；".join(messages)[-900:]
        _v215_sync_log("成功", total_ok_rows, final_msg)
        return True, final_msg
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        # Try to log the intended size when possible.
        try:
            intended = min(len(verified_df), max_rows)
        except Exception:
            intended = 0
        _v215_sync_log("失敗", intended, msg)
        return False, msg

def load_v215_sync_log() -> pd.DataFrame:
    try:
        if V215_SYNC_LOG_PATH.exists():
            return pd.read_csv(V215_SYNC_LOG_PATH)
    except Exception:
        pass
    return pd.DataFrame()


def load_v215_gsheet_config() -> Dict[str, Any]:
    """Load Google Sheet sync settings from local data folder.

    Streamlit query/session state can reset on a full browser reload.
    This tiny config file keeps the webhook URL and auto-sync setting stable
    during the app instance lifetime without putting the webhook into the URL.
    """
    try:
        if V215_CONFIG_PATH.exists():
            data = json.loads(V215_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_v215_gsheet_config(webhook_url: str = "", enable: bool = False, auto_sync: bool = False) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "webhook_url": str(webhook_url or "").strip(),
            "enable": bool(enable),
            "auto_sync": bool(auto_sync),
            "updated_at": now_taipei().strftime("%Y-%m-%d %H:%M:%S"),
        }
        V215_CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def clear_v215_gsheet_config() -> None:
    try:
        if V215_CONFIG_PATH.exists():
            V215_CONFIG_PATH.unlink()
    except Exception:
        pass


def latest_v215_sync_status() -> Dict[str, Any]:
    df = load_v215_sync_log()
    if df.empty:
        return {"status": "尚未同步", "time": "-", "rows": 0, "message": "尚未送出 Google Sheet 同步"}
    try:
        row = df.tail(1).iloc[0].to_dict()
        return {
            "status": str(row.get("狀態", row.get("status", "-"))),
            "time": str(row.get("時間", row.get("time", "-"))),
            "rows": row.get("筆數", row.get("rows", 0)),
            "message": str(row.get("訊息", row.get("message", ""))),
        }
    except Exception:
        return {"status": "讀取失敗", "time": "-", "rows": 0, "message": "同步紀錄讀取失敗"}


st.title("🧬 盤中即時看盤 v2.15.6 真永久學習資料庫｜學習勝率修正版")
st.caption("v2.15.6 修正：Google Sheet 同步維持批次寫入；學習勝率改用盤後驗證樣本，不再只看舊版本機 journal 的單一成功標籤。")

refresh_default = _get_query_int("refresh", 30, 15, 120, 15)
top_n_default = _get_query_int("top_n", 15, 5, 50, 5)
min_ai_default = _get_query_int("min_ai", 0, 0, 100, 5)
min_strength_default = _get_query_int("min_strength", 0, 0, 100, 5)
attack_default = _get_query_int("attack", 65, 50, 85, 5)
watch_default = _get_query_int("watch", 55, 40, 75, 5)
weak_default = _get_query_float("weak", -1.0, -5.0, 0.0, 0.5)
chase_default = _get_query_float("chase", 7.0, 3.0, 10.0, 0.5)
extra_codes_default = _get_query_value("extra_codes", "")
mode_default = _get_query_value("mode", "盤中市場池掃描")
pool_default = _get_query_int("pool_size", 300, 30, 600, 10)
view_default = _get_query_value("view", "精簡")

with st.sidebar:
    st.header("即時設定")
    view_mode = st.radio("畫面模式", ["精簡", "完整診斷"], index=1 if view_default == "完整診斷" else 0)
    scan_mode = st.radio(
        "即時掃描範圍",
        ["盤後AI候選", "盤中市場池掃描"],
        index=1 if mode_default == "盤中市場池掃描" else 0,
    )
    pool_size = st.slider("市場池檔數", min_value=30, max_value=600, value=pool_default, step=10, disabled=(scan_mode != "盤中市場池掃描"))
    refresh_seconds = st.slider("自動刷新秒數", min_value=15, max_value=120, value=refresh_default, step=15)
    top_n = st.slider("主表顯示前 N 檔", min_value=5, max_value=50, value=top_n_default, step=5)
    min_ai = st.slider("最低 AI / 市場池分", 0, 100, min_ai_default, 5)
    min_strength = st.slider("最低即時強度分", 0, 100, min_strength_default, 5)

    st.markdown("---")
    st.subheader("手動 / 核心監控")
    extra_codes_text = st.text_area("額外監控代碼", value=extra_codes_default, placeholder="例如：3441, 2382, 2313")
    manual_codes = parse_extra_codes(extra_codes_text)
    tracked_codes = _unique_keep_order(manual_codes + FOCUS_CODES)
    st.caption("固定追蹤：" + "、".join([f"{c} {FOCUS_LABELS.get(c, '')}".strip() for c in FOCUS_CODES]))
    if manual_codes:
        st.caption("你手動加入：" + "、".join(manual_codes))

    st.markdown("---")
    st.subheader("警示條件")
    attack_threshold = st.slider("強勢進攻門檻", 50, 85, attack_default, 5)
    watch_threshold = st.slider("觀察偏強門檻", 40, 75, watch_default, 5)
    weak_drop = st.slider("AI高分轉弱跌幅", -5.0, 0.0, weak_default, 0.5)
    chase_pct = st.slider("不要追高漲幅", 3.0, 10.0, chase_default, 0.5)

    st.markdown("---")
    if st.button("清除盤中記憶 / 學習暫存", type="secondary"):
        clear_intraday_memory()
        clear_runtime_signal_log()
        try:
            clear_v211_learning_log()
        except Exception:
            pass
        st.rerun()

    st.markdown("---")
    st.subheader("v2.15 永久學習")
    _saved_gsheet_cfg = load_v215_gsheet_config()
    default_webhook = _v215_secret_value("GSHEET_WEBHOOK_URL", "google_sheet.webhook_url", default="")
    if not default_webhook:
        default_webhook = str(_saved_gsheet_cfg.get("webhook_url", "") or "")
    default_enable = bool(_saved_gsheet_cfg.get("enable", bool(default_webhook)))
    default_auto_sync = bool(_saved_gsheet_cfg.get("auto_sync", False))

    v215_enable_gsheet = st.toggle("啟用 Google Sheet 同步", value=default_enable, key="v215_enable_gsheet_persist")
    v215_webhook_url = st.text_input(
        "Google Sheet Webhook URL",
        value=default_webhook,
        type="password",
        key="v215_webhook_url_persist",
        help="會保存到 data/v215_google_sheet_config.json；刷新頁面不會消失。也可放 Streamlit secrets：GSHEET_WEBHOOK_URL。",
    )
    v215_auto_sync = st.toggle(
        "每輪自動同步最近紀錄",
        value=default_auto_sync,
        key="v215_auto_sync_persist",
        help="開啟後會跟著自動刷新秒數同步；例如刷新 60 秒，約每 60 秒同步一次。",
    )

    # Auto-save the URL/toggles so full browser reloads do not wipe them.
    if str(v215_webhook_url or "").strip() or v215_enable_gsheet or v215_auto_sync:
        save_v215_gsheet_config(v215_webhook_url, v215_enable_gsheet, v215_auto_sync)

    st.caption(f"刷新頻率：約每 {refresh_seconds} 秒；自動同步：{'已開啟' if v215_auto_sync and v215_enable_gsheet else '未開啟'}")
    _sync_status_sidebar = latest_v215_sync_status()
    st.caption(f"最後同步：{_sync_status_sidebar.get('time', '-')}｜{_sync_status_sidebar.get('status', '-') }｜{_sync_status_sidebar.get('rows', 0)} 筆")
    if st.button("儲存 Google Sheet 設定", type="secondary", key="v215_save_gsheet_config_btn"):
        save_v215_gsheet_config(v215_webhook_url, v215_enable_gsheet, v215_auto_sync)
        st.success("已儲存 Google Sheet 同步設定。")
    if st.button("清除 Google Sheet 設定", type="secondary", key="v215_clear_gsheet_config_btn"):
        clear_v215_gsheet_config()
        st.success("已清除本機保存設定，請重新整理。")

_set_query_if_changed({
    "view": view_mode,
    "mode": scan_mode,
    "pool_size": pool_size,
    "refresh": refresh_seconds,
    "top_n": top_n,
    "min_ai": min_ai,
    "min_strength": min_strength,
    "attack": attack_threshold,
    "watch": watch_threshold,
    "weak": weak_drop,
    "chase": chase_pct,
    "extra_codes": extra_codes_text.strip(),
})

components.html(
    f"""
    <script>
      setTimeout(function() {{ window.parent.location.reload(); }}, {int(refresh_seconds) * 1000});
    </script>
    """,
    height=0,
)

rank_df = load_rank()
with st.spinner("建立盤中掃描清單並抓取即時報價..."):
    universe_df, universe_source = build_live_universe(rank_df, scan_mode, pool_size, tracked_codes)
    symbols = build_symbols(universe_df)
    quotes_df = fetch_twse_mis_quotes(symbols)

# v2.12.1: temporary quote outages should not make the whole page show 0/0.
# Prefer fresh MIS quotes; otherwise reuse the last in-session quote snapshot, then fall back to data/intraday_snapshot.csv if present.
quote_source_note = "MIS即時報價"
if quotes_df.empty or "盤中現價" not in quotes_df.columns or pd.to_numeric(quotes_df.get("盤中現價"), errors="coerce").notna().sum() == 0:
    fallback_df = st.session_state.get("v212_last_good_quotes_df")
    if isinstance(fallback_df, pd.DataFrame) and not fallback_df.empty:
        quotes_df = fallback_df.copy()
        quote_source_note = "沿用上一輪成功報價"
    else:
        snap_path = DATA_DIR / "intraday_snapshot.csv"
        if snap_path.exists():
            try:
                snap = pd.read_csv(snap_path)
                if "代號" in snap.columns:
                    snap["代號"] = snap["代號"].astype(str).str.replace(".0", "", regex=False).str.zfill(4)
                    quotes_df = snap.copy()
                    quote_source_note = "沿用 GitHub 盤中快照"
            except Exception:
                pass
else:
    st.session_state["v212_last_good_quotes_df"] = quotes_df.copy()

merged = universe_df.copy()
if quotes_df.empty:
    st.warning("目前沒有抓到盤中報價。可能是非交易時間、TWSE MIS 暫時無回應，或網路限制。")
    for col in ["盤中現價", "盤中漲跌幅", "盤中成交量", "報價時間", "報價市場", "昨收", "開盤", "最高", "最低"]:
        merged[col] = np.nan
else:
    merged = merged.merge(quotes_df, on="代號", how="left")
    if "即時名稱" in merged.columns:
        merged["名稱"] = merged["即時名稱"].fillna(merged["名稱"])
    if "報價市場" in merged.columns:
        merged["市場"] = merged["報價市場"].fillna(merged["市場"])
    if quote_source_note != "MIS即時報價":
        st.info(f"本輪 MIS 即時報價沒有成功，已{quote_source_note}，避免決策表歸零；等下一輪即時報價恢復會自動更新。")

# Core calculation chain retained, but UI no longer repeats every old section.
live_df = compute_live_strength(merged, attack_threshold, watch_threshold, weak_drop, chase_pct)
live_df = add_entry_timing(live_df, chase_pct=chase_pct)
live_df, surge_df, surge_has_prev = update_surge_radar(live_df)
live_df = add_decision_dashboard(live_df)
live_df = add_limitup_reattack_engine(live_df, chase_pct=chase_pct)
live_df = add_v281_three_zone_entry(live_df)
live_df = add_entry_signal_layer(live_df, chase_pct=chase_pct)
live_df = apply_v28_entry_signal_overrides(live_df)
live_df, intraday_memory_df = update_intraday_memory_features(live_df)
live_df = add_v29_left_predictive_ai(live_df, chase_pct=chase_pct)
live_df = add_v210_trader_decision(live_df, chase_pct=chase_pct)

# v2.11 stable learning + v2.12 lifecycle state machine.
v211_learning_log_df = update_v211_signal_learning(live_df)
v211_missed_limit_df = build_v211_missed_limit_report(live_df, v211_learning_log_df)
v211_summary = build_v211_learning_summary(v211_learning_log_df)
lifecycle_df = build_v212_lifecycle(live_df, v211_learning_log_df)
lifecycle_df = _ensure_columns(lifecycle_df, {
    "v212優先級": 99,
    "v212排序分": 0.0,
    "即時強度分": 0.0,
    "最新時間": "",
    "學習狀態": "",
    "v212生命週期狀態": "⚪ 候選觀察",
    "v212目前決策": "等待",
    "v212位置判斷": "未判斷",
    "v212下一步": "等待下一輪刷新。",
})

# v2.13 / v2.14: write a robust journal, then use it for conservative auto-weighting.
v213_signal_journal_df = update_v213_signal_journal(lifecycle_df)
v214_weight_profile = build_v214_weight_profile(v213_signal_journal_df)
lifecycle_df = apply_v214_auto_weights(lifecycle_df, v214_weight_profile)
v213_summary = build_v213_journal_summary(v213_signal_journal_df)
v215_verified_journal_df = build_v215_postclose_verification(v213_signal_journal_df, lifecycle_df)
_v215_save_verified_journal(v215_verified_journal_df)
v215_stats = build_v215_stats(v215_verified_journal_df)

if "v215_enable_gsheet" in globals() and v215_enable_gsheet and v215_auto_sync:
    # Auto-sync only the latest rows to reduce repeated traffic. The webhook should upsert by 驗證Key.
    try:
        if str(v215_webhook_url or "").strip():
            push_v215_to_google_sheet(v215_verified_journal_df.tail(60), v215_webhook_url, max_rows=60, chunk_size=20)
    except Exception:
        pass

if "手動加入" in lifecycle_df.columns:
    lifecycle_df["加入來源"] = np.where(lifecycle_df["手動加入"].astype(bool), "手動監控", "自動掃描")
else:
    lifecycle_df["加入來源"] = "自動掃描"

filtered = lifecycle_df[(lifecycle_df["AI總分"] >= min_ai) & (lifecycle_df["即時強度分"] >= min_strength)].copy()

# Minimal, non-redundant summary.
quote_ok = int(lifecycle_df["盤中現價"].notna().sum())
can_try = int(lifecycle_df.get("v212生命週期狀態", pd.Series(dtype=str)).astype(str).str.contains("可試單|到價確認", regex=True).sum())
arrived_wait = int(lifecycle_df.get("v212生命週期狀態", pd.Series(dtype=str)).astype(str).str.contains("到價等確認", regex=False).sum())
early_count = int(lifecycle_df.get("v212生命週期狀態", pd.Series(dtype=str)).astype(str).str.contains("前兆|候選升溫", regex=True).sum())
no_buy_count = int(lifecycle_df.get("v212生命週期狀態", pd.Series(dtype=str)).astype(str).str.contains("不買|錯過|取消", regex=True).sum())
surge_count = int(len(surge_df)) if "surge_df" in globals() else 0
best_pct = float(lifecycle_df["盤中漲跌幅"].max()) if len(lifecycle_df) else 0.0
max_speed = float(pd.to_numeric(lifecycle_df.get("刷新漲速%", 0), errors="coerce").fillna(0).max()) if len(lifecycle_df) else 0.0

m1, m2, m3, m4 = st.columns(4)
m1.metric("掃描 / 報價", f"{len(universe_df)} / {quote_ok}")
m2.metric("可試單 / 到價等確認", f"{can_try} / {arrived_wait}")
m3.metric("前兆 / 風險不買", f"{early_count} / {no_buy_count}")
m4.metric("最後刷新", now_taipei().strftime("%H:%M:%S"))

m5, m6, m7, m8 = st.columns(4)
m5.metric("盤中最強漲幅", f"{best_pct:.2f}%")
m6.metric("最高刷新漲速", f"{max_speed:.2f}%")
m7.metric("爆衝雷達", surge_count)
m8.metric("學習紀錄 / 驗證", f"{int(v213_summary.get("total", 0))} / {int(v215_stats.get("verified", 0))}")

st.divider()

st.subheader("🧬 v2.15.6 真永久學習資料庫 + 盤後驗證器｜學習勝率修正版")
st.caption("目前會先把驗證後訊號寫到 data/v215_verified_signal_journal.csv；若設定 Google Sheet Webhook，可手動或自動同步到 Google Sheet。v2.15.6 起，學習勝率以盤後驗證結果為主，避免顯示 0% 的舊版誤導。")
vm1, vm2, vm3, vm4 = st.columns(4)
vm1.metric("驗證樣本", int(v215_stats.get("verified", 0)))
vm2.metric("驗證勝率", f"{float(v215_stats.get('win_rate', 0)):.1f}%")
vm3.metric("平均驗證報酬", f"{float(v215_stats.get('avg_ret', 0)):.2f}%")
vm4.metric("接近漲停/大漲", int(v215_stats.get("near_limit", 0)))
vm5, vm6 = st.columns(2)
vm5.info(f"目前較有效型態：{_safe_text(v215_stats.get('best_type'), '樣本不足')}")
vm6.warning(f"目前較弱型態：{_safe_text(v215_stats.get('weak_type'), '樣本不足')}")

# v2.15.2: Sync status is always visible, so the user does not need to guess whether it is working.
st.markdown("#### 🔁 Google Sheet 同步狀態")
sync_status = latest_v215_sync_status()
sm1, sm2, sm3, sm4 = st.columns(4)
sm1.metric("同步開關", "開啟" if ('v215_enable_gsheet' in globals() and v215_enable_gsheet) else "關閉")
sm2.metric("自動同步", "開啟" if ('v215_auto_sync' in globals() and v215_auto_sync and v215_enable_gsheet) else "關閉")
sm3.metric("最後同步", _safe_text(sync_status.get("time"), "-"))
sm4.metric("最後筆數", sync_status.get("rows", 0))

if 'v215_enable_gsheet' in globals() and v215_enable_gsheet:
    c_sync, c_log = st.columns([1, 2])
    with c_sync:
        if st.button("立即同步到 Google Sheet", type="primary"):
            ok, msg = push_v215_to_google_sheet(v215_verified_journal_df, v215_webhook_url, max_rows=200, chunk_size=20)
            if ok:
                st.success("已送出 Google Sheet 同步。")
            else:
                st.error("同步失敗：" + msg)
    with c_log:
        sync_log = load_v215_sync_log()
        if not sync_log.empty:
            st.caption("最近同步紀錄")
            st.dataframe(sync_log.tail(5), use_container_width=True, hide_index=True)
        else:
            st.caption("尚未有同步紀錄。")
else:
    st.info("尚未啟用 Google Sheet 同步；目前仍會保留本機 CSV，並可在下方下載。")

# v2.14 compact weight-learning status.
st.subheader("🧠 v2.14 / v2.15 自動調權狀態")
wm1, wm2, wm3, wm4 = st.columns(4)
sample_size = int(v214_weight_profile.get("sample_size", 0))
verified_n = int(v215_stats.get("verified", 0))
learning_ready = "資料不足" if sample_size < 30 or verified_n < 30 else "可開始參考"
# v2.15.6: show the real post-close/verified win rate once verification samples are mature.
# The old v2.14 success_rate can stay 0 when local runtime fields are not populated, which misleads the user.
verified_win_rate = float(v215_stats.get('win_rate', 0) or 0)
local_win_rate = float(v214_weight_profile.get('success_rate', 0) or 0)
learning_win_rate_display = verified_win_rate if verified_n >= 30 else local_win_rate
wm1.metric("學習樣本", f"{sample_size} / 30")
wm2.metric("驗證樣本", f"{verified_n} / 30")
wm3.metric("學習勝率", f"{learning_win_rate_display:.1f}%")
wm4.metric("學習成熟度", learning_ready)
wm5, wm6 = st.columns(2)
wm5.metric("左側/資金權重", f"{float(v214_weight_profile.get('left_weight', 1)):.2f} / {float(v214_weight_profile.get('money_weight', 1)):.2f}")
wm6.metric("前兆/風險權重", f"{float(v214_weight_profile.get('limit_weight', 1)):.2f} / {float(v214_weight_profile.get('risk_penalty', 1)):.2f}")
st.caption(_safe_text(v214_weight_profile.get("confidence"), "") + "｜" + _safe_text(v214_weight_profile.get("note"), ""))
if verified_n >= 30:
    st.info("v2.15.6：學習勝率已改用盤後驗證樣本計算；有效上漲、小幅有效、接近漲停/大漲都會被納入，不再只看舊版 ✅ 標籤。")
else:
    st.warning("學習勝率不是每輪即時變動的『學習率』；要等盤後驗證樣本累積後才有意義。最高只會顯示 🟢 高信心小量，仍必須照防守停損執行。")

# 1) Primary current decision table.
st.subheader("🧭 v2.14 交易員目前決策")
st.caption("主表只保留會影響進場的欄位。v2.14 新增『信心閘門』與『調權後分』；🟢 高信心小量仍不是無腦重倉，只是更嚴格的試單條件。")
main_cols = _cols_exist(lifecycle_df, [
    "代號", "名稱", "市場", "產業", "交易型態", "v214信心閘門", "v212生命週期狀態", "v212目前決策", "我會不會買",
    "第一買點", "盤中現價", "v214停損距離%", "v212位置判斷", "防守停損", "右側加碼價", "追價上限",
    "盤中漲跌幅", "刷新漲速%", "v214調權後分", "左側低吸分", "盤中資金分", "v29漲停前兆分", "v210決策分",
    "v214下一步", "v212下一步", "還缺什麼確認", "不能買原因", "資料來源", "AI來源", "報價時間"
])
main_df = _safe_sort(filtered, ["v212優先級", "v212排序分", "即時強度分"], ascending=[True, False, False]).head(top_n)
if main_df.empty:
    st.info("目前沒有符合篩選條件的股票。可以降低左側篩選的 AI / 即時強度門檻，或等待下一輪刷新。")
else:
    try:
        st.dataframe(main_df[main_cols].style.applymap(_v212_style_signal, subset=["v214信心閘門", "v212生命週期狀態"]), use_container_width=True, hide_index=True)
    except Exception:
        st.dataframe(main_df[main_cols], use_container_width=True, hide_index=True)

# 2) Focus names.
st.subheader("🎯 核心追蹤：聯一光 / 廣達 / 華通")
focus_df = lifecycle_df[lifecycle_df["代號"].astype(str).str.zfill(4).isin(FOCUS_CODES)].copy()
focus_df["焦點排序"] = focus_df["代號"].map({"3441": 1, "2382": 2, "2313": 3}).fillna(9)
focus_df = focus_df.sort_values("焦點排序")
focus_cols = _cols_exist(focus_df, [
    "代號", "名稱", "v214信心閘門", "v212生命週期狀態", "v212目前決策", "第一買點", "盤中現價", "v214停損距離%", "防守停損", "右側加碼價", "追價上限",
    "v212位置判斷", "盤中漲跌幅", "刷新漲速%", "回檔幅度%", "v214調權後分", "左側低吸分", "盤中資金分", "v29漲停前兆分", "v214下一步", "v212下一步", "還缺什麼確認", "報價時間"
])
if focus_df.empty:
    st.warning("目前沒有抓到 3441 / 2382 / 2313 的報價。")
else:
    st.dataframe(focus_df[focus_cols], use_container_width=True, hide_index=True)

# 3) Lifecycle learning/tracking.
st.subheader("🔄 訊號生命週期追蹤")
st.caption("同一檔股票每天只保留一個目前狀態，歷程放在『訊號歷程』；不再同時顯示互相矛盾的可買/不買訊號。")
life_cols = _cols_exist(lifecycle_df, [
    "代號", "名稱", "v212生命週期狀態", "交易員訊號", "首次時間", "首次價格", "目前價格", "目前報酬%", "最高報酬%", "最大回撤%",
    "學習狀態", "訊號歷程", "訊號變更次數", "錯誤歸因", "v212下一步", "最新時間"
])
life_df = lifecycle_df[~lifecycle_df.get("學習狀態", pd.Series(index=lifecycle_df.index, dtype=str)).isna()].copy()
life_df = _safe_sort(life_df, ["v212優先級", "最新時間"], ascending=[True, False]).head(top_n)
if life_df.empty:
    st.info("目前尚未累積生命週期追蹤資料。等待訊號出現後會開始記錄。")
else:
    st.dataframe(life_df[life_cols], use_container_width=True, hide_index=True)

st.subheader("🧾 v2.15 訊號紀錄 + 盤後驗證")
st.caption("這區顯示已驗證後的訊號紀錄。若 Google Sheet Webhook 已設定，請用上方按鈕同步；否則先用 CSV 下載保存。")
journal_cols = _cols_exist(v215_verified_journal_df, [
    "日期", "最新時間", "代號", "名稱", "股票型態", "目前狀態", "目前決策", "結果分類", "盤後驗證結果", "驗證狀態", "驗證時間",
    "首次價格", "驗證價格", "驗證報酬%", "驗證最高報酬%", "驗證最大回撤%", "目前報酬%", "最高報酬%", "最大回撤%",
    "左側低吸分", "盤中資金分", "漲停前兆分", "AI總分", "風險分", "狀態變更次數", "狀態歷程"
])
if v215_verified_journal_df.empty:
    st.info("尚未累積 v2.15 訊號驗證紀錄。")
else:
    show_journal = _safe_sort(v215_verified_journal_df, ["日期", "最新時間"], ascending=[False, False]).head(top_n * 2)
    st.dataframe(show_journal[journal_cols], use_container_width=True, hide_index=True)
    csv_bytes = v215_verified_journal_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button("下載 v2.15 驗證訊號紀錄 CSV", csv_bytes, file_name=f"v215_verified_signal_journal_{now_taipei().strftime('%Y%m%d')}.csv", mime="text/csv")

# 4) Near limit / missed check only if relevant.
if not v211_missed_limit_df.empty:
    st.subheader("📌 近漲停 / 錯過檢查")
    miss_cols = _cols_exist(v211_missed_limit_df, ["代號", "名稱", "目前價", "盤中漲跌幅", "漲停距離%", "交易員訊號", "檢查結果", "可能原因", "左側低吸分", "盤中資金分", "漲停前兆分", "AI來源"])
    st.dataframe(v211_missed_limit_df[miss_cols].head(top_n), use_container_width=True, hide_index=True)

# 5) Advanced diagnostics: collapsed by default to reduce page chaos.
with st.expander("🧪 進階診斷 / 市場池 / 爆衝雷達 / 原始學習紀錄", expanded=(view_mode == "完整診斷")):
    tabs = st.tabs(["爆衝雷達", "市場池前段", "學習原始紀錄", "全部快照"])
    with tabs[0]:
        if not surge_has_prev:
            st.info("第一輪快照還沒有上一輪資料可比較；下一次刷新後爆衝雷達才會啟動。")
        elif surge_df.empty:
            st.info("目前沒有明顯爆衝或急轉弱。")
        else:
            surge_cols = _cols_exist(surge_df, [
                "代號", "名稱", "市場", "產業", "爆衝警示", "刷新漲速%", "上一輪價格", "盤中現價", "量能增量", "量能跳升分", "突破日內高", "盤中漲跌幅", "即時強度分", "爆衝建議", "報價時間"
            ])
            st.dataframe(surge_df[surge_cols].head(top_n), use_container_width=True, hide_index=True)
    with tabs[1]:
        market_cols = _cols_exist(lifecycle_df, [
            "代號", "名稱", "市場", "產業", "v212生命週期狀態", "盤中現價", "盤中漲跌幅", "刷新漲速%", "即時強度分", "AI總分", "風險分", "市場池排名", "資料來源", "AI來源", "報價時間"
        ])
        st.dataframe(_safe_sort(filtered, ["v212優先級", "即時強度分"], ascending=[True, False])[market_cols].head(top_n), use_container_width=True, hide_index=True)
    with tabs[2]:
        if v211_learning_log_df.empty:
            st.info("尚無學習紀錄。")
        else:
            learn_cols = _cols_exist(v211_learning_log_df, [
                "首次時間", "代號", "名稱", "訊號分類", "交易員訊號", "訊號歷程", "訊號變更次數", "首次價格", "目前價格", "目前報酬%", "最高報酬%", "最大回撤%", "學習狀態", "錯誤歸因", "最新時間"
            ])
            st.dataframe(v211_learning_log_df[learn_cols].sort_values("最新時間", ascending=False).head(top_n * 2), use_container_width=True, hide_index=True)
            csv_bytes = v211_learning_log_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
            st.download_button("下載 v2.12 學習紀錄 CSV", csv_bytes, file_name=f"v212_learning_{now_taipei().strftime('%Y%m%d')}.csv", mime="text/csv")
    with tabs[3]:
        all_cols = _cols_exist(lifecycle_df, [
            "代號", "名稱", "市場", "產業", "v212生命週期狀態", "交易員訊號", "第一買點", "盤中現價", "防守停損", "右側加碼價", "追價上限", "盤中漲跌幅", "刷新漲速%", "左側低吸分", "盤中資金分", "v29漲停前兆分", "即時入場分", "AI總分", "風險分", "即時強度分", "資料來源", "AI來源", "報價時間"
        ])
        st.dataframe(lifecycle_df[all_cols], use_container_width=True, hide_index=True)

st.caption("提醒：這是盤中快照與規則化風控系統，不是保證獲利或券商逐筆資料。v2.15 的目的，是把訊號結果保存並驗證，讓後續調權有根據；最高信號仍只代表「小量試單 + 嚴格停損」，不是無腦重倉。")

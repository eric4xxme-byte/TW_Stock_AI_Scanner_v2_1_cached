# pages/live_intraday.py
# v2.8.1 Live Intraday Limit-Up Precursor + Three-Zone Entry Engine
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
        return float(text)
    except Exception:
        return default


def _clean_number(value: Any) -> float:
    v = _to_float(value, default=0.0)
    if isinstance(v, float) and math.isnan(v):
        return 0.0
    return float(v)


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
    rows = []
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw",
    }

    dedup_symbols = list(dict.fromkeys(symbols))
    batch_size = 24

    for i in range(0, len(dedup_symbols), batch_size):
        batch = dedup_symbols[i : i + batch_size]
        params = {
            "ex_ch": "|".join(batch),
            "json": "1",
            "delay": "0",
            "_": str(int(time.time() * 1000)),
        }
        try:
            r = requests.get(QUOTE_URL, params=params, headers=headers, timeout=8)
            r.encoding = "utf-8"
            payload = r.json()
            msg_array = payload.get("msgArray", []) or []
            rows.extend(msg_array)
        except Exception:
            continue
        time.sleep(0.08)

    if not rows:
        return pd.DataFrame()

    out = []
    for q in rows:
        code = str(q.get("c", "")).zfill(4)
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
        strength = float(row.get("即時強度分", 0) or 0)
        ai = float(row.get("AI總分", 0) or 0)
        risk = float(row.get("風險分", 0) or 0)
        pct = float(row.get("盤中漲跌幅", 0) or 0)
        vol_score = float(row.get("盤中量能分", 0) or 0)

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
        ai = float(row.get("AI總分", 0) or 0)
        risk = float(row.get("風險分", 0) or 0)
        strength = float(row.get("即時強度分", 0) or 0)
        pct = float(row.get("盤中漲跌幅", 0) or 0)
        vol_score = float(row.get("盤中量能分", 0) or 0)
        alert = str(row.get("盤中警示", "中性"))
        ai_source = str(row.get("AI來源", ""))

        px = _to_float(row.get("盤中現價"))
        high = _to_float(row.get("最高"))
        low = _to_float(row.get("最低"))
        open_px = _to_float(row.get("開盤"))
        prev = _to_float(row.get("昨收"))

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
        ai = float(row.get("AI總分", 0) or 0)
        risk = float(row.get("風險分", 0) or 0)
        strength = float(row.get("即時強度分", 0) or 0)
        pct = float(row.get("盤中漲跌幅", 0) or 0)
        vol_score = float(row.get("盤中量能分", 0) or 0)
        speed = float(row.get("刷新漲速%", 0) or 0)
        vol_jump = float(row.get("量能跳升分", 0) or 0)
        px = _to_float(row.get("盤中現價"))
        prev = _to_float(row.get("昨收"))
        high = _to_float(row.get("最高"))
        low = _to_float(row.get("最低"))
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
    """Parse display price text such as '72.5' or '-' into float."""
    try:
        if value is None:
            return np.nan
        text = str(value).replace(",", "").strip()
        if text in {"", "-", "--", "nan", "None"}:
            return np.nan
        return float(text)
    except Exception:
        return np.nan


def add_entry_signal_layer(df: pd.DataFrame, chase_pct: float = 7.0) -> pd.DataFrame:
    """v2.7.1: turn watch/decision fields into a clear entry signal.

    This layer is intentionally stricter than the decision dashboard.
    A/B are watch states; only ✅ means the setup has triggered and survived at least one refresh.
    """
    df = df.copy()

    def signal(row):
        px = _to_float(row.get("盤中現價"))
        prev_px = _to_float(row.get("上一輪價格"))
        trigger = _parse_price_text(row.get("觸發價"))
        pct = float(row.get("盤中漲跌幅", 0) or 0)
        speed = float(row.get("刷新漲速%", 0) or 0)
        ai = float(row.get("AI總分", 0) or 0)
        risk = float(row.get("風險分", 0) or 0)
        strength = float(row.get("即時強度分", 0) or 0)
        vol_score = float(row.get("盤中量能分", 0) or 0)
        vol_jump = float(row.get("量能跳升分", 0) or 0)
        decision = str(row.get("決策等級", ""))
        entry = str(row.get("盤中入場判斷", ""))
        surge = str(row.get("爆衝警示", ""))
        ai_source = str(row.get("AI來源", ""))
        limit_dist = _to_float(row.get("漲停距離%"))

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
        px = _to_float(row.get("盤中現價"))
        prev = _to_float(row.get("昨收"))
        open_px = _to_float(row.get("開盤"))
        high = _to_float(row.get("最高"))
        low = _to_float(row.get("最低"))
        pct = float(row.get("盤中漲跌幅", 0) or 0)
        speed = float(row.get("刷新漲速%", 0) or 0)
        vol_score = float(row.get("盤中量能分", 0) or 0)
        vol_jump = float(row.get("量能跳升分", 0) or 0)
        strength = float(row.get("即時強度分", 0) or 0)
        ai = float(row.get("AI總分", 0) or 0)
        risk = float(row.get("風險分", 0) or 0)
        surge_score = float(row.get("爆衝分", 0) or 0)
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
        px = _to_float(row.get("盤中現價"))
        pct = float(row.get("盤中漲跌幅", 0) or 0)
        speed = float(row.get("刷新漲速%", 0) or 0)
        vol_score = float(row.get("盤中量能分", 0) or 0)
        vol_jump = float(row.get("量能跳升分", 0) or 0)
        risk = float(row.get("風險分", 0) or 0)
        precursor = float(row.get("漲停前兆分", 0) or 0)
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
        precursor = float(row.get("漲停前兆分", 0) or 0)
        risk = float(row.get("風險分", 0) or 0)
        pct = float(row.get("盤中漲跌幅", 0) or 0)
        limit_dist = _to_float(row.get("漲停距離%"))
        near_limit = bool(pct >= 8.8 or (not math.isnan(limit_dist) and limit_dist <= 1.2))

        if state == "✅ 二次攻擊可小量試單" and risk < 38 and not near_limit:
            df.at[idx, "入場訊號"] = "✅ 可小量試單"
            df.at[idx, "可否入場"] = "可以小量觀察進場"
            df.at[idx, "入場確認"] = "回檔後二次攻擊觸發"
            df.at[idx, "入場優先級"] = 1
            df.at[idx, "入場訊號分"] = max(float(row.get("入場訊號分", 0) or 0), precursor)
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
            return df
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
        strength = float(row.get("即時強度分", 0) or 0)
        pct = float(row.get("盤中漲跌幅", 0) or 0)
        vol_score = float(row.get("盤中量能分", 0) or 0)
        risk = float(row.get("風險分", 0) or 0)
        ai = float(row.get("AI總分", 0) or 0)
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
        px = _to_float(row.get("盤中現價"))
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
                "AI總分": round(float(row.get("AI總分", 0) or 0), 1),
                "風險分": round(float(row.get("風險分", 0) or 0), 1),
                "首次即時強度分": round(float(row.get("即時強度分", 0) or 0), 1),
                "最新即時強度分": round(float(row.get("即時強度分", 0) or 0), 1),
                "首次盤中漲跌幅": round(float(row.get("盤中漲跌幅", 0) or 0), 2),
                "最新盤中漲跌幅": round(float(row.get("盤中漲跌幅", 0) or 0), 2),
                "盤中成交量": round(float(row.get("盤中成交量", 0) or 0), 0),
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

            log_df.loc[idx, "目前價格"] = round(px, 2)
            log_df.loc[idx, "最高價格"] = round(high_px, 2)
            log_df.loc[idx, "最低價格"] = round(low_px, 2)
            log_df.loc[idx, "目前報酬%"] = round(current_ret, 2)
            log_df.loc[idx, "最高報酬%"] = round(high_ret, 2)
            log_df.loc[idx, "最大回撤%"] = round(low_ret, 2)
            log_df.loc[idx, "最新即時強度分"] = round(float(current_row.get("即時強度分", 0) or 0), 1)
            log_df.loc[idx, "最新盤中漲跌幅"] = round(float(current_row.get("盤中漲跌幅", 0) or 0), 2)
            log_df.loc[idx, "盤中成交量"] = round(float(current_row.get("盤中成交量", 0) or 0), 0)
            log_df.loc[idx, "最新時間"] = now_time

        def status(row):
            ret = float(row.get("目前報酬%", 0) or 0)
            high_ret = float(row.get("最高報酬%", 0) or 0)
            drawdown = float(row.get("最大回撤%", 0) or 0)
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



# ---------- Surge radar ----------

def _load_last_snapshot() -> pd.DataFrame:
    if SURGE_SNAPSHOT_PATH.exists():
        try:
            df = pd.read_csv(SURGE_SNAPSHOT_PATH, dtype={"代號": str})
            if "代號" in df.columns:
                df["代號"] = df["代號"].astype(str).str.zfill(4)
            return df
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
            speed = float(row.get("刷新漲速%", 0) or 0)
            vol_jump = float(row.get("量能跳升分", 0) or 0)
            pct = float(row.get("盤中漲跌幅", 0) or 0)
            risk = float(row.get("風險分", 0) or 0)
            strength = float(row.get("即時強度分", 0) or 0)
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


# ---------- UI ----------

st.title("⚡ 盤中即時看盤 v2.8 漲停前兆 + 回檔再攻入場引擎")
st.caption("先抓漲停前兆，再判斷回檔後是否可能二次攻擊，最後給出低吸區 / 確認價 / 追價上限，避免等確認後才買高。")

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

with st.sidebar:
    st.header("即時設定")
    scan_mode = st.radio(
        "即時掃描範圍",
        ["盤後AI候選", "盤中市場池掃描"],
        index=1 if mode_default == "盤中市場池掃描" else 0,
        help="盤後AI候選：只看每日AI 30檔。盤中市場池掃描：用上市+上櫃成交金額池擴大即時掃描，再依即時強度排序。",
    )
    pool_size = st.slider("市場池檔數", min_value=30, max_value=600, value=pool_default, step=10, disabled=(scan_mode != "盤中市場池掃描"))
    refresh_seconds = st.slider("自動刷新秒數", min_value=15, max_value=120, value=refresh_default, step=15)
    top_n = st.slider("顯示前 N 檔", min_value=5, max_value=50, value=top_n_default, step=5)
    min_ai = st.slider("最低 AI / 市場池分", 0, 100, min_ai_default, 5)
    min_strength = st.slider("最低即時強度分", 0, 100, min_strength_default, 5)

    st.markdown("---")
    st.subheader("手動監控股票")
    extra_codes_text = st.text_area(
        "額外監控代碼",
        value=extra_codes_default,
        placeholder="例如：3441, 6285, 2313",
        help="手動加入股一定會固定顯示。表格中的加入來源只顯示文字，不需要點選。",
    )
    manual_codes = parse_extra_codes(extra_codes_text)
    tracked_codes = _unique_keep_order(manual_codes + FOCUS_CODES)
    st.caption("v2.8 固定追蹤：" + "、".join([f"{c} {FOCUS_LABELS.get(c, '')}".strip() for c in FOCUS_CODES]))
    if manual_codes:
        st.caption("你手動加入：" + "、".join(manual_codes))

    st.markdown("---")
    st.subheader("警示條件")
    attack_threshold = st.slider("強勢進攻門檻", 50, 85, attack_default, 5)
    watch_threshold = st.slider("觀察偏強門檻", 40, 75, watch_default, 5)
    weak_drop = st.slider("AI高分轉弱跌幅", -5.0, 0.0, weak_default, 0.5)
    chase_pct = st.slider("不要追高漲幅", 3.0, 10.0, chase_default, 0.5)

    st.info("設定會寫進網址參數，所以自動刷新後不會跳回預設值。v2.8 預設會固定追蹤 3441 聯一光、2382 廣達、2313 華通。市場池越大，刷新越慢。")

_set_query_if_changed(
    {
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
    }
)

components.html(
    f"""
    <script>
      setTimeout(function() {{
        window.parent.location.reload();
      }}, {int(refresh_seconds) * 1000});
    </script>
    """,
    height=0,
)

rank_df = load_rank()
with st.spinner("建立盤中掃描清單並抓取即時報價..."):
    universe_df, universe_source = build_live_universe(rank_df, scan_mode, pool_size, tracked_codes)
    symbols = build_symbols(universe_df)
    quotes_df = fetch_twse_mis_quotes(symbols)

merged = universe_df.copy()
if quotes_df.empty:
    st.warning("目前沒有抓到盤中報價。可能是非交易時間、TWSE MIS 暫時無回應，或網路限制。")
    for col in ["盤中現價", "盤中漲跌幅", "盤中成交量", "報價時間", "報價市場"]:
        merged[col] = np.nan
else:
    merged = merged.merge(quotes_df, on="代號", how="left")
    if "即時名稱" in merged.columns:
        merged["名稱"] = merged["即時名稱"].fillna(merged["名稱"])
    if "報價市場" in merged.columns:
        merged["市場"] = merged["報價市場"].fillna(merged["市場"])

live_df = compute_live_strength(merged, attack_threshold, watch_threshold, weak_drop, chase_pct)
live_df = add_entry_timing(live_df, chase_pct=chase_pct)
live_df, surge_df, surge_has_prev = update_surge_radar(live_df)
live_df = add_decision_dashboard(live_df)
live_df = add_limitup_reattack_engine(live_df, chase_pct=chase_pct)
live_df = add_v281_three_zone_entry(live_df)
live_df = add_entry_signal_layer(live_df, chase_pct=chase_pct)
live_df = apply_v28_entry_signal_overrides(live_df)
# v2.5.1: Keep the internal boolean, but show a readable text column instead of a non-clickable checkbox.
if "手動加入" in live_df.columns:
    live_df["加入來源"] = np.where(live_df["手動加入"].astype(bool), "手動監控", "自動掃描")
else:
    live_df["加入來源"] = "自動掃描"
filtered = live_df[(live_df["AI總分"] >= min_ai) & (live_df["即時強度分"] >= min_strength)].copy()

quote_ok = int(live_df["盤中現價"].notna().sum())
up_count = int((live_df["盤中漲跌幅"] > 0).sum())
best_pct = float(live_df["盤中漲跌幅"].max()) if len(live_df) else 0
best_strength = float(live_df["即時強度分"].max()) if len(live_df) else 0
attack_count = int((live_df["盤中警示"] == "強勢進攻").sum())
watch_count = int((live_df["盤中警示"] == "觀察偏強").sum())
weak_count = int((live_df["盤中警示"] == "AI高分轉弱").sum())
no_chase_count = int((live_df["盤中警示"].isin(["不要追高", "高風險上漲"])).sum())
entry_break_count = int((live_df["盤中入場判斷"] == "強勢進攻，可盯突破").sum())
entry_pullback_count = int((live_df["盤中入場判斷"] == "觀察偏強，等回測確認").sum())
market_pool_count = int((live_df.get("資料來源", pd.Series(dtype=str)).astype(str).str.contains("市場池")).sum())
daily_ai_count = int((live_df.get("AI來源", pd.Series(dtype=str)).astype(str).eq("盤後AI")).sum())

c1, c2, c3, c4 = st.columns(4)
c1.metric("掃描股票數", len(universe_df))
c2.metric("報價成功檔數", quote_ok)
c3.metric("盤中上漲檔數", up_count)
c4.metric("最高即時強度分", f"{best_strength:.1f}")

c5, c6, c7, c8 = st.columns(4)
c5.metric("盤中最強漲幅", f"{best_pct:.2f}%")
c6.metric("強勢進攻", attack_count)
c7.metric("AI高分轉弱", weak_count)
c8.metric("不要追/高風險", no_chase_count)

c9, c10, c11, c12 = st.columns(4)
c9.metric("觀察偏強", watch_count)
c10.metric("可盯突破", entry_break_count)
c11.metric("等回測確認", entry_pullback_count)
c12.metric("自動刷新", f"{refresh_seconds} 秒")

c13, c14, c15, c16 = st.columns(4)
c13.metric("最後刷新", now_taipei().strftime("%H:%M:%S"))
c14.metric("資料模式", scan_mode)
c15.metric("市場池股數", market_pool_count)
c16.metric("含盤後AI分數", daily_ai_count)

surge_count = int(len(surge_df)) if "surge_df" in globals() else 0
surge_manual_count = int(surge_df.get("手動加入", pd.Series(dtype=bool)).astype(bool).sum()) if surge_count else 0
max_speed = float(pd.to_numeric(live_df.get("刷新漲速%", 0), errors="coerce").fillna(0).max()) if len(live_df) else 0.0
vol_jump_count = int((pd.to_numeric(live_df.get("量能跳升分", 0), errors="coerce").fillna(0) >= 80).sum()) if len(live_df) else 0
c17, c18, c19, c20 = st.columns(4)
c17.metric("爆衝雷達", surge_count)
c18.metric("手動爆衝", surge_manual_count)
c19.metric("最高刷新漲速", f"{max_speed:.2f}%")
c20.metric("量能跳升前段", vol_jump_count)

decision_counts = live_df.get("決策等級", pd.Series(dtype=str)).astype(str).value_counts()
d1, d2, d3, d4 = st.columns(4)
d1.metric("A 可盯突破", int(decision_counts.get("🟢 A 可盯突破", 0)))
d2.metric("B 等回測", int(decision_counts.get("🟡 B 等回測", 0)))
d3.metric("不追/高風險", int(decision_counts.get("🔴 D 不追高", 0) + decision_counts.get("🔴 D 高風險", 0)))
d4.metric("轉弱避開", int(decision_counts.get("⚫ E 轉弱避開", 0) + decision_counts.get("🟠 C 高分轉弱", 0)))

entry_counts = live_df.get("入場訊號", pd.Series(dtype=str)).astype(str).value_counts()
e1, e2, e3, e4 = st.columns(4)
e1.metric("✅ 可小量試單", int(entry_counts.get("✅ 可小量試單", 0)))
e2.metric("🟢 觸發/二攻", int(entry_counts.get("🟢 觸發中，等站穩", 0) + entry_counts.get("🟢 等突破觸發", 0) + entry_counts.get("🟢 等二次攻擊觸發", 0)))
e3.metric("👀 早期雷達/回測", int(entry_counts.get("👀 早期雷達", 0) + entry_counts.get("🟡 等回測確認", 0)))
e4.metric("🔴 不可追/避開", int(entry_counts.get("🔴 不可追", 0) + entry_counts.get("⚫ 避開", 0)))

p1, p2, p3, p4 = st.columns(4)
precursor_high = int((pd.to_numeric(live_df.get("漲停前兆分", 0), errors="coerce").fillna(0) >= 62).sum())
reattack_ready = int((live_df.get("回檔再攻狀態", pd.Series(dtype=str)).astype(str) == "✅ 二次攻擊可小量試單").sum())
reattack_wait = int((live_df.get("回檔再攻狀態", pd.Series(dtype=str)).astype(str) == "🟢 等二次攻擊觸發").sum())
focus_active = int((live_df.get("v28核心追蹤", pd.Series(dtype=str)).astype(str) == "是").sum())
p1.metric("漲停前兆升溫", precursor_high)
p2.metric("二次攻擊可試", reattack_ready)
p3.metric("等二次觸發", reattack_wait)
p4.metric("核心追蹤股", focus_active)

st.caption(f"掃描來源：{universe_source}")
if scan_mode == "盤中市場池掃描":
    st.warning("市場池掃描股若顯示『市場池估分』，代表它只有盤中動能與成交金額排序，沒有完整盤後AI/籌碼驗證。入場判斷要更保守。")


st.divider()

st.subheader("🚦 即時入場訊號中控台")
st.caption("先看這區：只有 ✅ 可小量試單 才代表系統認為已觸發入場條件；🟢 只是等待突破或等待站穩，🟡 是等回測，🔴/⚫ 不碰。")

entry_signal_cols = [
    "代號", "名稱", "市場", "產業", "v28核心追蹤", "入場訊號", "可否入場", "入場訊號分", "入場確認",
    "盤中現價", "左側低吸區", "右側確認價", "追價上限", "觸發價", "二次攻擊觸發價", "回測支撐價", "防守停損價", "停損參考", "壓力參考",
    "盤中漲跌幅", "刷新漲速%", "回檔幅度%", "日內高點漲幅%", "盤中成交量",
    "AI總分", "風險分", "即時強度分", "爆衝分", "漲停前兆分", "漲停前兆狀態", "再攻機率", "回檔再攻狀態", "漲停雷達", "漲停距離%",
    "入場價位策略", "三段式進場建議", "買高警示", "建議進場區間", "入場條件檢查", "建議下單方式", "資料來源", "AI來源", "報價時間"
]
entry_signal_cols = [c for c in entry_signal_cols if c in live_df.columns]
entry_signal_df = live_df[live_df["入場訊號"].isin(["✅ 可小量試單", "🟢 觸發中，等站穩", "🟢 等突破觸發", "🟢 等二次攻擊觸發", "👀 早期雷達", "🟡 等回測確認"])].copy()
entry_signal_df = entry_signal_df.sort_values(["入場優先級", "入場訊號分", "決策分"], ascending=[True, False, False])

if entry_signal_df.empty:
    st.info("目前沒有明確入場訊號。先不要硬追，等下一次刷新、突破觸發價或回測確認。")
else:
    st.dataframe(entry_signal_df[entry_signal_cols].head(top_n), use_container_width=True, hide_index=True)

with st.expander("🔴 目前不可入場 / 不可追 / 避開", expanded=False):
    blocked_df = live_df[live_df["入場訊號"].isin(["🔴 不可追", "⚫ 避開"])].copy()
    blocked_df = blocked_df.sort_values(["入場優先級", "盤中漲跌幅"], ascending=[True, False])
    if blocked_df.empty:
        st.caption("目前沒有明確不可追或轉弱避開名單。")
    else:
        st.dataframe(blocked_df[entry_signal_cols].head(top_n), use_container_width=True, hide_index=True)


st.subheader("🚀 漲停前兆雷達")
st.caption("v2.8：這區不靠盤後 AI 擋股票，優先看短線漲速、量能跳升、日內高點、距離漲停與回檔再攻條件。")
precursor_cols = [
    "代號", "名稱", "市場", "產業", "v28核心追蹤", "漲停前兆分", "漲停前兆狀態", "再攻機率", "回檔再攻狀態",
    "盤中現價", "左側低吸區", "右側確認價", "追價上限", "二次攻擊觸發價", "回測支撐價", "防守停損價", "入場價位策略", "三段式進場建議", "建議進場區間",
    "盤中漲跌幅", "刷新漲速%", "回檔幅度%", "日內高點漲幅%", "盤中成交量", "量能跳升分",
    "入場訊號", "可否入場", "回檔再攻判斷", "AI總分", "風險分", "資料來源", "AI來源", "報價時間"
]
precursor_cols = [c for c in precursor_cols if c in live_df.columns]
precursor_df = live_df[pd.to_numeric(live_df.get("漲停前兆分", 0), errors="coerce").fillna(0) >= 45].copy()
precursor_df = precursor_df.sort_values(["漲停前兆分", "再攻機率", "即時強度分"], ascending=[False, True, False])
if precursor_df.empty:
    st.info("目前沒有明顯漲停前兆。")
else:
    st.dataframe(precursor_df[precursor_cols].head(top_n), use_container_width=True, hide_index=True)

st.subheader("🎯 重點股回檔再攻分析：聯一光 / 廣達 / 華通")
st.caption("這區固定顯示 3441、2382、2313：用來回答『急拉回檔後，是否可能再爆衝、該在低吸區還是確認價進場』。看到 ✅ 才是可小量試單；看到 🟢/👀 都只是等條件。")
focus_df = live_df[live_df["代號"].astype(str).str.zfill(4).isin(FOCUS_CODES)].copy()
focus_df["焦點排序"] = focus_df["代號"].map({"3441": 1, "2382": 2, "2313": 3}).fillna(9)
focus_df = focus_df.sort_values(["焦點排序"])
focus_cols = [
    "代號", "名稱", "入場訊號", "可否入場", "盤中現價", "左側低吸區", "右側確認價", "追價上限", "二次攻擊觸發價", "回測支撐價", "防守停損價", "入場價位策略", "三段式進場建議", "建議進場區間",
    "回檔再攻狀態", "再攻機率", "漲停前兆分", "漲停前兆狀態", "盤中漲跌幅", "刷新漲速%", "回檔幅度%", "日內高點漲幅%",
    "AI總分", "風險分", "即時強度分", "回檔再攻判斷", "報價時間"
]
focus_cols = [c for c in focus_cols if c in focus_df.columns]
if focus_df.empty:
    st.warning("目前沒有抓到 3441 / 2382 / 2313 的即時資料。")
else:
    st.dataframe(focus_df[focus_cols], use_container_width=True, hide_index=True)

st.subheader("🎯 今日入場決策中控台")
st.caption("這區保留 A/B/C/D/E 決策分層；真正要不要進，以上方『即時入場訊號』為準。")

decision_cols = [
    "代號", "名稱", "市場", "產業", "入場訊號", "可否入場", "決策等級", "決策分", "入場訊號分", "爆衝分", "漲停雷達", "漲停距離%",
    "盤中現價", "盤中漲跌幅", "刷新漲速%", "盤中成交量", "AI總分", "風險分", "即時強度分",
    "觸發價", "停損參考", "壓力參考", "第一優先原因", "入場建議", "入場條件檢查", "資料來源", "AI來源", "報價時間"
]
decision_cols = [c for c in decision_cols if c in live_df.columns]

ready_df = live_df[live_df["決策等級"].isin(["🟢 A 可盯突破", "🟡 B 等回測"])].copy()
ready_df = ready_df.sort_values(["決策排序", "決策分", "即時強度分"], ascending=[True, False, False])

risk_df = live_df[live_df["決策等級"].isin(["🔴 D 不追高", "🔴 D 高風險", "🟠 C 高分轉弱", "⚫ E 轉弱避開"])].copy()
risk_df = risk_df.sort_values(["決策排序", "決策分"], ascending=[True, False])

if ready_df.empty:
    st.info("目前沒有 A/B 級入場觀察股。先不要硬追，等下一次刷新或等回測訊號。")
else:
    st.dataframe(ready_df[decision_cols].head(top_n), use_container_width=True, hide_index=True)

with st.expander("🔴 目前不建議追價 / 轉弱避開", expanded=False):
    if risk_df.empty:
        st.caption("目前沒有明顯 D/E 風險股。")
    else:
        st.dataframe(risk_df[decision_cols].head(top_n), use_container_width=True, hide_index=True)

st.subheader("🔥 漲停爆衝雷達")
st.caption("v2.6.2：比較上一輪與目前報價，抓短時間價格加速度、量能跳升、日內高點突破。這用來補足『突然爆衝』，不是單純看目前漲幅排行。")
if not surge_has_prev:
    st.info("這是本次啟動後第一輪快照，還沒有上一輪價格可比較。等下一次自動刷新後，爆衝雷達才會開始判斷。")
elif surge_df.empty:
    st.info("目前沒有偵測到明顯爆衝或急轉弱。")
else:
    surge_cols = [
        "代號", "名稱", "市場", "產業", "加入來源", "資料來源", "AI來源", "爆衝警示", "刷新漲速%", "上一輪價格", "盤中現價",
        "量能增量", "量能跳升分", "突破日內高", "盤中漲跌幅", "即時強度分", "盤中入場判斷", "觸發價", "停損參考", "壓力參考", "爆衝建議", "報價時間"
    ]
    surge_cols = [c for c in surge_cols if c in surge_df.columns]
    st.dataframe(surge_df[surge_cols].head(top_n), use_container_width=True, hide_index=True)

# v2.6.1/2: signal tracking
signal_log_df = update_runtime_signal_log(live_df)

st.subheader("今日訊號追蹤")
st.caption("v2.6.1：只記錄有效進攻、觀察訊號、轉弱訊號與風險訊號，不再把大量普通中性股塞進紀錄。時間統一使用台灣時間。這是前台即時紀錄；Streamlit 重開或重新部署後可能會重置，長期統計仍要靠後台回測。")

if signal_log_df.empty:
    st.info("目前尚未出現可追蹤的盤中訊號。")
else:
    sig_total = len(signal_log_df)
    sig_effective = int((pd.to_numeric(signal_log_df.get("目前報酬%", 0), errors="coerce").fillna(0) > 0).sum())
    sig_failed = int((pd.to_numeric(signal_log_df.get("目前報酬%", 0), errors="coerce").fillna(0) <= -1.5).sum())
    best_ret = float(pd.to_numeric(signal_log_df.get("最高報酬%", 0), errors="coerce").fillna(0).max())

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("今日有效訊號數", sig_total)
    s2.metric("目前為正", sig_effective)
    s3.metric("失效訊號", sig_failed)
    s4.metric("最高訊號報酬", f"{best_ret:.2f}%")

    type_counts = signal_log_df.get("訊號類型", pd.Series(dtype=str)).astype(str).value_counts()
    t1, t2, t3, t4 = st.columns(4)
    t1.metric("有效進攻", int(type_counts.get("有效進攻", 0)))
    t2.metric("觀察訊號", int(type_counts.get("觀察訊號", 0)))
    t3.metric("轉弱訊號", int(type_counts.get("轉弱訊號", 0)))
    t4.metric("風險訊號", int(type_counts.get("風險訊號", 0)))

    track_cols = [
        "首次時間", "代號", "名稱", "市場", "訊號類型", "首次標籤", "首次入場判斷", "首次訊號價", "目前價格",
        "目前報酬%", "最高報酬%", "最大回撤%", "目前狀態", "AI總分", "風險分",
        "首次即時強度分", "最新即時強度分", "最新盤中漲跌幅", "觸發價", "停損參考", "壓力參考", "建議動作", "最新時間"
    ]
    track_cols = [c for c in track_cols if c in signal_log_df.columns]
    show_log = signal_log_df.copy()
    if "首次時間" in show_log.columns:
        show_log = show_log.sort_values(["首次時間", "目前報酬%"], ascending=[False, False])
    st.dataframe(show_log[track_cols], use_container_width=True, hide_index=True)

    csv_bytes = signal_log_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(
        "下載今日訊號紀錄 CSV",
        data=csv_bytes,
        file_name=f"intraday_signal_log_{now_taipei().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )

    if st.button("清除今日前台訊號紀錄", type="secondary"):
        clear_runtime_signal_log()
        st.rerun()


# Manual watchlist always visible.
manual_live_df = live_df[live_df.get("手動加入", False).astype(bool)].copy()
if tracked_codes:
    st.subheader("手動 / 核心監控股票即時狀態")
    st.caption("這區固定顯示你左側輸入的股票，以及 v2.8 核心追蹤股 3441 / 2382 / 2313。表格不需要勾選。")
    manual_cols = [
        "代號", "名稱", "市場", "產業", "資料來源", "AI來源", "加入來源", "v28核心追蹤", "市場池排名", "入場訊號", "可否入場", "漲停前兆分", "回檔再攻狀態", "再攻機率", "盤中標籤", "爆衝警示", "刷新漲速%", "量能跳升分", "盤中入場判斷", "入場型態",
        "左側低吸區", "右側確認價", "追價上限", "觸發價", "二次攻擊觸發價", "回測支撐價", "防守停損價", "入場價位策略", "三段式進場建議", "建議進場區間", "停損參考", "壓力參考", "AI總分", "風險分", "即時強度分",
        "盤中現價", "盤中漲跌幅", "盤中成交量", "報價時間", "即時判斷", "建議動作", "回檔再攻判斷"
    ]
    manual_cols = [c for c in manual_cols if c in manual_live_df.columns]
    if manual_live_df.empty:
        st.warning("已收到手動監控代碼，但目前沒有產生資料。請確認代碼是否為 4 碼台股代號，或等待下一次刷新。")
    else:
        st.dataframe(manual_live_df[manual_cols], use_container_width=True, hide_index=True)
    st.divider()

# Priority watchlist.
watch_df = live_df[
    (
        live_df["盤中警示"].isin(["強勢進攻", "觀察偏強"])
        & (live_df["風險分"] < 40)
        & (live_df["AI總分"] >= 45)
        & (live_df["盤中漲跌幅"] > 0)
    )
].copy()
watch_df = watch_df.sort_values(["警示排序", "即時強度分"], ascending=[True, False])

st.subheader("盤中入場時機判斷")
st.caption("優先看『可盯突破』與『等回測確認』；觸發價、停損與壓力只做盤中觀察參考，不是下單指令。")

entry_df = live_df[
    live_df["盤中入場判斷"].isin(["強勢進攻，可盯突破", "觀察偏強，等回測確認", "AI高分但盤中轉弱", "漲幅偏高，不追"])
].copy()
entry_priority = {
    "強勢進攻，可盯突破": 1,
    "觀察偏強，等回測確認": 2,
    "AI高分但盤中轉弱": 3,
    "漲幅偏高，不追": 4,
}
entry_df["入場排序"] = entry_df["盤中入場判斷"].map(entry_priority).fillna(9)
entry_df = entry_df.sort_values(["入場排序", "即時強度分"], ascending=[True, False])

common_cols = [
    "代號", "名稱", "市場", "產業", "入場訊號", "可否入場", "決策等級", "是否可入場", "決策分", "爆衝分", "漲停前兆分", "漲停前兆狀態", "再攻機率", "回檔再攻狀態", "漲停雷達", "漲停距離%",
    "資料來源", "AI來源", "加入來源", "市場池排名", "盤中標籤", "爆衝警示", "刷新漲速%", "量能跳升分", "回檔幅度%", "盤中入場判斷", "入場型態",
    "觸發價", "二次攻擊觸發價", "回測支撐價", "防守停損價", "停損參考", "壓力參考", "AI總分", "風險分", "即時強度分",
    "盤中現價", "盤中漲跌幅", "盤中成交量", "報價時間", "即時判斷", "不追原因", "建議動作", "回檔再攻判斷"
]

entry_cols = [c for c in common_cols if c in entry_df.columns]
if entry_df.empty:
    st.info("目前沒有明確入場時機訊號。先觀察，不急著追。")
else:
    st.dataframe(entry_df[entry_cols].head(top_n), use_container_width=True, hide_index=True)

st.subheader("今日優先盯盤")
st.caption("只列出：強勢進攻 / 觀察偏強，且風險分不高、AI/市場池分不太低、盤中漲幅為正。")
watch_cols = [c for c in common_cols if c in watch_df.columns]
if watch_df.empty:
    st.info("目前沒有符合『優先盯盤』條件的股票。先觀察，不急著追。")
else:
    st.dataframe(watch_df[watch_cols].head(top_n), use_container_width=True, hide_index=True)

st.subheader("盤中市場池即時前段")
st.caption("這區是 v2.5 重點：從市場池中依即時強度排序，不只限於盤後 AI 30 檔。")
market_cols = [c for c in common_cols if c in filtered.columns]
st.dataframe(filtered.sort_values(["警示排序", "即時強度分"], ascending=[True, False])[market_cols].head(top_n), use_container_width=True, hide_index=True)

st.subheader("全部掃描池即時快照")
st.dataframe(live_df[market_cols], use_container_width=True, hide_index=True)

st.caption("提醒：這是網頁快照更新，不是券商逐筆成交資料。v2.8.1 的漲停前兆分與三段式入場價是規則化風控參考，不保證漲停；只有 ✅ 可小量試單 才代表條件觸發，且仍需嚴守防守停損。")

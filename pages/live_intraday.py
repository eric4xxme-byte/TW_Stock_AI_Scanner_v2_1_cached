# pages/live_intraday.py
# v2.6 Live Intraday Signal Tracking
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


def update_runtime_signal_log(live_df: pd.DataFrame) -> pd.DataFrame:
    """Keep a local runtime signal log.

    This file is written by the Streamlit app instance. It survives browser refreshes,
    but it is not committed to GitHub and may reset after Streamlit reboot/redeploy.
    """
    now = datetime.now()
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

        should_log = (alert_name in IMPORTANT_ALERTS) or (entry_name in IMPORTANT_ENTRIES)
        if not should_log or math.isnan(px) or px <= 0:
            continue

        signal_key = f"{today}|{code}|{alert_name}|{entry_name}"
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


# ---------- UI ----------

st.title("⚡ 盤中即時看盤 v2.6 訊號追蹤")
st.caption("盤後 AI 候選 + 盤中市場池掃描 + 前台即時報價 + 入場時機輔助判斷 + 今日訊號追蹤。")

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
pool_default = _get_query_int("pool_size", 150, 30, 300, 10)

with st.sidebar:
    st.header("即時設定")
    scan_mode = st.radio(
        "即時掃描範圍",
        ["盤後AI候選", "盤中市場池掃描"],
        index=1 if mode_default == "盤中市場池掃描" else 0,
        help="盤後AI候選：只看每日AI 30檔。盤中市場池掃描：用上市+上櫃成交金額池擴大即時掃描，再依即時強度排序。",
    )
    pool_size = st.slider("市場池檔數", min_value=30, max_value=300, value=pool_default, step=10, disabled=(scan_mode != "盤中市場池掃描"))
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
    if manual_codes:
        st.caption("已手動加入：" + "、".join(manual_codes))

    st.markdown("---")
    st.subheader("警示條件")
    attack_threshold = st.slider("強勢進攻門檻", 50, 85, attack_default, 5)
    watch_threshold = st.slider("觀察偏強門檻", 40, 75, watch_default, 5)
    weak_drop = st.slider("AI高分轉弱跌幅", -5.0, 0.0, weak_default, 0.5)
    chase_pct = st.slider("不要追高漲幅", 3.0, 10.0, chase_default, 0.5)

    st.info("設定會寫進網址參數，所以自動刷新後不會跳回預設值。市場池越大，刷新越慢、也越容易被報價來源限制。")

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
    universe_df, universe_source = build_live_universe(rank_df, scan_mode, pool_size, manual_codes)
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
c13.metric("最後刷新", datetime.now().strftime("%H:%M:%S"))
c14.metric("資料模式", scan_mode)
c15.metric("市場池股數", market_pool_count)
c16.metric("含盤後AI分數", daily_ai_count)

st.caption(f"掃描來源：{universe_source}")
if scan_mode == "盤中市場池掃描":
    st.warning("市場池掃描股若顯示『市場池估分』，代表它只有盤中動能與成交金額排序，沒有完整盤後AI/籌碼驗證。入場判斷要更保守。")


st.divider()

# v2.6: signal tracking
signal_log_df = update_runtime_signal_log(live_df)

st.subheader("今日訊號追蹤")
st.caption("系統會紀錄盤中警示第一次出現的時間與價格，並追蹤目前報酬、最高報酬與最大回撤。這是前台即時紀錄；Streamlit 重開或重新部署後可能會重置，長期統計仍要靠後台回測。")

if signal_log_df.empty:
    st.info("目前尚未出現可追蹤的盤中訊號。")
else:
    sig_total = len(signal_log_df)
    sig_effective = int((pd.to_numeric(signal_log_df.get("目前報酬%", 0), errors="coerce").fillna(0) > 0).sum())
    sig_failed = int((pd.to_numeric(signal_log_df.get("目前報酬%", 0), errors="coerce").fillna(0) <= -1.5).sum())
    best_ret = float(pd.to_numeric(signal_log_df.get("最高報酬%", 0), errors="coerce").fillna(0).max())

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("今日訊號數", sig_total)
    s2.metric("目前為正", sig_effective)
    s3.metric("失效訊號", sig_failed)
    s4.metric("最高訊號報酬", f"{best_ret:.2f}%")

    track_cols = [
        "首次時間", "代號", "名稱", "市場", "首次標籤", "首次入場判斷", "首次訊號價", "目前價格",
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
        file_name=f"intraday_signal_log_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )

    if st.button("清除今日前台訊號紀錄", type="secondary"):
        clear_runtime_signal_log()
        st.rerun()


# Manual watchlist always visible.
manual_live_df = live_df[live_df.get("手動加入", False).astype(bool)].copy()
if manual_codes:
    st.subheader("手動監控股票即時狀態")
    st.caption("這區固定顯示你左側輸入的股票；即使它沒有進入今日AI候選或市場池前段，也會顯示。表格不需要勾選，加入來源會顯示為「手動監控」。")
    manual_cols = [
        "代號", "名稱", "市場", "產業", "資料來源", "AI來源", "加入來源", "市場池排名", "盤中標籤", "盤中入場判斷", "入場型態",
        "觸發價", "停損參考", "壓力參考", "AI總分", "風險分", "即時強度分",
        "盤中現價", "盤中漲跌幅", "盤中成交量", "報價時間", "即時判斷", "建議動作"
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
    "代號", "名稱", "市場", "產業", "資料來源", "AI來源", "加入來源", "市場池排名", "盤中標籤", "盤中入場判斷", "入場型態",
    "觸發價", "停損參考", "壓力參考", "AI總分", "風險分", "即時強度分",
    "盤中現價", "盤中漲跌幅", "盤中成交量", "報價時間", "即時判斷", "不追原因", "建議動作"
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

st.caption("提醒：這是網頁快照更新，不是券商逐筆成交資料。市場池估分股沒有完整盤後籌碼驗證；觸發價、停損與壓力是規則化參考，不等於下單建議。")

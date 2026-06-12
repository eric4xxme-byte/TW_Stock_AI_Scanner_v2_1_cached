# -*- coding: utf-8 -*-
"""
TW Stock AI Scanner v2.16｜Background learning sync with session split

Purpose:
- Run from GitHub Actions without opening Streamlit, with intraday/post-close/night-session split.
- Build intraday market pool from saved AI candidates + TWSE/TPEx value leaders + focus codes.
- Fetch TWSE MIS intraday quotes.
- Update data/v215_verified_signal_journal.csv as a durable GitHub-side journal.
- Sync recent rows to Google Sheet webhook in batches.

Required env:
- GSHEET_WEBHOOK_URL: Google Apps Script Web App URL ending in /exec
Optional env:
- BACKGROUND_POOL_LIMIT: default 180
- BACKGROUND_SYNC_LIMIT: default 120
- BACKGROUND_BATCH_SIZE: default 20
- FORCE_RUN: "1" to run even outside Taiwan trading/session hours
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

LATEST_RANK_FILE = DATA_DIR / "latest_rank.csv"
INTRADAY_SNAPSHOT_FILE = DATA_DIR / "intraday_snapshot.csv"
JOURNAL_FILE = DATA_DIR / "v215_verified_signal_journal.csv"
META_FILE = DATA_DIR / "v215_background_sync_meta.json"
POST_CLOSE_FILE = DATA_DIR / "v216_post_close_verification.json"

TAIPEI = timezone(timedelta(hours=8))
MIS_URLS = [
    "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
    "http://mis.twse.com.tw/stock/api/getStockInfo.jsp",
]
MIS_WARMUP_URLS = [
    "https://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw",
    "https://mis.twse.com.tw/stock/index.jsp",
    "http://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw",
]

FOCUS_STOCKS = {
    "3441": {"名稱": "聯一光", "市場": "上市", "產業": "光電業"},
    "2382": {"名稱": "廣達", "市場": "上市", "產業": "電子工業"},
    "2313": {"名稱": "華通", "市場": "上市", "產業": "電子工業"},
}

FALLBACK_POOL = [
    ("2330", "台積電", "上市", "半導體業"),
    ("2317", "鴻海", "上市", "電子工業"),
    ("2454", "聯發科", "上市", "半導體業"),
    ("2382", "廣達", "上市", "電子工業"),
    ("2313", "華通", "上市", "電子工業"),
    ("3441", "聯一光", "上市", "光電業"),
    ("2308", "台達電", "上市", "電子零組件業"),
    ("3231", "緯創", "上市", "電子工業"),
    ("6669", "緯穎", "上市", "電子工業"),
    ("3008", "大立光", "上市", "光電業"),
]


def now_tw() -> datetime:
    return datetime.now(TAIPEI)


def v216_session_mode(dt: datetime) -> str:
    """Return the Taiwan market session bucket used by v2.16."""
    if dt.weekday() >= 5:
        return "weekend"
    minutes = dt.hour * 60 + dt.minute
    if 8 * 60 + 45 <= minutes <= 13 * 60 + 35:
        return "intraday"
    if 13 * 60 + 36 <= minutes <= 16 * 60 + 30:
        return "post_close_verify"
    if minutes >= 16 * 60 + 31 or minutes <= 5 * 60 + 10:
        return "night_context"
    if 5 * 60 + 11 <= minutes < 8 * 60 + 45:
        return "pre_open"
    return "off_hours"


def in_taiwan_market_window(dt: datetime) -> bool:
    if dt.weekday() >= 5:
        return False
    # v2.16: journal learning runs only intraday and limited post-close verification,
    # not all night. Night context is handled by background_market_context.py.
    return v216_session_mode(dt) in {"intraday", "post_close_verify"}


def normalize_code(v: Any) -> str:
    s = str(v or "").strip()
    m = re.search(r"(\d{4,6})", s)
    if not m:
        return ""
    return m.group(1)[:6]


def safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    if v is None:
        return default
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if math.isfinite(float(v)):
            return float(v)
        return default
    s = str(v).strip().replace(",", "").replace("%", "")
    if s in {"", "-", "--", "None", "nan", "NaN"}:
        return default
    # If a range like 291~292 appears, use the first numeric value.
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return default
    try:
        return float(m.group(0))
    except Exception:
        return default


def safe_int(v: Any, default: Optional[int] = None) -> Optional[int]:
    f = safe_float(v)
    if f is None:
        return default
    return int(f)


def scalar(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        if isinstance(v, float) and not math.isfinite(v):
            return ""
        return v
    if isinstance(v, (list, tuple, set)):
        return " / ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    return str(v)


def request_json(url: str, timeout: int = 12) -> Any:
    headers = {
        "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/125 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def pick(row: Dict[str, Any], names: Iterable[str], default: Any = "") -> Any:
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return default


def parse_market_rows(data: Any, market: str, limit: int) -> List[Dict[str, Any]]:
    if not isinstance(data, list):
        return []
    rows: List[Dict[str, Any]] = []
    for r in data:
        if not isinstance(r, dict):
            continue
        code = normalize_code(pick(r, ["證券代號", "Code", "SecuritiesCompanyCode", "代號", "股票代號", "有價證券代號"]))
        if not code or not code.isdigit() or len(code) < 4:
            continue
        name = str(pick(r, ["證券名稱", "Name", "CompanyName", "名稱", "股票名稱", "有價證券名稱"], code)).strip()
        money = safe_float(pick(r, ["成交金額", "TradeValue", "trade_value", "Trading_money", "Amount", "成交金額(元)"]), 0) or 0
        industry = str(pick(r, ["產業", "industry_category", "產業別"], "未知")).strip() or "未知"
        rows.append({"代號": code, "名稱": name, "市場": market, "產業": industry, "成交金額": money, "來源": f"{market}成交金額排行"})
    rows.sort(key=lambda x: safe_float(x.get("成交金額"), 0) or 0, reverse=True)
    return rows[:limit]


def fetch_value_leaders(limit: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    twse_urls = [
        "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
        "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=open_data",
        "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=open_data",
    ]
    for url in twse_urls:
        rows = parse_market_rows(request_json(url), "上市", limit)
        if rows:
            out.extend(rows)
            break
    tpex_urls = ["https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"]
    for url in tpex_urls:
        rows = parse_market_rows(request_json(url), "上櫃", limit)
        if rows:
            out.extend(rows)
            break
    # de-dupe by code, keep highest amount
    by_code: Dict[str, Dict[str, Any]] = {}
    for r in out:
        c = r.get("代號", "")
        if not c:
            continue
        if c not in by_code or (safe_float(r.get("成交金額"), 0) or 0) > (safe_float(by_code[c].get("成交金額"), 0) or 0):
            by_code[c] = r
    return sorted(by_code.values(), key=lambda x: safe_float(x.get("成交金額"), 0) or 0, reverse=True)[:limit]


def read_latest_rank(limit: int) -> List[Dict[str, Any]]:
    if not LATEST_RANK_FILE.exists():
        return []
    try:
        df = pd.read_csv(LATEST_RANK_FILE, dtype=str).head(limit)
    except Exception:
        return []
    rows = []
    for _, r in df.iterrows():
        code = normalize_code(r.get("代號") or r.get("stock_id"))
        if not code:
            continue
        rows.append({
            "代號": code,
            "名稱": str(r.get("名稱") or r.get("stock_name") or code),
            "市場": str(r.get("市場") or ""),
            "產業": str(r.get("產業") or r.get("industry_category") or "未知"),
            "AI總分": safe_float(r.get("AI總分") or r.get("AI分數") or r.get("總分"), 50) or 50,
            "風險分": safe_float(r.get("風險分"), 30) or 30,
            "來源": "盤後AI候選",
        })
    return rows


def build_pool(limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    rows.extend(read_latest_rank(min(limit, 80)))
    rows.extend(fetch_value_leaders(limit))
    for c, meta in FOCUS_STOCKS.items():
        rows.append({"代號": c, **meta, "來源": "固定重點股"})
    for c, name, market, industry in FALLBACK_POOL:
        rows.append({"代號": c, "名稱": name, "市場": market, "產業": industry, "來源": "備援池"})

    by_code: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        c = normalize_code(r.get("代號"))
        if not c:
            continue
        old = by_code.get(c, {})
        merged = {**old, **{k: v for k, v in r.items() if v not in (None, "")}}
        merged["代號"] = c
        # source priority
        if old and old.get("來源") == "盤後AI候選":
            merged["來源"] = old.get("來源")
        by_code[c] = merged
    # Keep focus stocks no matter what, then top pool.
    items = list(by_code.values())
    items.sort(key=lambda r: (0 if r.get("代號") in FOCUS_STOCKS else 1, -(safe_float(r.get("成交金額"), 0) or 0)))
    return items[:limit]


def chunked(items: List[Any], n: int) -> Iterable[List[Any]]:
    for i in range(0, len(items), n):
        yield items[i:i+n]


def build_ex_channels(pool: List[Dict[str, Any]]) -> Tuple[List[str], Dict[str, Dict[str, Any]], List[str]]:
    channels: List[str] = []
    meta: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for r in pool:
        c = normalize_code(r.get("代號"))
        if not c:
            continue
        order.append(c)
        meta[c] = r
        # Try both exchanges for reliability.
        channels.append(f"tse_{c}.tw")
        channels.append(f"otc_{c}.tw")
    seen = set()
    uniq = []
    for ch in channels:
        if ch not in seen:
            uniq.append(ch)
            seen.add(ch)
    return uniq, meta, order


def build_mis_url(base_url: str, batch: List[str]) -> str:
    ex_ch = "%7c".join(quote(x, safe="_.") for x in batch)
    return f"{base_url}?ex_ch={ex_ch}&json=1&delay=0&_={int(time.time()*1000)}"


def warmup(session: requests.Session, headers: Dict[str, str]) -> None:
    for url in MIS_WARMUP_URLS:
        try:
            session.get(url, headers=headers, timeout=8)
            time.sleep(0.12)
        except Exception:
            pass


def fetch_mis_quotes(channels: List[str], batch_size: int = 24, retries: int = 2) -> List[Dict[str, Any]]:
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
        "Referer": "https://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }
    warmup(session, headers)
    out: List[Dict[str, Any]] = []
    seen = set()
    for batch in chunked(channels, batch_size):
        ok = False
        for attempt in range(retries):
            for base in MIS_URLS:
                try:
                    r = session.get(build_mis_url(base, batch), headers=headers, timeout=15)
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    arr = data.get("msgArray", [])
                    if isinstance(arr, list) and arr:
                        for q in arr:
                            ch = str(q.get("ch") or "")
                            if ch in seen:
                                continue
                            seen.add(ch)
                            out.append(q)
                        ok = True
                        break
                except Exception as exc:
                    print(f"[WARN] MIS failed attempt={attempt+1}: {exc}")
            if ok:
                break
            time.sleep(0.6 + attempt * 0.7)
        time.sleep(0.25)
    return out


def extract_price(q: Dict[str, Any]) -> Tuple[Optional[float], str]:
    for key, label in [("z", "成交價"), ("pz", "最近成交價")]:
        f = safe_float(q.get(key))
        if f is not None:
            return f, label
    bid = safe_float(q.get("b"))
    ask = safe_float(q.get("a"))
    if bid is not None and ask is not None:
        return round((bid + ask)/2, 2), "買賣均價"
    if bid is not None:
        return bid, "最佳買價"
    if ask is not None:
        return ask, "最佳賣價"
    y = safe_float(q.get("y"))
    if y is not None:
        return y, "昨收參考"
    return None, "無報價"


def quote_rows(quotes: List[Dict[str, Any]], meta: Dict[str, Dict[str, Any]], order: List[str]) -> pd.DataFrame:
    rows = []
    ts = now_tw().strftime("%Y-%m-%d %H:%M:%S")
    for q in quotes:
        code = normalize_code(q.get("c"))
        if not code:
            continue
        m = meta.get(code, {})
        price, source = extract_price(q)
        prev = safe_float(q.get("y"))
        high = safe_float(q.get("h"))
        low = safe_float(q.get("l"))
        pct = None
        if price is not None and prev not in (None, 0):
            pct = round((price - prev) / prev * 100, 2)
        market = "上市" if str(q.get("ex", "")).lower() == "tse" or str(q.get("ch", "")).startswith("tse_") else ("上櫃" if str(q.get("ex", "")).lower() == "otc" or str(q.get("ch", "")).startswith("otc_") else m.get("市場", ""))
        rows.append({
            "最新時間": ts,
            "代號": code,
            "名稱": str(q.get("n") or m.get("名稱") or code),
            "市場": market,
            "產業": m.get("產業", "未知"),
            "股票型態": classify_type(code, m.get("產業", ""), market),
            "盤中現價": price,
            "昨收": prev,
            "最高": high,
            "最低": low,
            "盤中漲跌幅": pct if pct is not None else 0,
            "盤中成交量": safe_int(q.get("v"), 0) or 0,
            "價格來源": source,
            "AI總分": safe_float(m.get("AI總分"), 50) or 50,
            "風險分": safe_float(m.get("風險分"), 30) or 30,
            "資料來源": m.get("來源", "市場池"),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    order_map = {c: i for i, c in enumerate(order)}
    df["_order"] = df["代號"].map(order_map).fillna(9999)
    # Prefer real transaction price over fallback.
    df["_score"] = df["價格來源"].map({"成交價": 4, "最近成交價": 3, "買賣均價": 2, "最佳買價": 2, "最佳賣價": 2, "昨收參考": 1}).fillna(0)
    df = df.sort_values(["_order", "_score"], ascending=[True, False]).drop_duplicates("代號", keep="first")
    return df.drop(columns=["_order", "_score"], errors="ignore").reset_index(drop=True)


def classify_type(code: str, industry: str, market: str) -> str:
    if code.startswith("00"):
        return "ETF / 市場動能"
    if code in {"2330", "2317", "2382", "2308", "2454", "2881", "2882", "2891", "2892"}:
        return "中大型資金股"
    if market == "上櫃" or code in {"3441"}:
        return "小型強攻 / 漲停前兆股"
    if "金融" in str(industry):
        return "資金股 / 金融"
    return "動能股 / 先看資金"


def calc_decision(row: pd.Series) -> Dict[str, Any]:
    price = safe_float(row.get("盤中現價"), 0) or 0
    prev = safe_float(row.get("昨收"), 0) or 0
    pct = safe_float(row.get("盤中漲跌幅"), 0) or 0
    high = safe_float(row.get("最高"), price) or price
    low = safe_float(row.get("最低"), price) or price
    ai = safe_float(row.get("AI總分"), 50) or 50
    risk = safe_float(row.get("風險分"), 30) or 30
    vol = safe_float(row.get("盤中成交量"), 0) or 0

    # These are heuristic until full minute-level memory is available in background runner.
    capital_score = min(100, max(0, 45 + pct * 4 + (10 if vol > 2000 else 0) + (8 if ai >= 60 else 0)))
    left_score = min(100, max(0, 55 - max(pct, 0) * 2 + (10 if price > 0 and low > 0 and price <= low * 1.012 else 0) - max(0, risk - 50) * 0.5))
    limit_score = min(100, max(0, pct * 8 + (15 if high and price >= high * 0.995 else 0) + (20 if pct >= 7 else 0)))
    entry_score = round(0.35 * capital_score + 0.35 * left_score + 0.2 * limit_score + 0.1 * ai - 0.25 * max(0, risk - 40), 1)

    if price <= 0 or prev <= 0:
        state = "⚪ 無有效報價"
        decision = "觀察"
    elif pct >= 9.2:
        state = "🔴 接近漲停 / 不追"
        decision = "不可追"
    elif entry_score >= 72 and pct <= 5.5 and risk <= 55:
        state = "✅ 到價確認 / 可小量"
        decision = "高信心小量"
    elif capital_score >= 68 and pct <= 4.5:
        state = "👀 前兆出現"
        decision = "等低吸"
    elif pct >= 6:
        state = "🔴 已過熱 / 等回測"
        decision = "不可追"
    elif left_score >= 65 and risk <= 55:
        state = "⏳ 到價等確認"
        decision = "嚴格小量等待確認"
    else:
        state = "⚪ 候選 / 觀察"
        decision = "觀察"

    # Price plan: left zone = near current low-support area, not a stale old target.
    support = round(max(low, price * 0.985), 2) if price else 0
    stop = round(support * 0.985, 2) if support else 0
    add_price = round(max(high, price * 1.008), 2) if price else 0
    chase_limit = round(price * 1.018, 2) if price else 0
    stop_distance_pct = round((price - stop) / price * 100, 2) if price and stop else 0

    result = "追蹤中"
    if pct >= 9:
        result = "接近漲停/大漲"
    elif pct >= 2.5:
        result = "有效/上漲"
    elif pct <= -2:
        result = "弱勢/回撤"

    return {
        "目前狀態": state,
        "目前決策": decision,
        "結果分類": result,
        "盤後驗證結果": "盤中背景暫估",
        "驗證狀態": "背景追蹤中",
        "左側低吸分": round(left_score, 1),
        "盤中資金分": round(capital_score, 1),
        "漲停前兆分": round(limit_score, 1),
        "即時入場分": entry_score,
        "左側試單價": f"{support:.2f}",
        "防守停損": f"{stop:.2f}",
        "右側加碼價": f"{add_price:.2f}",
        "追價上限": f"{chase_limit:.2f}",
        "停損距離%": stop_distance_pct,
    }


def load_journal() -> pd.DataFrame:
    if not JOURNAL_FILE.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(JOURNAL_FILE, dtype=str).astype("object")
    except Exception:
        return pd.DataFrame()


def update_journal(live: pd.DataFrame) -> pd.DataFrame:
    now = now_tw()
    date_s = now.strftime("%Y-%m-%d")
    log = load_journal()
    if log.empty:
        log = pd.DataFrame()
    log = log.astype("object")

    existing_keys = set(str(x) for x in log.get("驗證Key", pd.Series(dtype=str)).tolist()) if not log.empty and "驗證Key" in log.columns else set()
    rows_to_append: List[Dict[str, Any]] = []

    for _, r in live.iterrows():
        code = str(r.get("代號", ""))
        if not code:
            continue
        key = f"{date_s}_{code}"
        d = calc_decision(r)
        price = safe_float(r.get("盤中現價"), 0) or 0
        base = {
            "驗證Key": key,
            "日期": date_s,
            "最新時間": now.strftime("%H:%M:%S"),
            "代號": code,
            "名稱": r.get("名稱", code),
            "股票型態": r.get("股票型態", ""),
            "目前狀態": d["目前狀態"],
            "目前決策": d["目前決策"],
            "結果分類": d["結果分類"],
            "盤後驗證結果": d["盤後驗證結果"],
            "驗證狀態": d["驗證狀態"],
            "驗證時間": now.strftime("%Y-%m-%d %H:%M:%S"),
            "首次價格": price,
            "驗證價格": price,
            "目前報酬%": 0,
            "驗證報酬%": 0,
            "最高報酬%": 0,
            "最大回撤%": 0,
            "驗證最高報酬%": 0,
            "驗證最大回撤%": 0,
            "AI總分": r.get("AI總分", 50),
            "風險分": r.get("風險分", 30),
            "左側低吸分": d["左側低吸分"],
            "盤中資金分": d["盤中資金分"],
            "漲停前兆分": d["漲停前兆分"],
            "即時入場分": d["即時入場分"],
            "左側試單價": d["左側試單價"],
            "防守停損": d["防守停損"],
            "右側加碼價": d["右側加碼價"],
            "追價上限": d["追價上限"],
            "停損距離%": d["停損距離%"],
            "資料來源": r.get("資料來源", "背景市場池"),
            "同步來源": "GitHub Actions background v2.15.4",
            "狀態歷程": d["目前狀態"],
        }
        if key in existing_keys and not log.empty:
            idxs = log.index[log["驗證Key"].astype(str) == key].tolist()
            if not idxs:
                rows_to_append.append(base)
                continue
            idx = idxs[0]
            first_price = safe_float(log.at[idx, "首次價格"] if "首次價格" in log.columns else price, price) or price
            current_ret = round((price - first_price) / first_price * 100, 2) if first_price else 0
            prev_high = safe_float(log.at[idx, "最高報酬%"] if "最高報酬%" in log.columns else 0, 0) or 0
            prev_dd = safe_float(log.at[idx, "最大回撤%"] if "最大回撤%" in log.columns else 0, 0) or 0
            high_ret = max(prev_high, current_ret)
            dd = min(prev_dd, current_ret)
            old_state = str(log.at[idx, "目前狀態"] if "目前狀態" in log.columns else "")
            history = str(log.at[idx, "狀態歷程"] if "狀態歷程" in log.columns else "")
            if old_state and old_state != base["目前狀態"] and base["目前狀態"] not in history:
                history = (history + " → " + base["目前狀態"]).strip(" →")
            base.update({
                "首次價格": first_price,
                "目前報酬%": current_ret,
                "驗證報酬%": current_ret,
                "最高報酬%": high_ret,
                "驗證最高報酬%": high_ret,
                "最大回撤%": dd,
                "驗證最大回撤%": dd,
                "狀態歷程": history or base["目前狀態"],
            })
            for col, val in base.items():
                if col not in log.columns:
                    log[col] = ""
                log.at[idx, col] = scalar(val)
        else:
            rows_to_append.append(base)
            existing_keys.add(key)

    if rows_to_append:
        log = pd.concat([log, pd.DataFrame(rows_to_append).astype("object")], ignore_index=True)
    # Keep object dtype and stable header.
    log = log.astype("object")
    log.to_csv(JOURNAL_FILE, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    return log


def post_rows_to_sheet(rows: List[Dict[str, Any]], url: str, batch_size: int = 20, timeout: int = 45) -> Dict[str, Any]:
    if not url:
        return {"ok": False, "message": "GSHEET_WEBHOOK_URL not set", "sent": 0}
    total_inserted = 0
    total_updated = 0
    total_skipped = 0
    chunks = list(chunked(rows, batch_size))
    messages = []
    for i, batch in enumerate(chunks, 1):
        payload = {"version": "v2.15.4-background", "chunk_no": i, "total_chunks": len(chunks), "rows": batch}
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            text = resp.text[:500]
            if resp.status_code != 200:
                return {"ok": False, "message": f"batch {i}/{len(chunks)} HTTP {resp.status_code}: {text}", "sent": sum(len(c) for c in chunks[:i-1])}
            try:
                data = resp.json()
            except Exception:
                data = {"ok": False, "error": text}
            if not data.get("ok"):
                return {"ok": False, "message": f"batch {i}/{len(chunks)} failed: {data}", "sent": sum(len(c) for c in chunks[:i-1])}
            total_inserted += int(data.get("inserted", 0) or 0)
            total_updated += int(data.get("updated", 0) or 0)
            total_skipped += int(data.get("skipped", 0) or 0)
            messages.append(f"{i}/{len(chunks)} ok")
            time.sleep(0.4)
        except Exception as exc:
            return {"ok": False, "message": f"batch {i}/{len(chunks)} exception: {exc}", "sent": sum(len(c) for c in chunks[:i-1])}
    return {"ok": True, "inserted": total_inserted, "updated": total_updated, "skipped": total_skipped, "sent": len(rows), "message": "; ".join(messages)}


def save_meta(meta: Dict[str, Any]) -> None:
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    dt = now_tw()
    mode = v216_session_mode(dt)
    force = os.getenv("FORCE_RUN", "").strip() == "1"
    if not force and not in_taiwan_market_window(dt):
        meta = {"updated_at": dt.isoformat(), "status": "skipped", "session_mode": mode, "reason": "outside v2.16 intraday/post-close learning window"}
        save_meta(meta)
        print(json.dumps(meta, ensure_ascii=False))
        return 0

    pool_limit = int(os.getenv("BACKGROUND_POOL_LIMIT", "180"))
    sync_limit = int(os.getenv("BACKGROUND_SYNC_LIMIT", "120"))
    webhook_url = os.getenv("GSHEET_WEBHOOK_URL", "").strip()

    pool = build_pool(pool_limit)
    channels, meta_map, order = build_ex_channels(pool)
    quotes = fetch_mis_quotes(channels)
    live = quote_rows(quotes, meta_map, order)
    if live.empty and INTRADAY_SNAPSHOT_FILE.exists():
        try:
            live = pd.read_csv(INTRADAY_SNAPSHOT_FILE, dtype=str)
            live["同步來源"] = "fallback data/intraday_snapshot.csv"
        except Exception:
            live = pd.DataFrame()

    if live.empty:
        meta = {"updated_at": dt.isoformat(), "status": "no_quotes", "pool_count": len(pool), "quote_raw_count": len(quotes)}
        save_meta(meta)
        print(json.dumps(meta, ensure_ascii=False))
        return 0

    journal = update_journal(live)
    # Sync latest rows only to avoid timeouts.
    sync_df = journal.tail(sync_limit).copy().astype("object") if not journal.empty else pd.DataFrame()
    rows = []
    for _, row in sync_df.iterrows():
        rows.append({str(k): scalar(v) for k, v in row.to_dict().items()})
    sync_result = post_rows_to_sheet(rows, webhook_url, batch_size=int(os.getenv("BACKGROUND_BATCH_SIZE", "20"))) if rows and webhook_url else {"ok": False, "message": "no webhook or no rows", "sent": 0}

    meta = {
        "updated_at": dt.isoformat(),
        "status": "ok",
        "session_mode": mode,
        "pool_count": len(pool),
        "quote_raw_count": len(quotes),
        "live_count": int(len(live)),
        "journal_count": int(len(journal)),
        "sync": sync_result,
    }
    save_meta(meta)
    try:
        if mode == "post_close_verify":
            POST_CLOSE_FILE.write_text(json.dumps({"updated_at": dt.isoformat(), "status": "ok", "session_mode": mode, "journal_count": int(len(journal)), "sync": sync_result}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    # Return non-zero only if quotes/journal succeeded but sync failed? We keep 0 to avoid noisy Actions failures.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

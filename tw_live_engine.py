# -*- coding: utf-8 -*-
"""
TW Stock AI Scanner v2.24｜Live Intraday Engine

Purpose
- GitHub Actions background runner for near-real-time Taiwan stock snapshots.
- Reads candidates from data/latest_rank.csv, plus focus codes.
- Fetches TWSE MIS quotes by trying both listed and OTC channels.
- Writes:
  - data/live_intraday.csv
  - data/live_intraday_meta.json
  - data/intraday_snapshot.csv        # compatibility with existing app.py
  - data/intraday_meta.json           # compatibility with existing app.py

This is not broker-grade tick data. It is a lightweight market snapshot for AI decision panels.
"""

from __future__ import annotations

import argparse
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

RANK_FILE = DATA_DIR / "latest_rank.csv"
LIVE_FILE = DATA_DIR / "live_intraday.csv"
LIVE_META_FILE = DATA_DIR / "live_intraday_meta.json"
INTRADAY_FILE = DATA_DIR / "intraday_snapshot.csv"
INTRADAY_META_FILE = DATA_DIR / "intraday_meta.json"

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

DEFAULT_FOCUS_META: Dict[str, Dict[str, str]] = {
    "3441": {"名稱": "聯一光", "市場": "上市", "產業": "光電業"},
    "2382": {"名稱": "廣達", "市場": "上市", "產業": "電子工業"},
    "2313": {"名稱": "華通", "市場": "上市", "產業": "電子工業"},
    "6770": {"名稱": "力積電", "市場": "上市", "產業": "半導體業"},
    "2409": {"名稱": "友達", "市場": "上市", "產業": "光電業"},
    "3042": {"名稱": "晶技", "市場": "上市", "產業": "電子零組件業"},
    "6257": {"名稱": "矽格", "市場": "上櫃", "產業": "半導體業"},
}

FALLBACK_POOL: List[Tuple[str, str, str, str]] = [
    ("2330", "台積電", "上市", "半導體業"),
    ("2317", "鴻海", "上市", "電子工業"),
    ("2454", "聯發科", "上市", "半導體業"),
    ("2382", "廣達", "上市", "電子工業"),
    ("2313", "華通", "上市", "電子工業"),
    ("3441", "聯一光", "上市", "光電業"),
    ("6770", "力積電", "上市", "半導體業"),
    ("2409", "友達", "上市", "光電業"),
    ("3231", "緯創", "上市", "電子工業"),
    ("6669", "緯穎", "上市", "電子工業"),
]


def now_tw() -> datetime:
    return datetime.now(TAIPEI)


def session_mode(dt: Optional[datetime] = None) -> str:
    dt = dt or now_tw()
    if dt.weekday() >= 5:
        return "weekend"
    m = dt.hour * 60 + dt.minute
    if 8 * 60 + 30 <= m < 9 * 60:
        return "pre_open"
    if 9 * 60 <= m <= 13 * 60 + 35:
        return "intraday"
    if 13 * 60 + 36 <= m <= 16 * 60:
        return "post_close"
    return "off_hours"


def normalize_code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    m = re.search(r"(\d{4,6})", text)
    if not m:
        return ""
    code = m.group(1)
    return code[:4] if len(code) >= 4 else code.zfill(4)


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(float(value)) else default
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in {"", "-", "--", "None", "nan", "NaN", "null"}:
        return default
    # TWSE MIS bid/ask may look like 12.3_12.4_...
    if "_" in text:
        for part in text.split("_"):
            f = safe_float(part, None)
            if f is not None:
                return f
        return default
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return default
    try:
        return float(m.group(0))
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    f = safe_float(value, None)
    if f is None:
        return default
    try:
        return int(f)
    except Exception:
        return default


def scalar(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value)


def split_codes(text: str) -> List[str]:
    out: List[str] = []
    for part in re.split(r"[,，\s]+", text or ""):
        code = normalize_code(part)
        if code and code not in out:
            out.append(code)
    return out


def read_rank_candidates(limit: int) -> List[Dict[str, Any]]:
    if not RANK_FILE.exists():
        return []
    try:
        df = pd.read_csv(RANK_FILE, dtype=str).head(max(limit, 1))
    except Exception as exc:
        print(f"[WARN] failed reading latest_rank.csv: {exc}")
        return []
    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        code = normalize_code(r.get("代號") or r.get("stock_id"))
        if not code:
            continue
        rows.append(
            {
                "代號": code,
                "名稱": str(r.get("名稱") or r.get("stock_name") or code),
                "市場": str(r.get("市場") or ""),
                "產業": str(r.get("產業") or r.get("industry_category") or "未知"),
                "AI總分": safe_float(r.get("AI總分") or r.get("AI分數") or r.get("總分"), 50) or 50,
                "風險分": safe_float(r.get("風險分"), 30) or 30,
                "技術分": safe_float(r.get("技術分"), 0) or 0,
                "籌碼分": safe_float(r.get("籌碼分"), 0) or 0,
                "來源": "latest_rank",
            }
        )
    return rows


def build_pool(limit: int, focus_codes: List[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    rows.extend(read_rank_candidates(limit))
    for code in focus_codes:
        meta = DEFAULT_FOCUS_META.get(code, {"名稱": code, "市場": "", "產業": "重點追蹤"})
        rows.append({"代號": code, **meta, "AI總分": 55, "風險分": 35, "技術分": 0, "籌碼分": 0, "來源": "focus"})
    for code, name, market, industry in FALLBACK_POOL:
        rows.append({"代號": code, "名稱": name, "市場": market, "產業": industry, "AI總分": 50, "風險分": 35, "技術分": 0, "籌碼分": 0, "來源": "fallback"})

    by_code: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        code = normalize_code(row.get("代號"))
        if not code:
            continue
        if code not in by_code:
            by_code[code] = row.copy()
            by_code[code]["代號"] = code
        else:
            old = by_code[code]
            # Preserve latest_rank scores if available, but fill empty metadata.
            for k, v in row.items():
                if old.get(k) in (None, "") and v not in (None, ""):
                    old[k] = v
    focus_set = set(focus_codes)
    items = list(by_code.values())
    items.sort(key=lambda r: (0 if r["代號"] in focus_set else 1, -float(safe_float(r.get("AI總分"), 0) or 0)))
    return items[: max(limit, len(focus_codes), 1)]


def chunked(items: List[Any], size: int) -> Iterable[List[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def build_channels(pool: List[Dict[str, Any]]) -> Tuple[List[str], Dict[str, Dict[str, Any]], List[str]]:
    channels: List[str] = []
    meta: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for r in pool:
        code = normalize_code(r.get("代號"))
        if not code:
            continue
        order.append(code)
        meta[code] = r
        # Try both markets. choose_best_rows will keep the valid one.
        channels.append(f"tse_{code}.tw")
        channels.append(f"otc_{code}.tw")
    seen: set[str] = set()
    uniq: List[str] = []
    for ch in channels:
        if ch not in seen:
            seen.add(ch)
            uniq.append(ch)
    return uniq, meta, order


def build_url(base: str, batch: List[str]) -> str:
    ex_ch = "%7c".join(quote(x, safe="_.") for x in batch)
    return f"{base}?ex_ch={ex_ch}&json=1&delay=0&_={int(time.time() * 1000)}"


def warmup(session: requests.Session, headers: Dict[str, str]) -> None:
    for url in MIS_WARMUP_URLS:
        try:
            session.get(url, headers=headers, timeout=8)
            time.sleep(0.12)
        except Exception:
            pass


def fetch_quotes(channels: List[str], batch_size: int = 24, retries: int = 3) -> Tuple[List[Dict[str, Any]], List[str]]:
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
        "Referer": "https://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }
    warmup(session, headers)
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    errors: List[str] = []
    for batch in chunked(channels, batch_size):
        batch_ok = False
        for attempt in range(retries):
            for base in MIS_URLS:
                try:
                    resp = session.get(build_url(base, batch), headers=headers, timeout=15)
                    if resp.status_code != 200:
                        errors.append(f"HTTP {resp.status_code} {base}")
                        continue
                    data = resp.json()
                    arr = data.get("msgArray", [])
                    if isinstance(arr, list) and arr:
                        for q in arr:
                            ch = str(q.get("ch") or "")
                            if ch and ch in seen:
                                continue
                            if ch:
                                seen.add(ch)
                            out.append(q)
                        batch_ok = True
                        break
                except Exception as exc:
                    errors.append(str(exc)[:120])
            if batch_ok:
                break
            time.sleep(0.5 + attempt * 0.6)
        time.sleep(0.2)
    return out, errors[-10:]


def extract_market(q: Dict[str, Any], fallback: str = "") -> str:
    ex = str(q.get("ex") or "").lower()
    ch = str(q.get("ch") or "").lower()
    if ex == "tse" or ch.startswith("tse_"):
        return "上市"
    if ex == "otc" or ch.startswith("otc_"):
        return "上櫃"
    return fallback or "未知"


def extract_price(q: Dict[str, Any]) -> Tuple[Optional[float], str, str]:
    for key, source, status in [("z", "成交價", "正常"), ("pz", "最近成交價", "最近成交價")]:
        price = safe_float(q.get(key), None)
        if price is not None:
            return price, source, status
    bid = safe_float(q.get("b"), None)
    ask = safe_float(q.get("a"), None)
    if bid is not None and ask is not None:
        return round((bid + ask) / 2, 2), "最佳買賣均價", "買賣價估算"
    if bid is not None:
        return bid, "最佳買價", "買價參考"
    if ask is not None:
        return ask, "最佳賣價", "賣價參考"
    prev = safe_float(q.get("y"), None)
    if prev is not None:
        return prev, "昨收參考", "無成交價，昨收參考"
    return None, "無", "無有效報價"


def quote_to_row(q: Dict[str, Any], meta_map: Dict[str, Dict[str, Any]], ts: str) -> Optional[Dict[str, Any]]:
    code = normalize_code(q.get("c"))
    if not code:
        return None
    meta = meta_map.get(code, {})
    price, price_source, status = extract_price(q)
    prev = safe_float(q.get("y"), None)
    open_p = safe_float(q.get("o"), None)
    high = safe_float(q.get("h"), None)
    low = safe_float(q.get("l"), None)
    pct = None
    change = None
    if price is not None and prev not in (None, 0):
        change = round(price - float(prev), 2)
        pct = round((price - float(prev)) / float(prev) * 100, 2)
    volume = safe_int(q.get("v"), 0)
    ai = safe_float(meta.get("AI總分"), 50) or 50
    risk = safe_float(meta.get("風險分"), 35) or 35
    tech = safe_float(meta.get("技術分"), 0) or 0
    chip = safe_float(meta.get("籌碼分"), 0) or 0
    live = calc_live_decision(price, prev, high, low, pct, volume, ai, risk, tech, chip)
    return {
        "快照時間": ts,
        "代號": code,
        "名稱": str(q.get("n") or meta.get("名稱") or code),
        "市場": extract_market(q, str(meta.get("市場") or "")),
        "產業": meta.get("產業", "未知"),
        "昨收": prev,
        "開盤": open_p,
        "最高": high,
        "最低": low,
        "盤中現價": price,
        "盤中漲跌": change,
        "盤中漲跌幅": pct,
        "盤中成交量": volume,
        "最近單量": safe_int(q.get("tv"), 0),
        "盤中時間": q.get("t", ""),
        "價格來源": price_source,
        "盤中狀態": status,
        "AI總分": ai,
        "技術分": tech,
        "籌碼分": chip,
        "風險分": risk,
        **live,
        "候選來源": meta.get("來源", "market_pool"),
    }


def calc_live_decision(
    price: Optional[float],
    prev: Optional[float],
    high: Optional[float],
    low: Optional[float],
    pct: Optional[float],
    volume: int,
    ai: float,
    risk: float,
    tech: float,
    chip: float,
) -> Dict[str, Any]:
    if not price or not prev:
        return {
            "盤中強度分": "",
            "即時決策": "觀察",
            "入場狀態": "⚪ 無有效即時價",
            "左側試單價": "",
            "右側確認價": "",
            "追價上限": "",
            "防守停損": "",
            "決策原因": "MIS 沒有有效成交價，不能用舊價硬判斷。",
        }
    pct = float(pct or 0)
    high = float(high or price)
    low = float(low or price)
    vol_bonus = 8 if volume >= 2000 else (4 if volume >= 800 else 0)
    pct_score = max(0, min(100, 50 + pct * 6))
    near_low_bonus = 8 if price <= low * 1.012 else 0
    near_high_bonus = 10 if high > 0 and price >= high * 0.992 else 0
    strength = round(max(0, min(100, ai * 0.42 + tech * 0.18 + chip * 0.16 + pct_score * 0.20 + vol_bonus - max(0, risk - 45) * 0.35 + near_high_bonus)), 1)

    left_price = round(max(low, price * 0.985), 2)
    right_price = round(max(high, price * 1.006), 2)
    chase_limit = round(price * 1.018, 2)
    stop = round(left_price * 0.982, 2)

    if pct >= 8.8:
        decision = "不可追"
        status = "🔴 接近漲停/過熱"
        reason = "漲幅已接近高風險區，寧可錯過，不用追高。"
    elif strength >= 72 and risk <= 55 and pct <= 5.5:
        decision = "可小量試單"
        status = "✅ 動能與分數同步"
        reason = "AI/技術/籌碼與盤中動能同步，且尚未過熱。"
    elif strength >= 62 and pct > 0:
        decision = "等站穩/回測"
        status = "🟡 有動能但不急追"
        reason = "盤中轉強，但要等回測不破或站回右側確認價。"
    elif pct <= -3:
        decision = "暫不進場"
        status = "⚪ 盤中轉弱"
        reason = "價格轉弱，先等止跌或尾盤確認。"
    else:
        decision = "觀察"
        status = "⚪ 訊號未完整"
        reason = "分數或價格結構還不夠一致。"

    return {
        "盤中強度分": strength,
        "即時決策": decision,
        "入場狀態": status,
        "左側試單價": f"{left_price:.2f}",
        "右側確認價": f"{right_price:.2f}",
        "追價上限": f"{chase_limit:.2f}",
        "防守停損": f"{stop:.2f}",
        "決策原因": reason,
    }


def choose_best(rows: List[Dict[str, Any]], order: List[str]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["代號"] = df["代號"].astype(str).map(normalize_code)
    order_map = {code: idx for idx, code in enumerate(order)}
    source_score = {"成交價": 5, "最近成交價": 4, "最佳買賣均價": 3, "最佳買價": 2, "最佳賣價": 2, "昨收參考": 1, "無": 0}
    df["_order"] = df["代號"].map(order_map).fillna(99999)
    df["_score"] = df["價格來源"].map(source_score).fillna(0)
    df = df.sort_values(["_order", "_score", "盤中成交量"], ascending=[True, False, False])
    df = df.drop_duplicates("代號", keep="first")
    return df.drop(columns=["_order", "_score"], errors="ignore").reset_index(drop=True)


def add_missing_rows(df: pd.DataFrame, meta_map: Dict[str, Dict[str, Any]], order: List[str], ts: str) -> pd.DataFrame:
    got = set(df["代號"].astype(str).map(normalize_code)) if not df.empty and "代號" in df.columns else set()
    missing: List[Dict[str, Any]] = []
    for code in order:
        if code in got:
            continue
        meta = meta_map.get(code, {})
        missing.append({
            "快照時間": ts,
            "代號": code,
            "名稱": meta.get("名稱", code),
            "市場": meta.get("市場", "未知"),
            "產業": meta.get("產業", "未知"),
            "昨收": "",
            "開盤": "",
            "最高": "",
            "最低": "",
            "盤中現價": "",
            "盤中漲跌": "",
            "盤中漲跌幅": "",
            "盤中成交量": "",
            "最近單量": "",
            "盤中時間": "",
            "價格來源": "無",
            "盤中狀態": "MIS 未回傳",
            "AI總分": meta.get("AI總分", 50),
            "技術分": meta.get("技術分", 0),
            "籌碼分": meta.get("籌碼分", 0),
            "風險分": meta.get("風險分", 35),
            "盤中強度分": "",
            "即時決策": "觀察",
            "入場狀態": "⚪ MIS 未回傳",
            "左側試單價": "",
            "右側確認價": "",
            "追價上限": "",
            "防守停損": "",
            "決策原因": "這一檔本次 MIS 沒有回傳，前台不得拿舊價格假裝更新。",
            "候選來源": meta.get("來源", "market_pool"),
        })
    if missing:
        df = pd.concat([df, pd.DataFrame(missing)], ignore_index=True)
    order_map = {code: idx for idx, code in enumerate(order)}
    df["_order"] = df["代號"].astype(str).map(normalize_code).map(order_map).fillna(99999)
    return df.sort_values("_order").drop(columns=["_order"], errors="ignore").reset_index(drop=True)


def write_outputs(df: pd.DataFrame, meta: Dict[str, Any]) -> None:
    # stable columns first
    first_cols = [
        "快照時間", "代號", "名稱", "市場", "產業", "盤中現價", "盤中漲跌幅", "盤中漲跌", "盤中成交量", "盤中時間",
        "AI總分", "盤中強度分", "即時決策", "入場狀態", "左側試單價", "右側確認價", "追價上限", "防守停損", "風險分", "決策原因",
        "昨收", "開盤", "最高", "最低", "最近單量", "價格來源", "盤中狀態", "技術分", "籌碼分", "候選來源",
    ]
    cols = [c for c in first_cols if c in df.columns] + [c for c in df.columns if c not in first_cols]
    df = df[cols]
    df.to_csv(LIVE_FILE, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    # compatibility with current app.py
    df.to_csv(INTRADAY_FILE, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    LIVE_META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    compat = {
        "updated_at": meta.get("updated_at"),
        "mode": "intraday_snapshot_v2_24_live",
        "source": "TWSE MIS getStockInfo.jsp",
        "candidate_count": meta.get("candidate_count", 0),
        "query_channel_count": meta.get("query_channel_count", 0),
        "quote_count": meta.get("quote_raw_count", 0),
        "quote_success_count": meta.get("valid_price_count", 0),
        "valid_price_count": meta.get("valid_price_count", 0),
        "trade_price_count": meta.get("trade_price_count", 0),
        "quote_failed_count": meta.get("quote_failed_count", 0),
        "note": "v2.24 live engine 寫入；若前台時間有更新但價格沒變，代表來源 MIS 該檔未回傳新成交價，不再假裝刷新。",
    }
    INTRADAY_META_FILE.write_text(json.dumps(compat, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=int(os.getenv("LIVE_LIMIT", "80")))
    parser.add_argument("--focus-codes", default=os.getenv("LIVE_FOCUS_CODES", "3441,2382,2313,6770,2409,3042,6257"))
    parser.add_argument("--batch-size", type=int, default=24)
    args = parser.parse_args(argv)

    ts_dt = now_tw()
    ts = ts_dt.isoformat(timespec="seconds")
    focus_codes = split_codes(args.focus_codes)
    pool = build_pool(args.limit, focus_codes)
    channels, meta_map, order = build_channels(pool)
    raw_quotes, errors = fetch_quotes(channels, batch_size=max(2, args.batch_size))
    rows = [r for q in raw_quotes if (r := quote_to_row(q, meta_map, ts)) is not None]
    df = choose_best(rows, order)
    df = add_missing_rows(df, meta_map, order, ts)

    valid_price_count = int(pd.to_numeric(df.get("盤中現價", pd.Series(dtype=str)), errors="coerce").notna().sum()) if not df.empty else 0
    trade_price_count = int(df.get("價格來源", pd.Series(dtype=str)).isin(["成交價", "最近成交價"]).sum()) if not df.empty else 0
    quote_failed_count = max(0, len(order) - valid_price_count)
    status = "ok" if valid_price_count > 0 else "no_valid_price"
    meta = {
        "updated_at": ts,
        "status": status,
        "session_mode": session_mode(ts_dt),
        "workflow_version": "v2.24-live-intraday",
        "candidate_count": len(order),
        "query_channel_count": len(channels),
        "quote_raw_count": len(raw_quotes),
        "valid_price_count": valid_price_count,
        "trade_price_count": trade_price_count,
        "quote_failed_count": quote_failed_count,
        "focus_codes": focus_codes,
        "errors_tail": errors,
        "note": "前台應以 data/live_intraday_meta.json 的 updated_at 判斷資料是否真的更新；不要只看 Streamlit rerun 時間。",
    }
    write_outputs(df, meta)
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

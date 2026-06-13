# -*- coding: utf-8 -*-
"""
TW Stock AI Scanner v2.16.9｜Market / night-session context builder

Purpose:
- Run from GitHub Actions without opening Streamlit.
- Build data/v216_market_context.json from Taiwan market breadth and Yahoo/global risk proxies.
- Build data/v216_night_session_context.json for night-session / next-day risk.
- Append data/v216_market_context_history.csv so the Streamlit page can show environment history.

Notes:
- This script never guesses if a source fails. It writes source_status and uses fallbacks.
- For futures/global proxies it uses Yahoo chart endpoints where available; if unavailable, context remains neutral.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

MARKET_CONTEXT_FILE = DATA_DIR / "v216_market_context.json"
NIGHT_CONTEXT_FILE = DATA_DIR / "v216_night_session_context.json"
POST_CLOSE_FILE = DATA_DIR / "v216_post_close_verification.json"
HISTORY_FILE = DATA_DIR / "v216_market_context_history.csv"
INTRADAY_SNAPSHOT_FILE = DATA_DIR / "intraday_snapshot.csv"
JOURNAL_FILE = DATA_DIR / "v215_verified_signal_journal.csv"
META_FILE = DATA_DIR / "v215_background_sync_meta.json"
TAIFEX_CACHE_FILE = DATA_DIR / "v216_taifex_last_valid.json"

TAIPEI = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def now_tw() -> datetime:
    return datetime.now(TAIPEI)


def session_mode(dt: datetime) -> str:
    if dt.weekday() >= 5:
        return "weekend"
    m = dt.hour * 60 + dt.minute
    if 8 * 60 + 45 <= m <= 13 * 60 + 35:
        return "intraday"
    if 13 * 60 + 36 <= m <= 16 * 60 + 30:
        return "post_close_verify"
    if m >= 16 * 60 + 31 or m <= 5 * 60 + 10:
        return "night_context"
    if 5 * 60 + 11 <= m < 8 * 60 + 45:
        return "pre_open"
    return "off_hours"


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if v is None:
            return default
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            f = float(v)
            return f if math.isfinite(f) else default
        s = str(v).strip().replace(",", "").replace("%", "")
        if s in {"", "-", "--", "None", "nan", "NaN"}:
            return default
        return float(s)
    except Exception:
        return default




def valid_taifex_price(value: Any) -> bool:
    """TXF/MTX near-month quote should never be 0 or a tiny value.
    If source parsing returns 0/blank, mark it invalid so the dashboard does
    not treat fake quotes as real futures prices.
    """
    v = safe_float(value, None)
    return bool(v is not None and 5000 <= v <= 80000)


def invalidate_taifex_quote(obj: Dict[str, Any], reason: str) -> Dict[str, Any]:
    obj = dict(obj or {})
    obj.update({
        "ok": False,
        "price": None,
        "previous_close": None,
        "change": None,
        "change_pct": None,
        "error": reason,
    })
    return obj


def _read_taifex_cache() -> Dict[str, Any]:
    try:
        if TAIFEX_CACHE_FILE.exists():
            data = json.loads(TAIFEX_CACHE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}

def _write_taifex_cache(result: Dict[str, Any]) -> None:
    """Persist the latest valid TXF/MTX quotes so weekend/off-hours UI can
    display the last real near-month quote instead of a misleading blank/0.
    """
    try:
        cache = _read_taifex_cache()
        for key in ["TXF", "MTX"]:
            q = (result or {}).get(key, {}) or {}
            if q.get("ok") and valid_taifex_price(q.get("price")) and not q.get("cached"):
                qq = dict(q)
                qq["cached_at"] = now_tw().strftime("%Y-%m-%d %H:%M:%S")
                cache[key] = qq
        if cache:
            TAIFEX_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _cached_taifex_quote(key: str, fallback_label: str, reason: str) -> Dict[str, Any]:
    cache = _read_taifex_cache()
    q = dict((cache.get(key) or {}))
    if q.get("ok") and valid_taifex_price(q.get("price")):
        q["label"] = q.get("label") or fallback_label
        q["cached"] = True
        q["ok"] = True
        q["source"] = "Cached last valid near-month futures quote"
        q["cache_reason"] = reason
        q["updated_at"] = q.get("updated_at") or q.get("cached_at")
        return q
    return invalidate_taifex_quote({"symbol": key, "label": fallback_label}, reason)

def read_csv_safe(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str)
    except Exception:
        return pd.DataFrame()


def fetch_yahoo(symbol: str, label: str, interval: str = "1m", range_: str = "1d") -> Dict[str, Any]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": range_, "interval": interval}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=10)
        r.raise_for_status()
        data = r.json()
        result = (data.get("chart", {}).get("result") or [None])[0]
        if not result:
            return {"symbol": symbol, "label": label, "ok": False, "error": "empty result"}
        meta = result.get("meta", {}) or {}
        price = safe_float(meta.get("regularMarketPrice"), None)
        prev = safe_float(meta.get("previousClose"), None)
        if price is None:
            quotes = result.get("indicators", {}).get("quote", [{}])[0]
            closes = [safe_float(x) for x in quotes.get("close", [])]
            closes = [x for x in closes if x is not None]
            if closes:
                price = closes[-1]
        change_pct = None
        if price is not None and prev not in (None, 0):
            change_pct = (price - prev) / prev * 100
        return {
            "symbol": symbol,
            "label": label,
            "ok": price is not None,
            "price": price,
            "previous_close": prev,
            "change_pct": change_pct,
            "currency": meta.get("currency", ""),
            "exchange": meta.get("exchangeName", meta.get("fullExchangeName", "")),
            "source": "Yahoo chart",
        }
    except Exception as exc:
        return {"symbol": symbol, "label": label, "ok": False, "error": str(exc), "source": "Yahoo chart"}



def _quote_from_finmind_row(row: Dict[str, Any], label: str, source_note: str) -> Dict[str, Any]:
    price = safe_float(row.get("close"), None)
    change = safe_float(row.get("change_price"), None)
    pct = safe_float(row.get("change_rate"), None)
    prev = None
    if price is not None and change is not None:
        prev = price - change
    symbol = str(row.get("futures_id") or row.get("symbol") or "").strip()
    if not valid_taifex_price(price):
        return {
            "ok": False,
            "symbol": symbol or "TXF",
            "label": label,
            "price": None,
            "previous_close": None,
            "change": None,
            "change_pct": None,
            "source": source_note,
            "error": f"invalid FinMind futures price: {price}",
        }
    return {
        "ok": True,
        "symbol": symbol or label,
        "label": label,
        "price": price,
        "previous_close": prev,
        "change": change,
        "change_pct": pct,
        "open": safe_float(row.get("open"), None),
        "high": safe_float(row.get("high"), None),
        "low": safe_float(row.get("low"), None),
        "average_price": safe_float(row.get("average_price"), None),
        "buy_price": safe_float(row.get("buy_price"), None),
        "sell_price": safe_float(row.get("sell_price"), None),
        "total_volume": safe_float(row.get("total_volume"), None),
        "volume": safe_float(row.get("volume"), None),
        "date": row.get("date"),
        "source": source_note,
        "updated_at": now_tw().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _select_finmind_near_month(rows: List[Dict[str, Any]], prefix: str) -> Optional[Dict[str, Any]]:
    """Select the nearest-month futures quote from FinMind snapshot rows.

    FinMind's futures snapshot usually includes a continuous/near-month row such
    as TXFR1. Prefer that. If it is absent, choose the valid quote with the
    largest total volume as a practical fallback.
    """
    if not rows:
        return None
    valid = []
    for r in rows:
        fid = str(r.get("futures_id") or r.get("symbol") or "").upper().strip()
        price = safe_float(r.get("close"), None)
        if not fid.startswith(prefix.upper()):
            continue
        if not valid_taifex_price(price):
            continue
        total_volume = safe_float(r.get("total_volume"), 0) or 0
        dt_txt = str(r.get("date") or "")
        score = 0
        # Continuous near month rows commonly end in R1, e.g. TXFR1.
        if fid == f"{prefix.upper()}R1":
            score += 1_000_000_000
        elif fid.endswith("R1"):
            score += 900_000_000
        # Prefer active contracts if no explicit R1 row exists.
        score += int(total_volume)
        valid.append((score, dt_txt, r))
    if not valid:
        return None
    valid.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return valid[0][2]


def fetch_finmind_near_month_futures(data_id: str, label: str) -> Dict[str, Any]:
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    url = "https://api.finmindtrade.com/api/v4/taiwan_futures_snapshot"
    params = {"data_id": data_id}
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(url, params=params, headers=headers, timeout=12)
        payload = r.json() if r.text else {}
        if not r.ok:
            return {
                "ok": False,
                "symbol": data_id,
                "label": label,
                "source": "FinMind taiwan_futures_snapshot",
                "error": f"HTTP {r.status_code}: {str(payload)[:180]}",
            }
        rows = payload.get("data") or []
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            rows = []
        chosen = _select_finmind_near_month(rows, data_id)
        if not chosen:
            return {
                "ok": False,
                "symbol": data_id,
                "label": label,
                "source": "FinMind taiwan_futures_snapshot",
                "error": f"no valid near-month quote rows for {data_id}; rows={len(rows)}",
            }
        out = _quote_from_finmind_row(chosen, label, "FinMind taiwan_futures_snapshot near-month")
        out["selection_rule"] = "prefer futures_id ending R1, else highest total_volume"
        out["requested_data_id"] = data_id
        return out
    except Exception as exc:
        return {
            "ok": False,
            "symbol": data_id,
            "label": label,
            "source": "FinMind taiwan_futures_snapshot",
            "error": str(exc),
        }



def fetch_finmind_futures_daily_close(data_id: str, label: str, days: int = 45) -> Dict[str, Any]:
    """Fetch the latest TXF/MTX near-month daily close / settlement as a holiday fallback.

    On weekends or after the real-time futures endpoint is unavailable, TXF still
    has a last official close / settlement from the most recent trading day.
    This function uses FinMind TaiwanFuturesDaily and returns that value instead
    of leaving the dashboard blank or using a fake 0.00.
    """
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    end_dt = now_tw().date()
    start_dt = end_dt - timedelta(days=days)
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanFuturesDaily",
        "data_id": data_id,
        "start_date": start_dt.isoformat(),
        "end_date": end_dt.isoformat(),
    }
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        payload = r.json() if r.text else {}
        if not r.ok:
            return {"ok": False, "symbol": data_id, "label": label, "source": "FinMind TaiwanFuturesDaily last close", "error": f"HTTP {r.status_code}: {str(payload)[:180]}"}
        rows = payload.get("data") or []
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list) or not rows:
            return {"ok": False, "symbol": data_id, "label": label, "source": "FinMind TaiwanFuturesDaily last close", "error": "empty daily rows"}

        candidates = []
        for rr in rows:
            if not isinstance(rr, dict):
                continue
            fid = str(rr.get("futures_id") or rr.get("symbol") or rr.get("data_id") or data_id).upper().strip()
            if fid and not fid.startswith(data_id.upper()):
                continue
            # Prefer regular day-session rows when the field exists, but do not
            # discard unknown sessions because FinMind column names vary.
            session_txt = str(rr.get("trading_session") or rr.get("session") or rr.get("交易時段") or "").strip()
            price = None
            for col in ["settlement_price", "settlement", "close", "收盤價", "結算價"]:
                price = safe_float(rr.get(col), None)
                if valid_taifex_price(price):
                    break
            if not valid_taifex_price(price):
                continue
            vol = safe_float(rr.get("volume") or rr.get("total_volume") or rr.get("交易口數"), 0) or 0
            date_txt = str(rr.get("date") or rr.get("Date") or "")
            contract = str(rr.get("contract_date") or rr.get("delivery_month") or rr.get("交割月份") or "")
            score = 0
            # Near-month continuous/front-month rows are preferred when present.
            if fid == f"{data_id.upper()}R1" or fid.endswith("R1"):
                score += 1_000_000_000
            # A regular day session close is better for holiday reference than a
            # night-session transient value.
            if any(k in session_txt for k in ["一般", "日盤", "regular", "day"]):
                score += 50_000_000
            score += int(vol)
            candidates.append((date_txt, score, contract, rr, float(price)))
        if not candidates:
            return {"ok": False, "symbol": data_id, "label": label, "source": "FinMind TaiwanFuturesDaily last close", "error": "no valid daily close / settlement quote"}
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        date_txt, score, contract, row, price = candidates[0]

        # Previous valid close for percentage reference.
        prev = None
        for d2, score2, contract2, row2, price2 in candidates[1:]:
            if d2 < date_txt:
                prev = price2
                break
        change = (price - prev) if prev else None
        pct = (change / prev * 100) if prev not in (None, 0) else None
        return {
            "ok": True,
            "symbol": str(row.get("futures_id") or data_id),
            "label": label,
            "price": price,
            "previous_close": prev,
            "change": change,
            "change_pct": pct,
            "date": date_txt,
            "contract_date": contract,
            "price_type": "last_close",
            "cached": False,
            "source": "FinMind TaiwanFuturesDaily last close / settlement",
            "updated_at": now_tw().strftime("%Y-%m-%d %H:%M:%S"),
            "note": "休市/週末/即時源失敗時使用最近交易日收盤或結算價",
        }
    except Exception as exc:
        return {"ok": False, "symbol": data_id, "label": label, "source": "FinMind TaiwanFuturesDaily last close", "error": str(exc)}




def fetch_yahoo_wtx_direct() -> Dict[str, Any]:
    """Fetch 台指期近一 directly from Yahoo's dedicated WTX& page.

    The earlier overview-page parser could accidentally pick the cash TAIEX
    value (加權指數). This direct parser only reads the dedicated
    https://tw.stock.yahoo.com/future/WTX%26 page and explicitly searches the
    台指期近一 / WTX& block for 成交、漲跌、漲幅、昨收.
    """
    url = "https://tw.stock.yahoo.com/future/WTX%26"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        if not (r.ok and r.text):
            return {"ok": False, "error": f"Yahoo WTX direct HTTP {getattr(r, 'status_code', 'NA')}", "source": "Yahoo WTX& direct"}
        flat = re.sub(r"<[^>]+>", " ", r.text)
        flat = re.sub(r"\s+", " ", flat)
        # Restrict parsing to the WTX& dedicated quote block. The page also
        # contains a comparison table with the cash index; do not use that table
        # as the primary price source.
        idxs = [i for i in [flat.find("# 台指期近一"), flat.find("WTX&"), flat.find("台指期近一")] if i >= 0]
        start = min(idxs) if idxs else 0
        window = flat[start:start + 2600]

        def first_num_after(label: str) -> Optional[float]:
            m = re.search(re.escape(label) + r"\s*([+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|[+-]?\d+(?:\.\d+)?)", window)
            return safe_float(m.group(1), None) if m else None

        # Best source: detail field named 成交. Fallback: hero price immediately
        # after WTX& / 台指期近一.
        price_f = first_num_after("成交")
        if not valid_taifex_price(price_f):
            m = re.search(r"WTX&\s+([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d+)?)", window)
            price_f = safe_float(m.group(1), None) if m else None
        if not valid_taifex_price(price_f):
            return {"ok": False, "error": "Yahoo WTX direct found no valid 成交 price", "source": "Yahoo WTX& direct", "parse_window": window[:300]}

        change_f = first_num_after("漲跌")
        pct_f = first_num_after("漲幅")
        prev_f = first_num_after("昨收")
        bid_f = first_num_after("買價")
        ask_f = first_num_after("賣價")
        open_f = first_num_after("開盤")
        high_f = first_num_after("最高")
        low_f = first_num_after("最低")
        volume_f = first_num_after("總量")
        updated = None
        mtime = re.search(r"(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})\s+更新", window)
        if mtime:
            updated = mtime.group(1).replace("/", "-")
        return {
            "ok": True,
            "symbol": "WTX&",
            "label": "台指期近月",
            "price": price_f,
            "previous_close": prev_f if valid_taifex_price(prev_f) else (price_f - change_f if change_f is not None else None),
            "change": change_f,
            "change_pct": pct_f,
            "bid": bid_f,
            "ask": ask_f,
            "open": open_f,
            "high": high_f,
            "low": low_f,
            "volume": volume_f,
            "source": "Yahoo WTX& direct page",
            "source_url": url,
            "updated_at": updated or now_tw().strftime("%Y-%m-%d %H:%M:%S"),
            "price_type": "live_or_last_close",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "source": "Yahoo WTX& direct"}

def fetch_tw_yahoo_futures() -> Dict[str, Any]:
    """Fetch Taiwan index futures near-month quote.

    Primary source: FinMind taiwan_futures_snapshot data_id=TXF, selecting the
    near-month / continuous front-month row such as TXFR1.
    Fallback source: Yahoo Taiwan futures page. If all sources fail, keep the
    quote invalid instead of showing fake 0.00.
    """
    result = {
        "TXF": {"symbol": "WTX&", "label": "台指期近月", "ok": False, "source": "Yahoo WTX& direct page"},
        "MTX": {"symbol": "MTX", "label": "小台近月", "ok": False, "source": "FinMind taiwan_futures_snapshot"},
    }

    # 1) Primary: Yahoo's dedicated WTX& page. This is exactly the 台指期近月
    # source requested by the user: https://tw.stock.yahoo.com/future/WTX&
    # It prevents accidentally using the cash weighted index as 台指期.
    txf_wtx = fetch_yahoo_wtx_direct()
    if txf_wtx.get("ok"):
        result["TXF"].update(txf_wtx)
    else:
        result["TXF"].update({"wtx_direct_error": txf_wtx.get("error"), "source": txf_wtx.get("source", "Yahoo WTX& direct page")})

    # 2) Secondary: FinMind futures snapshot, only if Yahoo WTX& direct page did
    # not return a valid near-month quote.
    txf_fm = fetch_finmind_near_month_futures("TXF", "台指期近月")
    if (not result["TXF"].get("ok")) and txf_fm.get("ok"):
        result["TXF"].update(txf_fm)
    elif not txf_fm.get("ok"):
        result["TXF"].update({"finmind_error": txf_fm.get("error")})

    # Small TAIEX futures if available. If unavailable, leave it blank; TXF is the key signal.
    mtx_fm = fetch_finmind_near_month_futures("MTX", "小台近月")
    if mtx_fm.get("ok"):
        result["MTX"].update(mtx_fm)
    else:
        # Some data vendors expose micro futures as TMF; try it as a secondary display only.
        tmf_fm = fetch_finmind_near_month_futures("TMF", "微型台指近月")
        if tmf_fm.get("ok"):
            result["MTX"].update(tmf_fm)
        else:
            result["MTX"].update({"finmind_error": mtx_fm.get("error"), "tmf_error": tmf_fm.get("error")})

    # 2) Fallback: Yahoo Taiwan futures page, useful when FinMind token/sponsor is unavailable.
    if result["TXF"].get("ok") and result["MTX"].get("ok"):
        return result

    urls = [
        "https://tw.stock.yahoo.com/future/WTX%26",
        "https://tw.stock.yahoo.com/future",
        "https://tw.stock.yahoo.com/future/futures.html",
    ]

    def clean_num(x: str) -> Optional[float]:
        return safe_float(str(x).replace(",", ""), None)

    try:
        text = ""
        for url in urls:
            try:
                r = requests.get(url, headers={"User-Agent": UA}, timeout=12)
                if r.ok and r.text:
                    text = r.text
                    break
            except Exception:
                continue
        if text:
            flat = re.sub(r"<[^>]+>", " ", text)
            flat = re.sub(r"\s+", " ", flat)
            def parse_yahoo_contract(key: str, label: str, name_patterns: List[str], symbol_hint: str) -> Optional[Dict[str, Any]]:
                """Robustly parse Yahoo Taiwan futures rows.

                Yahoo's HTML changes frequently. The stable information is the
                row text around labels such as 台指期近一 / WTX&. In that row the
                common order is: bid, ask, last, change, change_pct, volume,
                open, high, low, spread, reference, open_interest, time. This
                parser is deliberately tolerant and falls back to bid/ask
                midpoint if a traded last price is not present.
                """
                starts = []
                for pat in name_patterns + [symbol_hint]:
                    if not pat:
                        continue
                    i = flat.find(pat)
                    if i >= 0:
                        starts.append(i)
                if not starts:
                    return None
                start = min(starts)
                window = flat[start:start + 1800]
                # Keep percentages as separate tokens; normal numbers are prices/volumes.
                tokens = re.findall(r"[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?%?|[+-]?\d+(?:\.\d+)?%?", window)
                nums = []
                pcts = []
                for t in tokens:
                    if t.endswith('%'):
                        val = clean_num(t[:-1])
                        if val is not None:
                            pcts.append(val)
                    else:
                        val = clean_num(t)
                        if val is not None:
                            nums.append(val)
                # Remove clearly impossible small values before the first price cluster.
                price_like = [x for x in nums if valid_taifex_price(x)]
                if not price_like:
                    return None
                bid = ask = None
                price_f = None
                change_f = None
                pct_f = pcts[0] if pcts else None
                # Normal Yahoo row: bid, ask, last, change, ...
                if len(price_like) >= 3:
                    bid, ask, price_f = price_like[0], price_like[1], price_like[2]
                elif len(price_like) == 2:
                    bid, ask = price_like[0], price_like[1]
                    price_f = round((bid + ask) / 2, 2)
                else:
                    price_f = price_like[0]
                # Find the first plausible non-price signed/short number after the price cluster as change.
                short_nums = [x for x in nums if abs(x) < 2000]
                if short_nums:
                    # Avoid choosing pct-like values if a pct token already exists; first valid is usually change.
                    change_f = short_nums[0]
                prev = price_f - change_f if price_f is not None and change_f is not None else None
                return {
                    "ok": valid_taifex_price(price_f),
                    "symbol": symbol_hint,
                    "label": label,
                    "price": price_f if valid_taifex_price(price_f) else None,
                    "previous_close": prev if valid_taifex_price(price_f) else None,
                    "change": change_f if valid_taifex_price(price_f) else None,
                    "change_pct": pct_f if valid_taifex_price(price_f) else None,
                    "bid": bid,
                    "ask": ask,
                    "source": "Yahoo Taiwan futures page robust fallback",
                    "updated_at": now_tw().strftime("%Y-%m-%d %H:%M:%S"),
                    "parse_window": window[:260],
                }

            yahoo_contracts = [
                ("TXF", "台指期近月", ["台指期近一", "台指期近月", "臺指期近一", "臺指期近月"], "WTX&"),
                ("MTX", "小台近月", ["小台指近一", "小台近一", "小臺指近一", "小臺近一"], "MTX&"),
            ]
            for key, label, names, symbol_hint in yahoo_contracts:
                if result.get(key, {}).get("ok"):
                    continue
                parsed_quote = parse_yahoo_contract(key, label, names, symbol_hint)
                if parsed_quote:
                    if not parsed_quote.get("ok"):
                        parsed_quote.update({"error": f"invalid parsed futures price: {parsed_quote.get('price')}"})
                    result[key].update(parsed_quote)
    except Exception as exc:
        if not result["TXF"].get("ok"):
            result["TXF"].update({"ok": False, "error": str(exc)})

    # 3) Official last close / settlement fallback.
    # On weekends and holidays TXF has no live trade, but it still has the most
    # recent official close / settlement. Use that before falling back to local cache.
    if not result["TXF"].get("ok"):
        # FinMind daily futures historically uses TX for TAIEX futures, while
        # some realtime feeds use TXF/TXFR1. Try TX first for official last
        # close / settlement, then TXF as a compatibility fallback.
        last_err = None
        for code in ["TX", "TXF"]:
            txf_close = fetch_finmind_futures_daily_close(code, "台指期近月")
            if txf_close.get("ok"):
                txf_close["symbol"] = txf_close.get("symbol") or code
                txf_close["requested_data_id"] = code
                result["TXF"] = txf_close
                break
            last_err = txf_close.get("error")
        if not result["TXF"].get("ok"):
            result["TXF"].update({"daily_close_error": last_err})
    if not result["MTX"].get("ok"):
        last_err = None
        for code in ["MTX", "MXF", "MX"]:
            mtx_close = fetch_finmind_futures_daily_close(code, "小台近月")
            if mtx_close.get("ok"):
                mtx_close["symbol"] = mtx_close.get("symbol") or code
                mtx_close["requested_data_id"] = code
                result["MTX"] = mtx_close
                break
            last_err = mtx_close.get("error")
        if not result["MTX"].get("ok"):
            result["MTX"].update({"daily_close_error": last_err})

    # 4) Last local cache fallback if even official daily close is unavailable.
    # This is marked as cached so the UI can distinguish it from live / official close.
    if not result["TXF"].get("ok"):
        reason = result.get("TXF", {}).get("daily_close_error") or result.get("TXF", {}).get("finmind_error") or result.get("TXF", {}).get("error") or "no valid TXF near-month quote"
        result["TXF"] = _cached_taifex_quote("TXF", "台指期近月", reason)
    if not result["MTX"].get("ok"):
        reason = result.get("MTX", {}).get("daily_close_error") or result.get("MTX", {}).get("finmind_error") or result.get("MTX", {}).get("error") or "no valid MTX near-month quote"
        result["MTX"] = _cached_taifex_quote("MTX", "小台近月", reason)

    _write_taifex_cache(result)
    return result

def market_breadth_from_files() -> Dict[str, Any]:
    df = read_csv_safe(INTRADAY_SNAPSHOT_FILE)
    src = "data/intraday_snapshot.csv"
    if df.empty:
        df = read_csv_safe(JOURNAL_FILE)
        src = "data/v215_verified_signal_journal.csv"
    if df.empty:
        return {"ok": False, "source": src, "message": "no local stock snapshot"}
    pct_col = None
    for c in ["盤中漲跌幅", "驗證報酬%", "目前報酬%", "報酬%"]:
        if c in df.columns:
            pct_col = c
            break
    if not pct_col:
        return {"ok": False, "source": src, "message": "no pct column", "rows": len(df)}
    pct = pd.to_numeric(df[pct_col].astype(str).str.replace("%", "", regex=False), errors="coerce")
    pct = pct.dropna()
    if pct.empty:
        return {"ok": False, "source": src, "message": "no numeric pct", "rows": len(df)}
    up = int((pct > 0).sum())
    down = int((pct < 0).sum())
    flat = int((pct == 0).sum())
    total = int(len(pct))
    up_ratio = up / total if total else 0
    return {
        "ok": True,
        "source": src,
        "rows": int(len(df)),
        "valid_pct_rows": total,
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "up_ratio": round(up_ratio, 4),
        "avg_pct": round(float(pct.mean()), 4),
        "median_pct": round(float(pct.median()), 4),
        "max_pct": round(float(pct.max()), 4),
        "min_pct": round(float(pct.min()), 4),
    }


def compute_context(dt: datetime) -> Dict[str, Any]:
    mode = session_mode(dt)
    twii = fetch_yahoo("^TWII", "加權指數")
    twoii = fetch_yahoo("^TWOII", "櫃買指數")
    taiwan_futures = fetch_tw_yahoo_futures()
    txf = taiwan_futures.get("TXF", {}) or {}
    nq = fetch_yahoo("NQ=F", "NASDAQ 100 期貨")
    es = fetch_yahoo("ES=F", "S&P 500 期貨")
    sox = fetch_yahoo("^SOX", "費半指數")
    tnx = fetch_yahoo("^TNX", "美10年殖利率")
    dxy = fetch_yahoo("DX-Y.NYB", "美元指數")
    oil = fetch_yahoo("CL=F", "WTI 原油期貨")
    breadth = market_breadth_from_files()

    twii_pct = safe_float(twii.get("change_pct"), 0.0) or 0.0
    twoii_pct = safe_float(twoii.get("change_pct"), 0.0) or 0.0
    txf_pct = safe_float(txf.get("change_pct"), 0.0) or 0.0
    breadth_avg = safe_float(breadth.get("avg_pct"), 0.0) if breadth.get("ok") else 0.0
    breadth_up = safe_float(breadth.get("up_ratio"), 0.5) if breadth.get("ok") else 0.5

    # 50 is neutral. Keep conservative so a single failed source does not distort decisions.
    market_env_score = 50.0
    market_env_score += clamp(twii_pct * 7.0, -14, 14)
    market_env_score += clamp(txf_pct * 5.0, -10, 10)
    market_env_score += clamp(twoii_pct * 6.0, -12, 12)
    market_env_score += clamp((breadth_up - 0.5) * 35.0, -10, 10)
    market_env_score += clamp((breadth_avg or 0.0) * 3.0, -8, 8)
    market_env_score = round(clamp(market_env_score, 0, 100), 1)

    nq_pct = safe_float(nq.get("change_pct"), 0.0) or 0.0
    es_pct = safe_float(es.get("change_pct"), 0.0) or 0.0
    sox_pct = safe_float(sox.get("change_pct"), 0.0) or 0.0
    tnx_pct = safe_float(tnx.get("change_pct"), 0.0) or 0.0
    dxy_pct = safe_float(dxy.get("change_pct"), 0.0) or 0.0
    oil_pct = safe_float(oil.get("change_pct"), 0.0) or 0.0

    night_risk_score = 50.0
    night_risk_score -= clamp(nq_pct * 7.0, -14, 14)
    night_risk_score -= clamp(es_pct * 5.0, -10, 10)
    night_risk_score -= clamp(sox_pct * 5.0, -10, 10)
    night_risk_score += clamp(max(0.0, -nq_pct) * 6.0, 0, 12)
    night_risk_score += clamp(max(0.0, -sox_pct) * 5.0, 0, 10)
    night_risk_score += clamp(max(0.0, tnx_pct) * 1.5, 0, 6)
    night_risk_score += clamp(max(0.0, dxy_pct) * 2.0, 0, 5)
    # Oil jump is a mild macro risk input, not always bearish for all stocks.
    night_risk_score += clamp(max(0.0, oil_pct - 1.0) * 1.0, 0, 5)
    night_risk_score = round(clamp(night_risk_score, 0, 100), 1)

    if market_env_score >= 62:
        market_label = "🟢 大盤偏多"
        market_action = "可維持左側試單，但仍看停損距離。"
    elif market_env_score >= 48:
        market_label = "🟡 大盤震盪"
        market_action = "只做低風險左側，到價仍需確認。"
    elif market_env_score >= 35:
        market_label = "🔴 大盤偏弱"
        market_action = "個股可試單訊號降級，嚴格小量。"
    else:
        market_label = "⚫ 系統性風險"
        market_action = "不追價，等待大盤止跌或隔日確認。"

    if night_risk_score >= 68:
        night_label = "🔴 夜盤風險高"
        next_day = "隔日開盤風險偏高，高分股也要降級。"
    elif night_risk_score >= 55:
        night_label = "🟡 夜盤偏保守"
        next_day = "隔日先看開盤承接，不急追。"
    elif night_risk_score >= 42:
        night_label = "⚪ 夜盤中性"
        next_day = "隔日依個股與族群強弱判斷。"
    else:
        night_label = "🟢 夜盤偏多"
        next_day = "隔日 AI / 半導體 / 資金股承接機率較高。"

    context = {
        "updated_at": dt.isoformat(),
        "session_mode": mode,
        "market_env_score": market_env_score,
        "market_label": market_label,
        "market_action": market_action,
        "night_risk_score": night_risk_score,
        "night_label": night_label,
        "next_day_note": next_day,
        "breadth": breadth,
        "indices": {"TWII": twii, "TWOII": twoii},
        "taiwan_futures": taiwan_futures,
        "night_proxies": {"TXF": taiwan_futures.get("TXF", {}), "MTX": taiwan_futures.get("MTX", {}), "NQ=F": nq, "ES=F": es, "SOX": sox, "TNX": tnx, "DXY": dxy, "CL=F": oil},
        "source_status": {
            "twii_ok": bool(twii.get("ok")),
            "twoii_ok": bool(twoii.get("ok")),
            "txf_ok": bool(taiwan_futures.get("TXF", {}).get("ok")),
            "nq_ok": bool(nq.get("ok")),
            "es_ok": bool(es.get("ok")),
            "sox_ok": bool(sox.get("ok")),
            "breadth_ok": bool(breadth.get("ok")),
        },
    }
    return context


def save_json(path: Path, obj: Dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def append_history(ctx: Dict[str, Any]) -> None:
    row = {
        "updated_at": ctx.get("updated_at"),
        "session_mode": ctx.get("session_mode"),
        "market_env_score": ctx.get("market_env_score"),
        "market_label": ctx.get("market_label"),
        "night_risk_score": ctx.get("night_risk_score"),
        "night_label": ctx.get("night_label"),
        "twii_pct": (ctx.get("indices", {}).get("TWII", {}) or {}).get("change_pct"),
        "twoii_pct": (ctx.get("indices", {}).get("TWOII", {}) or {}).get("change_pct"),
        "txf_price": (ctx.get("taiwan_futures", {}).get("TXF", {}) or {}).get("price"),
        "txf_pct": (ctx.get("taiwan_futures", {}).get("TXF", {}) or {}).get("change_pct"),
        "nq_pct": (ctx.get("night_proxies", {}).get("NQ=F", {}) or {}).get("change_pct"),
        "sox_pct": (ctx.get("night_proxies", {}).get("SOX", {}) or {}).get("change_pct"),
        "breadth_up_ratio": (ctx.get("breadth", {}) or {}).get("up_ratio"),
        "breadth_avg_pct": (ctx.get("breadth", {}) or {}).get("avg_pct"),
    }
    old = read_csv_safe(HISTORY_FILE)
    new = pd.DataFrame([row])
    if old.empty:
        out = new
    else:
        out = pd.concat([old, new], ignore_index=True)
        out = out.drop_duplicates(subset=["updated_at"], keep="last").tail(500)
    out.to_csv(HISTORY_FILE, index=False, encoding="utf-8-sig")


def main() -> int:
    dt = now_tw()
    ctx = compute_context(dt)
    save_json(MARKET_CONTEXT_FILE, ctx)
    # Night context is the same payload but explicitly named for the front-end / later scripts.
    save_json(NIGHT_CONTEXT_FILE, {
        "updated_at": ctx.get("updated_at"),
        "session_mode": ctx.get("session_mode"),
        "night_risk_score": ctx.get("night_risk_score"),
        "night_label": ctx.get("night_label"),
        "next_day_note": ctx.get("next_day_note"),
        "night_proxies": ctx.get("night_proxies", {}),
        "source_status": ctx.get("source_status", {}),
    })
    if ctx.get("session_mode") == "post_close_verify":
        save_json(POST_CLOSE_FILE, {
            "updated_at": ctx.get("updated_at"),
            "status": "market_context_post_close_written",
            "market_env_score": ctx.get("market_env_score"),
            "market_label": ctx.get("market_label"),
            "breadth": ctx.get("breadth", {}),
        })
    append_history(ctx)
    print(json.dumps(ctx, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

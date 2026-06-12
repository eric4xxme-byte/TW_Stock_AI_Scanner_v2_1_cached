# -*- coding: utf-8 -*-
"""
TW Stock AI Scanner v2.16.4｜Market / night-session context builder

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
    return bool(v is not None and v >= 1000)


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


def fetch_tw_yahoo_futures() -> Dict[str, Any]:
    """Fetch Taiwan futures from Yahoo Taiwan futures page.

    The Yahoo Finance global chart symbol for Taiwan futures is not stable for
    near-month/night-session quotes. Yahoo Taiwan futures page usually exposes
    WTX& (台指期近一) and related contracts in page text. This parser is deliberately
    defensive: if the page layout changes, it falls back to IX0126.TW so the
    dashboard does not break.
    """
    result = {
        "TXF": {"symbol": "WTX&", "label": "台指期近一", "ok": False, "source": "Yahoo Taiwan futures"},
        "MTX": {"symbol": "MTX&", "label": "小台近一", "ok": False, "source": "Yahoo Taiwan futures"},
    }
    urls = [
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
            # Remove tags enough for regex scanning while preserving order.
            flat = re.sub(r"<[^>]+>", " ", text)
            flat = re.sub(r"\s+", " ", flat)
            contracts = [
                ("TXF", "台指期近一", r"台指期近一\s*WTX[^0-9-]*([0-9,]+(?:\.[0-9]+)?)\s*([0-9,]+(?:\.[0-9]+)?)\s*([0-9,]+(?:\.[0-9]+)?)\s*([+-]?[0-9,]+(?:\.[0-9]+)?)\s*([+-]?[0-9]+(?:\.[0-9]+)?)%"),
                ("MTX", "小台近一", r"小台(?:指)?近一\s*[^0-9-]*([0-9,]+(?:\.[0-9]+)?)\s*([0-9,]+(?:\.[0-9]+)?)\s*([0-9,]+(?:\.[0-9]+)?)\s*([+-]?[0-9,]+(?:\.[0-9]+)?)\s*([+-]?[0-9]+(?:\.[0-9]+)?)%"),
            ]
            for key, label, pattern in contracts:
                m = re.search(pattern, flat)
                if m:
                    bid, ask, price, change, pct = m.groups()[:5]
                    price_f = clean_num(price)
                    change_f = clean_num(change)
                    pct_f = clean_num(pct)
                    prev = price_f - change_f if price_f is not None and change_f is not None else None
                    parsed_quote = {
                        "ok": valid_taifex_price(price_f),
                        "price": price_f if valid_taifex_price(price_f) else None,
                        "previous_close": prev if valid_taifex_price(price_f) else None,
                        "change": change_f if valid_taifex_price(price_f) else None,
                        "change_pct": pct_f if valid_taifex_price(price_f) else None,
                        "bid": clean_num(bid),
                        "ask": clean_num(ask),
                        "source": "Yahoo Taiwan futures page",
                        "updated_at": now_tw().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    if not valid_taifex_price(price_f):
                        parsed_quote.update({"error": f"invalid parsed futures price: {price_f}"})
                    result[key].update(parsed_quote)
        # Backup: TIP TAIFEX index. This is not the near-month tradable contract, but better than blank.
        if not result["TXF"].get("ok"):
            backup = fetch_yahoo("IX0126.TW", "TIP TAIFEX TAIEX Futures Index", interval="1m", range_="1d")
            if backup.get("ok") and valid_taifex_price(backup.get("price")):
                result["TXF"].update(backup)
                result["TXF"].update({"symbol": "IX0126.TW", "label": "台指期參考指數", "source": "Yahoo chart backup: IX0126.TW"})
            else:
                result["TXF"] = invalidate_taifex_quote(result.get("TXF", {}), "no valid TXF quote from Yahoo Taiwan page or backup")
    except Exception as exc:
        result["TXF"].update({"ok": False, "error": str(exc)})
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

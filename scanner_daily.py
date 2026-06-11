# -*- coding: utf-8 -*-
"""
台股 AI Scanner v2.1 - 後台掃描器

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
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def fetch_twse_stock_day_all_openapi(limit: int) -> Tuple[List[Dict[str, str]], str]:
    """抓證交所 OpenAPI 上市個股日成交資訊。"""
    urls = [
        "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
        "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=open_data",
        "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=open_data",
    ]

    for url in urls:
        data = request_json(url, timeout=15)
        if not isinstance(data, list) or not data:
            continue

        rows = []
        for row in data:
            if not isinstance(row, dict):
                continue
            # OpenAPI / open_data 欄位有時是中文，有時是英文代稱，這裡都支援。
            code = row.get("證券代號") or row.get("Code") or row.get("code") or row.get("stock_id")
            name = row.get("證券名稱") or row.get("Name") or row.get("name") or row.get("stock_name")
            money = (
                row.get("成交金額")
                or row.get("TradeValue")
                or row.get("trade_value")
                or row.get("Trading_money")
            )
            close = row.get("收盤價") or row.get("ClosingPrice") or row.get("close")

            if code is None:
                continue
            code = normalize_stock_id(code)
            if not re.match(r"^\d{4}$", code):
                continue

            rows.append(
                {
                    "代號": code,
                    "名稱": str(name).strip() if name else code,
                    "產業": "未知",
                    "成交金額": clean_number(money),
                    "收盤價來源": clean_number(close),
                }
            )

        if rows:
            rows = sorted(rows, key=lambda x: x["成交金額"], reverse=True)
            return rows[:limit], "證交所上市成交金額排行（STOCK_DAY_ALL）"

    return [], "證交所 STOCK_DAY_ALL 抓取失敗"


def get_candidates(limit: int) -> Tuple[List[str], Dict[str, Dict[str, str]], str]:
    candidate_rows, source = fetch_twse_stock_day_all_openapi(limit)

    if not candidate_rows:
        candidate_rows = FALLBACK_CANDIDATES[:limit]
        source = "備援熱門股清單"

    candidates = unique_keep_order([r["代號"] for r in candidate_rows])[:limit]
    info_map = {
        normalize_stock_id(r["代號"]): {
            "名稱": str(r.get("名稱") or r["代號"]).strip(),
            "產業": str(r.get("產業") or "未知").strip(),
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
                }
            )

    # 候選來源名稱作為 fallback，避免名稱突然變回代號。
    for sid, info in candidate_info.items():
        rows.append({"代號": sid, "名稱": info.get("名稱", sid), "產業": info.get("產業", "未知")})

    # 常用備援表最後補。
    for r in FALLBACK_CANDIDATES:
        rows.append({"代號": r["代號"], "名稱": r["名稱"], "產業": r["產業"]})

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=["代號", "名稱", "產業"])

    out["代號"] = out["代號"].astype(str).map(normalize_stock_id)
    out["名稱"] = out["名稱"].fillna(out["代號"]).astype(str)
    out["產業"] = out["產業"].fillna("未知").astype(str)

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
    info_map = stock_info.set_index("代號")[["名稱", "產業"]].to_dict("index") if not stock_info.empty else {}

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
        "日期", "代號", "名稱", "產業", "收盤價", "AI總分", "技術分", "籌碼分", "風險分", "量比",
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

# pages/live_intraday.py
# v2.4.3 Live Intraday Page
# Add this file under: pages/live_intraday.py

from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

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


def _to_float(value, default=np.nan):
    try:
        if value is None:
            return default
        text = str(value).replace(",", "").strip()
        if text in {"", "-", "--", "nan", "None"}:
            return default
        return float(text)
    except Exception:
        return default


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


def load_rank() -> pd.DataFrame:
    if not RANK_PATH.exists():
        st.error("找不到 data/latest_rank.csv。請先讓 Daily Taiwan Stock AI Scan 成功跑完。")
        st.stop()

    df = pd.read_csv(RANK_PATH)

    # Normalize columns from older / newer scanner versions.
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
    return df


def build_symbols(df: pd.DataFrame) -> Tuple[List[str], Dict[str, str]]:
    symbols: List[str] = []
    symbol_to_code: Dict[str, str] = {}

    for _, row in df.iterrows():
        code = str(row["代號"]).zfill(4)
        market = str(row.get("市場", "未知"))

        # Prefer known market, but for unknown try both because many scanners mix listed and OTC.
        candidates = []
        if "上櫃" in market or "OTC" in market.upper():
            candidates = [f"otc_{code}.tw", f"tse_{code}.tw"]
        elif "上市" in market or "TWSE" in market.upper():
            candidates = [f"tse_{code}.tw", f"otc_{code}.tw"]
        else:
            candidates = [f"tse_{code}.tw", f"otc_{code}.tw"]

        for sym in candidates:
            symbols.append(sym)
            symbol_to_code[sym] = code

    return symbols, symbol_to_code


@st.cache_data(ttl=15, show_spinner=False)
def fetch_twse_mis_quotes(symbols: List[str]) -> pd.DataFrame:
    """Fetch quotes from TWSE MIS in small batches.

    Cached only 15 seconds to allow near-real-time refresh while avoiding excessive requests.
    """
    rows = []
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw",
    }

    # De-duplicate while preserving order.
    dedup_symbols = list(dict.fromkeys(symbols))
    batch_size = 18

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
            # Continue with other batches instead of failing whole page.
            continue
        time.sleep(0.15)

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
            # When no transaction yet, estimate with first bid/ask if available.
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

    # If trying tse and otc both returns duplicate code, keep the row with valid last price first.
    quotes["has_price"] = quotes["盤中現價"].notna().astype(int)
    quotes = quotes.sort_values(["代號", "has_price"], ascending=[True, False])
    quotes = quotes.drop_duplicates("代號", keep="first").drop(columns=["has_price"])
    return quotes


def compute_live_strength(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["AI總分"] = pd.to_numeric(df["AI總分"], errors="coerce").fillna(0)
    df["風險分"] = pd.to_numeric(df["風險分"], errors="coerce").fillna(0)
    df["盤中漲跌幅"] = pd.to_numeric(df["盤中漲跌幅"], errors="coerce").fillna(0)
    df["盤中成交量"] = pd.to_numeric(df["盤中成交量"], errors="coerce").fillna(0)

    # Volume percentile within current candidate list. Avoid over-trusting absolute MIS volume units.
    if df["盤中成交量"].max() > 0:
        df["盤中量能分"] = df["盤中成交量"].rank(pct=True) * 100
    else:
        df["盤中量能分"] = 0

    # Convert intraday return into 0~100 momentum score. -3% => 20, 0%=>50, +5%=>100, clipped.
    df["盤中漲幅分"] = ((df["盤中漲跌幅"] + 3) / 8 * 100).clip(0, 100)

    df["即時強度分"] = (
        df["AI總分"] * 0.50
        + df["盤中漲幅分"] * 0.25
        + df["盤中量能分"] * 0.15
        - df["風險分"] * 0.10
    ).round(1)

    def judge(row):
        if row["即時強度分"] >= 70 and row["盤中漲跌幅"] > 0:
            return "盤中強勢：AI分數與盤中動能同步"
        if row["AI總分"] >= 60 and row["盤中漲跌幅"] > 0:
            return "盤中偏強：可觀察量能是否延續"
        if row["盤中漲跌幅"] <= -3:
            return "盤中轉弱：避免追高，先觀察支撐"
        if row["風險分"] >= 40:
            return "高風險：即使上漲也不宜追高"
        return "中性：以盤後AI與風控為主"

    df["即時判斷"] = df.apply(judge, axis=1)
    return df.sort_values("即時強度分", ascending=False).reset_index(drop=True)


# ---------- UI ----------

st.title("⚡ 盤中即時看盤 v2.4.3")
st.caption("這是前台即時刷新頁：讀取盤後 AI 排名，再即時抓候選股盤中報價。適合盤中輔助判斷，不是券商逐筆報價。")

with st.sidebar:
    st.header("即時設定")
    refresh_seconds = st.slider("自動刷新秒數", min_value=15, max_value=120, value=30, step=15)
    top_n = st.slider("顯示前 N 檔", min_value=5, max_value=30, value=15, step=5)
    min_ai = st.slider("最低 AI 總分", 0, 100, 0, 5)
    min_strength = st.slider("最低即時強度分", 0, 100, 0, 5)

    st.info("保持這個頁面開著，它會依設定重新整理並抓最新快照。")

# Browser auto reload. This refreshes this page, not the whole GitHub Action data.
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
symbols, _ = build_symbols(rank_df)
quotes_df = fetch_twse_mis_quotes(symbols)

merged = rank_df.copy()
if quotes_df.empty:
    st.warning("目前沒有抓到盤中報價。可能是非交易時間、TWSE MIS 暫時無回應，或網路限制。")
    for col in ["盤中現價", "盤中漲跌幅", "盤中成交量", "報價時間", "報價市場"]:
        merged[col] = np.nan
else:
    merged = merged.merge(quotes_df, on="代號", how="left")
    merged["名稱"] = merged.get("即時名稱", pd.Series(index=merged.index)).fillna(merged["名稱"])
    merged["市場"] = merged.get("報價市場", pd.Series(index=merged.index)).fillna(merged["市場"])

live_df = compute_live_strength(merged)
filtered = live_df[(live_df["AI總分"] >= min_ai) & (live_df["即時強度分"] >= min_strength)].copy()

quote_ok = int(live_df["盤中現價"].notna().sum())
up_count = int((live_df["盤中漲跌幅"] > 0).sum())
best_pct = float(live_df["盤中漲跌幅"].max()) if len(live_df) else 0
best_strength = float(live_df["即時強度分"].max()) if len(live_df) else 0

c1, c2, c3, c4 = st.columns(4)
c1.metric("候選股票數", len(rank_df))
c2.metric("報價成功檔數", quote_ok)
c3.metric("盤中上漲檔數", up_count)
c4.metric("最高即時強度分", f"{best_strength:.1f}")

c5, c6, c7, c8 = st.columns(4)
c5.metric("盤中最強漲幅", f"{best_pct:.2f}%")
c6.metric("最後刷新", datetime.now().strftime("%H:%M:%S"))
c7.metric("自動刷新", f"{refresh_seconds} 秒")
c8.metric("資料模式", "前台即時")

st.divider()

st.subheader("盤中 AI 即時強勢股")
st.caption("即時強度分 = AI總分 + 盤中漲跌幅 + 候選股內量能排名 - 風險扣分。")

show_cols = [
    "代號", "名稱", "市場", "產業", "AI總分", "風險分", "即時強度分",
    "盤中現價", "盤中漲跌幅", "盤中成交量", "報價時間", "即時判斷"
]
show_cols = [c for c in show_cols if c in filtered.columns]

st.dataframe(
    filtered[show_cols].head(top_n),
    use_container_width=True,
    hide_index=True,
)

st.subheader("全部候選股即時快照")
st.dataframe(
    live_df[show_cols],
    use_container_width=True,
    hide_index=True,
)

st.caption("提醒：這是網頁快照更新，不是券商逐筆成交資料。法人、融資融券仍以盤後資料為準。")

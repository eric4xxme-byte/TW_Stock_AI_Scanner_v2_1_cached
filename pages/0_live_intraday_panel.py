# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Tuple

import pandas as pd
import streamlit as st

st.set_page_config(page_title="v2.24 即時行情優先面板", page_icon="⚡", layout="wide")

DATA_DIR = Path("data")
LOCAL_LIVE_CSV = DATA_DIR / "live_intraday.csv"
LOCAL_LIVE_META = DATA_DIR / "live_intraday_meta.json"

# 這是你的公開 repo。讀 raw GitHub 可以避免 Streamlit Cloud 檔案系統沒即時 pull 導致價格假更新。
RAW_BASE = "https://raw.githubusercontent.com/eric4xxme-byte/TW_Stock_AI_Scanner_v2_1_cached/main/data"
TAIPEI = timezone(timedelta(hours=8))


def inject_hard_refresh(seconds: int = 5) -> None:
    st.components.v1.html(
        f"""
        <script>
        const delay = {max(1, int(seconds)) * 1000};
        setTimeout(() => {{ window.parent.location.reload(); }}, delay);
        </script>
        """,
        height=0,
    )


def now_tw() -> datetime:
    return datetime.now(TAIPEI)


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


@st.cache_data(ttl=4, show_spinner=False)
def load_live_from_raw(cache_bust: int) -> Tuple[pd.DataFrame, Dict[str, Any], str]:
    csv_url = f"{RAW_BASE}/live_intraday.csv?cb={cache_bust}"
    meta_url = f"{RAW_BASE}/live_intraday_meta.json?cb={cache_bust}"
    df = pd.read_csv(csv_url, dtype=str)
    meta = json.loads(pd.read_json(meta_url, typ="series").to_json(force_ascii=False))
    return df, meta, "GitHub raw"


def load_live_from_local() -> Tuple[pd.DataFrame, Dict[str, Any], str]:
    df = pd.read_csv(LOCAL_LIVE_CSV, dtype=str) if LOCAL_LIVE_CSV.exists() else pd.DataFrame()
    meta = json.loads(LOCAL_LIVE_META.read_text(encoding="utf-8")) if LOCAL_LIVE_META.exists() else {}
    return df, meta, "local data"


def load_live() -> Tuple[pd.DataFrame, Dict[str, Any], str]:
    # 每 5 秒換 cache_bust。cache_data ttl=4，避免 Streamlit 自己卡住舊檔。
    cache_bust = int(time.time() // 5)
    try:
        return load_live_from_raw(cache_bust)
    except Exception as exc:
        df, meta, src = load_live_from_local()
        meta = dict(meta or {})
        meta["raw_fetch_error"] = str(exc)[:180]
        return df, meta, src


def numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


inject_hard_refresh(5)

st.title("⚡ v2.24 即時行情優先面板")
st.caption("這頁每 5 秒硬刷新；資料本體由 GitHub Actions 每 5 分鐘寫入 data/live_intraday.csv。畫面時間更新≠價格更新，價格以快照時間與資料來源為準。")

if st.button("立即重新讀取 GitHub 最新 CSV"):
    st.cache_data.clear()
    st.rerun()

live_df, meta, source = load_live()
updated_at = meta.get("updated_at", "")
updated_dt = parse_dt(updated_at)
age_sec = None
if updated_dt:
    age_sec = int((now_tw() - updated_dt.astimezone(TAIPEI)).total_seconds())

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("讀取來源", source)
m2.metric("資料狀態", meta.get("status", "unknown"))
m3.metric("快照更新", updated_at or "尚無")
m4.metric("資料年齡", f"{age_sec}s" if age_sec is not None else "-")
m5.metric("有效價格檔數", meta.get("valid_price_count", 0))

if meta.get("raw_fetch_error"):
    st.warning(f"GitHub raw 讀取失敗，暫用本機資料：{meta.get('raw_fetch_error')}")

if age_sec is not None:
    if age_sec > 480:
        st.error("資料已超過 8 分鐘沒有更新：不是前台問題，是 GitHub Actions 或 MIS 來源沒有寫入新檔。")
    elif age_sec > 360:
        st.warning("資料超過 6 分鐘：GitHub Actions 可能延遲，或目前不是台股盤中。")
    else:
        st.success("自動同步正常：資料時間在可接受範圍內。")

if live_df.empty:
    st.error("目前沒有 live_intraday.csv。請先讓 GitHub Actions 跑 v2.24 Live Intraday Auto Sync 5min。")
    st.stop()

live_df = numeric(live_df, ["盤中現價", "盤中漲跌幅", "盤中漲跌", "盤中成交量", "AI總分", "盤中強度分", "風險分"])

# Priority board
st.divider()
st.subheader("即時決策排行")
view_cols = [
    "代號", "名稱", "市場", "產業", "盤中現價", "盤中漲跌幅", "盤中成交量", "AI總分", "盤中強度分", "即時決策", "入場狀態",
    "左側試單價", "右側確認價", "追價上限", "防守停損", "風險分", "盤中時間", "價格來源", "盤中狀態", "決策原因", "快照時間",
]
view_cols = [c for c in view_cols if c in live_df.columns]

sort_cols = [c for c in ["盤中強度分", "盤中漲跌幅", "AI總分"] if c in live_df.columns]
show = live_df.copy()
if sort_cols:
    show = show.sort_values(sort_cols, ascending=[False] * len(sort_cols))

st.dataframe(show[view_cols], use_container_width=True, hide_index=True)

st.divider()
st.subheader("只看可小量/等確認")
if "即時決策" in live_df.columns:
    focus = live_df[live_df["即時決策"].astype(str).isin(["可小量試單", "等站穩/回測"])].copy()
    if focus.empty:
        st.info("目前沒有符合『可小量/等確認』的股票。")
    else:
        focus = focus.sort_values(["即時決策", "盤中強度分"], ascending=[True, False]) if "盤中強度分" in focus.columns else focus
        st.dataframe(focus[view_cols], use_container_width=True, hide_index=True)

st.divider()
st.subheader("同步診斷")
st.json(meta)

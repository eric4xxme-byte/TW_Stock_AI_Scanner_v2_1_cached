# -*- coding: utf-8 -*-
"""
台股 AI Scanner v2.1 - 前台快取讀取版

這版不在網站開啟時即時掃描 30 檔股票。
它只讀取 data/latest_*.csv，速度更快，也更穩定。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Tuple

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

DATA_DIR = Path("data")
RANK_FILE = DATA_DIR / "latest_rank.csv"
RISK_FILE = DATA_DIR / "latest_risk.csv"
PRICE_FILE = DATA_DIR / "latest_price_history.csv"
META_FILE = DATA_DIR / "latest_meta.json"

st.set_page_config(page_title="台股 AI Scanner v2.1", page_icon="📈", layout="wide")


@st.cache_data(ttl=120)
def load_outputs() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict]:
    rank_df = pd.read_csv(RANK_FILE) if RANK_FILE.exists() else pd.DataFrame()
    risk_df = pd.read_csv(RISK_FILE) if RISK_FILE.exists() else pd.DataFrame()
    price_df = pd.read_csv(PRICE_FILE) if PRICE_FILE.exists() else pd.DataFrame()
    meta = {}
    if META_FILE.exists():
        try:
            meta = json.loads(META_FILE.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    return rank_df, risk_df, price_df, meta


def format_int(x):
    try:
        return f"{int(float(x)):,}"
    except Exception:
        return x


def judgement_text(row: pd.Series) -> str:
    ai = float(row.get("AI總分", 0))
    risk = float(row.get("風險分", 0))
    chip = float(row.get("籌碼分", 0))
    if ai >= 78 and risk <= 35 and chip >= 60:
        return "高關注：技術與籌碼同步偏強，可列入隔日重點觀察，但仍不建議早盤直接追高。"
    if ai >= 68 and risk <= 45:
        return "偏多觀察：條件不差，適合等回測支撐或尾盤確認。"
    if ai >= 55:
        return "中性觀察：有部分條件轉強，但訊號不夠完整。"
    return "暫不進場：綜合分數不足，或風險、籌碼、技術條件不佳。"


def make_chart(price_df: pd.DataFrame, stock_id: str, stock_name: str):
    if price_df.empty:
        return None
    df = price_df[price_df["stock_id"].astype(str).str.zfill(4) == str(stock_id).zfill(4)].copy()
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "max", "min", "close", "Trading_Volume", "ma5", "ma10", "ma20"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.7, 0.3],
        subplot_titles=("股價 + 均線", "成交量"),
    )
    fig.add_trace(
        go.Candlestick(
            x=df["date"], open=df["open"], high=df["max"], low=df["min"], close=df["close"], name="K線"
        ),
        row=1,
        col=1,
    )
    if "ma5" in df.columns:
        fig.add_trace(go.Scatter(x=df["date"], y=df["ma5"], mode="lines", name="MA5"), row=1, col=1)
    if "ma10" in df.columns:
        fig.add_trace(go.Scatter(x=df["date"], y=df["ma10"], mode="lines", name="MA10"), row=1, col=1)
    if "ma20" in df.columns:
        fig.add_trace(go.Scatter(x=df["date"], y=df["ma20"], mode="lines", name="MA20"), row=1, col=1)
    fig.add_trace(go.Bar(x=df["date"], y=df["Trading_Volume"], name="成交量"), row=2, col=1)
    fig.update_layout(
        title=f"{stock_name} {stock_id}｜K線 + 均線 + 成交量",
        height=720,
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        template="plotly_white",
    )
    return fig


rank_df, risk_df, price_df, meta = load_outputs()

st.title("📈 台股 AI Scanner v2.1 Cached")
st.caption("後台先產生資料，網站只讀取結果：速度更快、穩定度更高。")

st.sidebar.header("資料狀態")
st.sidebar.write("最後更新：", meta.get("updated_at", "尚未產生"))
st.sidebar.write("候選股來源：", meta.get("candidate_source", "無"))
st.sidebar.write("送出候選股數：", meta.get("candidate_count", 0))
st.sidebar.write("成功分析股票數：", meta.get("success_count", 0))
st.sidebar.write("略過股票數：", meta.get("skipped_count", 0))
st.sidebar.write("已抓籌碼檔數：", meta.get("chip_fetched_count", 0))
st.sidebar.write("FinMind Token：", "已設定" if meta.get("has_finmind_token") else "未設定 / 未記錄")

st.sidebar.divider()
st.sidebar.header("篩選")
min_score = st.sidebar.slider("最低 AI 總分", 0, 100, 0)
max_risk = st.sidebar.slider("最高風險分", 0, 100, 100)

if st.sidebar.button("重新讀取已產生資料"):
    st.cache_data.clear()
    st.rerun()

if rank_df.empty:
    st.warning("目前沒有分析結果。請先執行 scanner_daily.py，或到 GitHub Actions 手動 Run workflow。")
    st.code("python scanner_daily.py --limit 30 --chip-limit 10")
    st.stop()

for c in ["AI總分", "風險分", "技術分", "籌碼分"]:
    if c in rank_df.columns:
        rank_df[c] = pd.to_numeric(rank_df[c], errors="coerce").fillna(0)

filtered = rank_df[(rank_df["AI總分"] >= min_score) & (rank_df["風險分"] <= max_risk)].copy()
filtered = filtered.sort_values("AI總分", ascending=False).reset_index(drop=True)

c1, c2, c3, c4 = st.columns(4)
c1.metric("分析股票數", len(rank_df))
c2.metric("最高 AI 分數", round(float(rank_df["AI總分"].max()), 1))
c3.metric("平均風險分", round(float(rank_df["風險分"].mean()), 1))
c4.metric("高關注股票數", int((rank_df["AI總分"] >= 75).sum()))

st.divider()
st.subheader("今日 AI 前 5 名")
top_df = filtered.head(5)
cols = st.columns(max(1, min(5, len(top_df))))
for col, (_, row) in zip(cols, top_df.iterrows()):
    with col:
        st.markdown(f"### {row.get('名稱', row.get('代號'))} {row.get('代號')}")
        st.caption(str(row.get("產業", "未知")))
        st.metric("AI總分", row.get("AI總分", 0))
        st.write(f"技術分：{row.get('技術分', 0)}")
        st.write(f"籌碼分：{row.get('籌碼分', 0)}")
        st.write(f"風險分：{row.get('風險分', 0)}")
        st.info(str(row.get("AI進場判斷", "")))

st.divider()
st.subheader("完整 AI 排名表")
show_cols = [
    "日期", "代號", "名稱", "產業", "收盤價", "AI總分", "技術分", "籌碼分", "風險分", "量比",
    "法人單日買賣超", "法人近3日買賣超", "融資變化", "融券變化", "籌碼狀態", "AI進場判斷",
    "停損參考", "壓力參考",
]
show_cols = [c for c in show_cols if c in filtered.columns]
st.dataframe(filtered[show_cols], use_container_width=True, hide_index=True)

st.divider()
st.subheader("高風險不要追")
st.caption("這裡不是做空建議，而是提醒：分數看起來不錯也可能有追高、倒貨或籌碼轉弱風險。")
if risk_df.empty:
    st.success("目前沒有明顯高風險名單。")
else:
    risk_show = [
        "日期", "代號", "名稱", "產業", "收盤價", "AI總分", "技術分", "籌碼分", "風險分", "量比",
        "法人單日買賣超", "法人近3日買賣超", "融資變化", "籌碼狀態", "技術風險", "籌碼風險", "AI進場判斷",
    ]
    risk_show = [c for c in risk_show if c in risk_df.columns]
    st.dataframe(risk_df[risk_show], use_container_width=True, hide_index=True)

st.divider()
st.subheader("單檔詳細分析")
options = (filtered["代號"].astype(str) + " " + filtered["名稱"].astype(str)).tolist()
if not options:
    st.warning("目前篩選條件下沒有股票。")
    st.stop()
selected = st.selectbox("選擇股票", options)
selected_id = selected.split(" ")[0].zfill(4)
row = filtered[filtered["代號"].astype(str).str.zfill(4) == selected_id].iloc[0]

k1, k2, k3, k4 = st.columns(4)
k1.metric("收盤價", row.get("收盤價", "-"))
k2.metric("AI總分", row.get("AI總分", "-"))
k3.metric("籌碼分", row.get("籌碼分", "-"))
k4.metric("風險分", row.get("風險分", "-"))

st.markdown(f"## {row.get('名稱', selected_id)} {selected_id}")
st.write(f"**產業：** {row.get('產業', '未知')}")
st.write(f"**籌碼狀態：** {row.get('籌碼狀態', '未知')}")

fig = make_chart(price_df, selected_id, str(row.get("名稱", selected_id)))
if fig:
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("這檔目前沒有快取 K 線資料。下一次後台掃描成功後會補上。")

st.markdown("### AI 總結")
st.info(judgement_text(row))

st.markdown("### 進場策略")
st.success(str(row.get("AI進場判斷", "無資料")))

st.markdown("### 出場策略")
st.warning(
    f"停損參考：{row.get('停損參考', '無資料')}。壓力參考：{row.get('壓力參考', '無資料')}。"
    "若出現爆量長上影、跌破5日線、法人轉賣或融資暴增，應考慮減碼或出場。"
)

st.markdown("### 技術面原因")
st.success(str(row.get("技術面原因", "無資料")))

st.markdown("### 籌碼面原因")
st.success(str(row.get("籌碼面原因", "無資料")))

st.markdown("### 風險提醒")
st.error(f"技術風險：{row.get('技術風險', '無資料')}｜籌碼風險：{row.get('籌碼風險', '無資料')}")

# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Tuple

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

try:
    from v225_decision_core import apply_v225_decision
except Exception as exc:  # pragma: no cover
    st.error(f'找不到 v225_decision_core.py：{exc}')
    st.stop()

st.set_page_config(page_title='v2.25 真實決策雷達', page_icon='🎯', layout='wide')

TAIPEI = timezone(timedelta(hours=8))
DATA = Path('data')
RAW_BASE = 'https://raw.githubusercontent.com/eric4xxme-byte/TW_Stock_AI_Scanner_v2_1_cached/main/data'


def now_tw() -> datetime:
    return datetime.now(TAIPEI)


def hard_refresh(seconds: int) -> None:
    components.html(
        f"""
        <script>
        setTimeout(() => {{ window.parent.location.reload(); }}, {max(3, int(seconds)) * 1000});
        </script>
        """,
        height=0,
    )


def safe_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(',', '', regex=False).str.replace('%', '', regex=False), errors='coerce')


@st.cache_data(ttl=5, show_spinner=False)
def load_github_decision(cb: int) -> Tuple[pd.DataFrame, Dict[str, Any], str]:
    url = f'{RAW_BASE}/v225_realtime_decision.csv?cb={cb}'
    meta_url = f'{RAW_BASE}/v225_realtime_decision_meta.json?cb={cb}'
    df = pd.read_csv(url, dtype=str)
    try:
        meta = json.loads(pd.read_json(meta_url, typ='series').to_json(force_ascii=False))
    except Exception:
        meta = {}
    return df, meta, 'GitHub v225 決策檔'


@st.cache_data(ttl=5, show_spinner=False)
def load_github_live_and_calc(cb: int) -> Tuple[pd.DataFrame, Dict[str, Any], str]:
    url = f'{RAW_BASE}/live_intraday.csv?cb={cb}'
    meta_url = f'{RAW_BASE}/live_intraday_meta.json?cb={cb}'
    df = pd.read_csv(url, dtype=str)
    decision, _state = apply_v225_decision(df, None)
    try:
        meta = json.loads(pd.read_json(meta_url, typ='series').to_json(force_ascii=False))
    except Exception:
        meta = {}
    meta['v225_frontend_calc'] = True
    return decision, meta, 'GitHub live_intraday 即時計算'


@st.cache_data(ttl=8, show_spinner=False)
def fetch_direct_mis(limit: int, focus_codes: str, cb: int) -> Tuple[pd.DataFrame, Dict[str, Any], str]:
    """從 Streamlit 前台伺服器直接抓 MIS，不等 GitHub Actions。"""
    import tw_live_engine as live
    ts_dt = live.now_tw()
    ts = ts_dt.isoformat(timespec='seconds')
    focus = live.split_codes(focus_codes)
    pool = live.build_pool(int(limit), focus)
    channels, meta_map, order = live.build_channels(pool)
    raw, errors = live.fetch_quotes(channels, batch_size=24, retries=2)
    rows = [r for q in raw if (r := live.quote_to_row(q, meta_map, ts)) is not None]
    df = live.choose_best(rows, order)
    df = live.add_missing_rows(df, meta_map, order, ts)
    decision, _state = apply_v225_decision(df, None)
    valid = int(pd.to_numeric(decision.get('盤中現價', pd.Series(dtype=str)), errors='coerce').notna().sum()) if not decision.empty else 0
    meta = {
        'status': 'ok' if valid else 'no_valid_price',
        'updated_at': ts,
        'mode': 'frontend_direct_mis',
        'candidate_count': len(order),
        'quote_raw_count': len(raw),
        'valid_price_count': valid,
        'errors_tail': errors[-5:],
        'note': '前台直接抓 MIS；這個模式不等 GitHub Actions，也不會寫入 Google Sheet。',
    }
    return decision, meta, '前台直抓 TWSE MIS'


def parse_time(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00')).astimezone(TAIPEI)
    except Exception:
        return None


with st.sidebar:
    st.header('v2.25 設定')
    mode = st.radio('資料模式', ['前台直抓 MIS', 'GitHub 背景決策檔', 'GitHub live 即時計算'], index=0)
    limit = st.slider('掃描檔數', 30, 300, 120, 10)
    focus = st.text_input('固定重點股', '3441,2382,2313,6770,2409,3042,6257')
    refresh = st.slider('前台刷新秒數', 5, 60, 10, 5)
    show_all = st.checkbox('顯示全部欄位', False)
    st.caption('A級不等於無腦買；必須同時給入場區、追價上限、停損與失效條件。')

hard_refresh(refresh)

st.title('🎯 v2.25 真實決策雷達｜二次確認，不再白老鼠試單')
st.caption('修正重點：不再單靠一個 tick 叫你小單；A級需量價/位置/風險/AI 同步，並用狀態鎖定降低一分鐘內反覆翻訊號。')

if st.button('立即重抓 / 清快取'):
    st.cache_data.clear()
    st.rerun()

cb = int(time.time() // max(5, refresh))
try:
    if mode == '前台直抓 MIS':
        df, meta, source = fetch_direct_mis(limit, focus, cb)
    elif mode == 'GitHub 背景決策檔':
        df, meta, source = load_github_decision(cb)
    else:
        df, meta, source = load_github_live_and_calc(cb)
except Exception as exc:
    st.error(f'讀取失敗：{exc}')
    st.stop()

updated_at = meta.get('updated_at') or meta.get('快照時間') or ''
udt = parse_time(updated_at)
age = int((now_tw() - udt).total_seconds()) if udt else None

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric('讀取來源', source)
c2.metric('資料狀態', meta.get('status', 'unknown'))
c3.metric('更新時間', updated_at or '-')
c4.metric('資料年齡', f'{age}s' if age is not None else '-')
c5.metric('有效價格', meta.get('valid_price_count', '-'))
c6.metric('候選數', len(df) if isinstance(df, pd.DataFrame) else 0)

if age is not None and mode != '前台直抓 MIS' and age > 480:
    st.warning('GitHub 背景檔超過 8 分鐘，代表 GitHub Actions 排程可能延遲。要看當下盤中，用左側「前台直抓 MIS」。')
elif mode == '前台直抓 MIS':
    st.success('目前使用前台直抓 MIS：不等 GitHub Actions；若這裡也不動，才是 MIS 或網路來源問題。')

if df.empty:
    st.error('沒有資料。')
    st.stop()

for col in ['盤中現價','盤中漲跌幅','盤中成交量','AI總分','風險分','v225信心分','v225優先級']:
    if col in df.columns:
        df[col] = safe_num(df[col])

# 總覽
sig = df.get('v225決策', pd.Series(dtype=str)).astype(str)
a_df = df[sig.str.contains('A級', regex=False)].copy()
b_df = df[sig.str.contains('B級', regex=False)].copy()
no_df = df[sig.str.contains('不可追|暫不進場', regex=True)].copy()

m1, m2, m3 = st.columns(3)
m1.metric('A級可試單/保留', len(a_df))
m2.metric('B級等二次確認', len(b_df))
m3.metric('不可追 / 暫不進場', len(no_df))

base_cols = [
    '代號','名稱','市場','產業','盤中現價','盤中漲跌幅','盤中成交量',
    'v225決策','v225訊號','v225信心分','v225條件檢查','v225入場區','v225右側確認價','v225追價上限','v225停損','v225失效條件','v225核心講解','v225狀態鎖定',
    'AI總分','風險分','價格來源','盤中時間','快照時間'
]
cols = [c for c in base_cols if c in df.columns]
if show_all:
    cols = list(df.columns)

st.divider()
st.subheader('A級：可小量，但只准照入場區 + 停損')
if a_df.empty:
    st.info('目前沒有 A級。這是正常的：v2.25 不會為了有名單而硬塞買點。')
else:
    a_df = a_df.sort_values(['v225優先級','v225信心分','盤中漲跌幅'], ascending=[True, False, False])
    st.dataframe(a_df[cols], use_container_width=True, hide_index=True)

st.subheader('B級：等二次確認，不先當白老鼠')
if b_df.empty:
    st.info('目前沒有 B級。')
else:
    b_df = b_df.sort_values(['v225優先級','v225信心分','盤中漲跌幅'], ascending=[True, False, False])
    st.dataframe(b_df[cols], use_container_width=True, hide_index=True)

st.subheader('完整排行')
rank = df.copy()
if 'v225優先級' in rank.columns:
    rank = rank.sort_values(['v225優先級','v225信心分','盤中漲跌幅'], ascending=[True, False, False])
st.dataframe(rank[cols], use_container_width=True, hide_index=True)

with st.expander('同步 / 來源診斷'):
    st.json(meta)

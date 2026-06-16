# -*- coding: utf-8 -*-
"""
TW Stock AI Scanner v2.25｜Realtime Decision Core

重點：
- 不再用單一 tick 直接從「可試單」翻成「不可」。
- 將訊號拆成：位置、動能、量能、風險、AI 分數、追高風險。
- 只有通過 2/3 確認才給 A 級試單；否則只列候選/等確認。
- 保留上一輪狀態，避免一分鐘內反覆打臉。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd

TAIPEI = timezone(timedelta(hours=8))


def now_tw() -> datetime:
    return datetime.now(TAIPEI)


def safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    if v is None:
        return default
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            f = float(v)
            return f if math.isfinite(f) else default
        except Exception:
            return default
    s = str(v).strip().replace(',', '').replace('%', '')
    if s in {'', '-', '--', 'None', 'none', 'nan', 'NaN', 'null'}:
        return default
    # 取區間第一個數字，例如 136.5~137.5
    import re
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    if not m:
        return default
    try:
        return float(m.group(0))
    except Exception:
        return default


def safe_text(v: Any, default: str = '') -> str:
    if v is None:
        return default
    s = str(v).strip()
    if s.lower() in {'nan', 'none', 'null'}:
        return default
    return s


def round_tick(price: float, up: bool = False) -> float:
    """簡化台股 tick：用於風險區間，不作精確下單撮合。"""
    if price <= 0:
        return 0.0
    if price < 10:
        tick = 0.01
    elif price < 50:
        tick = 0.05
    elif price < 100:
        tick = 0.1
    elif price < 500:
        tick = 0.5
    elif price < 1000:
        tick = 1.0
    else:
        tick = 5.0
    q = price / tick
    return round((math.ceil(q) if up else math.floor(q)) * tick, 2)


def classify_state(score: float, pct: float, risk: float, overheat: bool, price_ok: bool, trend_ok: bool, volume_ok: bool) -> Tuple[str, str, int]:
    """return decision, signal, priority"""
    if not price_ok:
        return '不判斷', '⚪ 無有效即時報價', 9
    if overheat:
        return '不可追', '🔴 追高風險區', 7
    if score >= 82 and trend_ok and volume_ok and risk <= 55 and pct <= 6.2:
        return 'A級可小量試單', '🟢 量價確認', 1
    if score >= 72 and trend_ok and risk <= 60 and pct <= 7.0:
        return 'B級等二次確認', '🟡 動能成立但缺一條件', 2
    if score >= 62 and pct > 0:
        return 'C級候選觀察', '🟣 有前兆但未確認', 4
    if pct <= -2.5:
        return '暫不進場', '⚪ 盤中轉弱', 8
    return '觀察', '⚪ 條件不足', 6


def calc_one(row: pd.Series, prev_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    code = safe_text(row.get('代號'))
    name = safe_text(row.get('名稱'), code)
    px = safe_float(row.get('盤中現價'), None)
    prev = safe_float(row.get('昨收'), None)
    open_p = safe_float(row.get('開盤'), None)
    high = safe_float(row.get('最高'), px)
    low = safe_float(row.get('最低'), px)
    pct = safe_float(row.get('盤中漲跌幅'), 0.0) or 0.0
    vol = safe_float(row.get('盤中成交量'), 0.0) or 0.0
    tv = safe_float(row.get('最近單量'), 0.0) or 0.0
    ai = safe_float(row.get('AI總分'), 50.0) or 50.0
    tech = safe_float(row.get('技術分'), 50.0)
    if tech is None or tech == 0:
        tech = 50.0
    chip = safe_float(row.get('籌碼分'), 50.0)
    if chip is None or chip == 0:
        chip = 50.0
    risk = safe_float(row.get('風險分'), 45.0) or 45.0
    source = safe_text(row.get('價格來源'))

    price_ok = px is not None and px > 0 and source not in {'昨收參考', '無'}
    if not price_ok:
        return {
            'v225決策': '不判斷', 'v225訊號': '⚪ 無有效即時報價', 'v225信心分': 0,
            'v225優先級': 9, 'v225入場區': '-', 'v225停損': '-', 'v225追價上限': '-',
            'v225失效條件': '等 MIS 回傳成交價；不能拿昨收或空價判斷。',
            'v225核心講解': f'{code} {name}：本輪沒有有效成交價，禁止用舊價假裝更新。',
            'v225條件檢查': '價格✗ / 量能- / 位置- / 風險-',
            'v225狀態鎖定': '無',
        }

    high = high or px
    low = low or px
    prev = prev or px
    open_p = open_p or prev

    day_range = max(high - low, max(px, 1) * 0.006)
    pos_in_range = max(0.0, min(1.0, (px - low) / day_range))
    near_high = px >= high * 0.992
    not_far_from_low = px <= low * 1.035
    above_open = px >= open_p * 1.002
    above_prev = px >= prev * 1.003
    trend_ok = (above_open and above_prev) or near_high
    volume_ok = vol >= 800 or tv >= 20
    overheat = pct >= 7.5 or px >= prev * 1.075
    late_limit_zone = pct >= 8.8

    momentum_score = max(0, min(100, 48 + pct * 6.2 + (8 if above_open else 0) + (8 if near_high else 0)))
    position_score = max(0, min(100, 66 if not_far_from_low else 50 + (12 if near_high else -8)))
    volume_score = max(0, min(100, 50 + (12 if vol >= 800 else 0) + (12 if vol >= 2500 else 0) + (8 if tv >= 20 else 0)))
    ai_score = max(0, min(100, ai * 0.65 + tech * 0.2 + chip * 0.15))
    risk_penalty = max(0, risk - 42) * 0.7 + (15 if overheat else 0)
    score = round(max(0, min(100, momentum_score * 0.30 + position_score * 0.18 + volume_score * 0.18 + ai_score * 0.24 + 10 - risk_penalty)), 1)

    decision, signal, priority = classify_state(score, pct, risk, overheat, price_ok, trend_ok, volume_ok)

    left_low = round_tick(max(low, px * 0.988), up=False)
    left_high = round_tick(px * 1.003, up=True)
    confirm = round_tick(max(high, px * 1.006), up=True)
    chase = round_tick(px * 1.012, up=True)
    stop_base = min(low, px * 0.976) if low and low < px else px * 0.972
    stop = round_tick(stop_base, up=False)

    conditions = []
    conditions.append('價站昨收/開盤✓' if trend_ok else '價未站穩✗')
    conditions.append('量能✓' if volume_ok else '量能不足✗')
    conditions.append('未過熱✓' if not overheat else '過熱✗')
    conditions.append('風險可控✓' if risk <= 55 else '風險偏高✗')
    conditions.append('靠近低風險區✓' if not_far_from_low else '離低吸區偏遠')
    condition_text = ' / '.join(conditions)

    if decision.startswith('A級'):
        entry_zone = f'{left_low:.2f}～{left_high:.2f}；只允許第一筆，不追過 {chase:.2f}'
        invalid = f'跌破 {stop:.2f} 或 2 輪內無法站回 {confirm:.2f}，直接撤。'
        explain = f'{code} {name}：量價與 AI 分同步，分數 {score}。可試單不是叫你追高，是只在 {left_low:.2f}～{left_high:.2f} 小量確認；超過追價上限不買。'
    elif decision.startswith('B級'):
        entry_zone = f'等站穩 {confirm:.2f} 或回測 {left_low:.2f}～{left_high:.2f} 不破'
        invalid = f'跌破 {stop:.2f} 或量縮跌回開盤下方，取消。'
        explain = f'{code} {name}：有動能但條件未滿，分數 {score}。現在不是白老鼠試單，等二次確認才動。'
    elif decision == '不可追':
        entry_zone = '不給新進價；等拉回承接或尾盤結構'
        invalid = '已在追高風險區，沒有失效問題，原則是不新追。'
        explain = f'{code} {name}：漲幅 {pct:.2f}% 已偏高，分數再高也不追；等回測再評估。'
    else:
        entry_zone = f'觀察；低吸只看 {left_low:.2f} 附近承接'
        invalid = f'跌破 {stop:.2f} 或分數低於 60，移出短線進場池。'
        explain = f'{code} {name}：條件不足，分數 {score}。目前只列觀察，不給買進。'

    lock_note = '無'
    if prev_state:
        prev_decision = safe_text(prev_state.get('v225決策'))
        prev_score = safe_float(prev_state.get('v225信心分'), 0) or 0
        weak_count = int(safe_float(prev_state.get('weak_count'), 0) or 0)
        # 防止上一輪 A 級，下一輪因一個 tick 直接變不可；必須真的跌破/過熱才解除。
        if prev_decision.startswith('A級') and decision in {'B級等二次確認', 'C級候選觀察', '觀察'} and not overheat and px > stop:
            decision = 'A級試單後觀察'
            signal = '🟢 訊號保留，等二次確認'
            priority = min(priority, 2)
            score = max(score, round(prev_score * 0.92, 1))
            weak_count += 1
            lock_note = f'沿用上一輪 A 級訊號第 {weak_count} 輪；未跌破停損前不瞬間翻空。'
            if weak_count >= 2:
                decision = 'B級等二次確認'
                signal = '🟡 A級降級，等站回'
                lock_note = '已連續 2 輪轉弱，從 A 級降為 B 級，不再新增試單。'
        else:
            weak_count = 0 if decision.startswith('A級') else weak_count
    else:
        weak_count = 0

    return {
        'v225決策': decision,
        'v225訊號': signal,
        'v225信心分': score,
        'v225優先級': priority,
        'v225入場區': entry_zone,
        'v225右側確認價': f'{confirm:.2f}',
        'v225追價上限': f'{chase:.2f}',
        'v225停損': f'{stop:.2f}',
        'v225失效條件': invalid,
        'v225條件檢查': condition_text,
        'v225核心講解': explain,
        'v225狀態鎖定': lock_note,
        'v225動能分': round(momentum_score, 1),
        'v225位置分': round(position_score, 1),
        'v225量能分': round(volume_score, 1),
        'v225AI結構分': round(ai_score, 1),
        'weak_count': weak_count,
    }


def load_state(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        df = pd.read_csv(path, dtype=str)
    except Exception:
        return {}
    if '代號' not in df.columns:
        return {}
    return {str(r.get('代號')).zfill(4): dict(r) for _, r in df.iterrows()}


def apply_v225_decision(df: pd.DataFrame, state_path: Optional[Path] = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    if df is None or df.empty:
        return pd.DataFrame(), pd.DataFrame()
    out = df.copy()
    if '代號' in out.columns:
        out['代號'] = out['代號'].astype(str).str.replace('.0', '', regex=False).str.zfill(4)
    states = load_state(state_path) if state_path else {}
    rows = []
    for _, row in out.iterrows():
        code = str(row.get('代號', '')).zfill(4)
        rows.append(calc_one(row, states.get(code)))
    dec = pd.DataFrame(rows)
    out = pd.concat([out.reset_index(drop=True), dec.reset_index(drop=True)], axis=1)
    now = now_tw().isoformat(timespec='seconds')
    state_cols = ['代號', '名稱', '盤中現價', '盤中漲跌幅', 'v225決策', 'v225訊號', 'v225信心分', 'v225停損', 'weak_count']
    state = out[[c for c in state_cols if c in out.columns]].copy()
    state['state_updated_at'] = now
    sort_cols = [c for c in ['v225優先級', 'v225信心分', '盤中漲跌幅'] if c in out.columns]
    if sort_cols:
        out = out.sort_values(sort_cols, ascending=[True, False, False]).reset_index(drop=True)
    return out, state


def write_meta(path: Path, meta: Dict[str, Any]) -> None:
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

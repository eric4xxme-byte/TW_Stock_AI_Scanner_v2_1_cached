#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2.23.2 Limit-up precursor background collector
Creates/updates data/v231_limitup_precursor_features.csv from existing learning journal and market context.
This is intentionally defensive: it should never fail the workflow just because a column is missing.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict

import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ).strftime("%Y-%m-%d")
NOW = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

JOURNAL = DATA / "v215_verified_signal_journal.csv"
OUT = DATA / "v231_limitup_precursor_features.csv"
META = DATA / "v231_limitup_precursor_meta.json"
MARKET = DATA / "v216_market_context.json"
NIGHT = DATA / "v216_night_session_context.json"
LATEST_RANK = DATA / "latest_rank.csv"
INTRADAY = DATA / "intraday_snapshot.csv"


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception:
        try:
            return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        except Exception:
            return pd.DataFrame()


def first_col(row: pd.Series, names: list[str], default: Any = "") -> Any:
    for n in names:
        if n in row.index:
            v = row.get(n)
            if v is not None and str(v).strip() != "":
                return v
    return default


def num(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, (list, tuple, dict)):
            return default
        s = str(x).strip().replace(",", "").replace("%", "")
        if s in ("", "-", "nan", "None", "null"):
            return default
        return float(s)
    except Exception:
        return default


def clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except Exception:
        return lo


def market_value(ctx: Dict[str, Any], keys: list[str], default: float = 0.0) -> float:
    for k in keys:
        cur: Any = ctx
        ok = True
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok:
            val = num(cur, None)  # type: ignore[arg-type]
            if val is not None:
                return float(val)
    return default


def row_to_feature(row: pd.Series, market_ctx: Dict[str, Any], night_ctx: Dict[str, Any]) -> Dict[str, Any]:
    code = str(first_col(row, ["代號", "股票代號", "stock_id", "code"], "")).strip()
    name = str(first_col(row, ["名稱", "股票名稱", "name"], "")).strip()
    date = str(first_col(row, ["日期", "date"], TODAY)).strip() or TODAY
    latest_time = str(first_col(row, ["最新時間", "驗證時間", "首次時間", "time", "updated_at"], NOW)).strip() or NOW

    first_price = num(first_col(row, ["首次價格", "first_price", "買入價", "左側試單價"], 0))
    verify_price = num(first_col(row, ["驗證價格", "目前價格", "盤中現價", "current_price", "price"], 0))
    if verify_price <= 0:
        verify_price = first_price

    ret = num(first_col(row, ["驗證報酬%", "目前報酬%", "報酬%", "盤中漲跌幅", "change_pct"], 0))
    max_ret = num(first_col(row, ["驗證最高報酬%", "最高報酬%", "最高漲幅%", "max_profit_pct"], ret))
    drawdown = num(first_col(row, ["驗證最大回撤%", "最大回撤%", "max_drawdown_pct"], 0))
    amp = abs(max_ret - drawdown)

    # If no price return but prices exist, approximate return from first->current.
    if abs(ret) < 0.0001 and first_price > 0 and verify_price > 0:
        ret = (verify_price / first_price - 1.0) * 100.0

    limit_distance = max(0.0, 10.0 - max_ret)  # Taiwan normal limit approximation.
    near_high_flag = 1 if (max_ret >= max(ret, 0) - 0.3 and max_ret >= 2.0) else 0

    ai_score = num(first_col(row, ["AI總分", "v223最終分", "v222最終智能分", "v221最終智能分", "調權後分"], 50), 50)
    strength = num(first_col(row, ["即時強度分", "盤中強度分", "最高即時強度分"], 0))
    risk = num(first_col(row, ["風險分", "v223風險分", "v222新聞風險分"], 30), 30)
    volume = num(first_col(row, ["盤中成交量", "成交量", "volume"], 0))

    status = str(first_col(row, ["目前狀態", "v212生命週期狀態", "交易員訊號", "目前決策"], ""))
    decision = str(first_col(row, ["目前決策", "v223最終訊號", "v222最終進場訊號", "v221最終進場訊號"], ""))
    result = str(first_col(row, ["結果分類", "盤後驗證結果", "驗證狀態"], ""))
    stock_type = str(first_col(row, ["股票型態", "股型態", "產業", "industry"], ""))

    # Feature scoring. These are collection features, not final buy rules.
    short_speed_score = clip(max(ret, 0) * 13 + max(max_ret, 0) * 6 + (20 if near_high_flag else 0))
    volume_jump_score = clip((math.log10(max(volume, 1)) * 12 if volume > 0 else 0) + max(strength, 0) * 0.35)
    reattack_score = clip((20 if "二次" in status + decision else 0) + max(max_ret - max(ret, 0), 0) * 8 + max(strength, 0) * 0.25)

    market_score = market_value(market_ctx, ["market_env_score", "score", "market.score"], 50)
    night_risk = market_value(night_ctx or market_ctx, ["night_risk_score", "risk_score", "night.risk_score"], 50)
    event_score = num(first_col(row, ["v222事件可信度", "v221即時事件分", "時事題材分"], 0))

    data_quality = 0
    data_quality += 25 if verify_price > 0 else 0
    data_quality += 15 if first_price > 0 else 0
    data_quality += 15 if latest_time else 0
    data_quality += 10 if abs(ret) > 0.0001 or abs(max_ret) > 0.0001 else 0
    data_quality += 10 if ai_score > 0 else 0
    data_quality += 10 if market_score > 0 else 0
    data_quality += 10 if decision or status else 0
    data_quality += 5 if name else 0
    data_quality = int(clip(data_quality))

    precursor_score = clip(
        short_speed_score * 0.28
        + volume_jump_score * 0.22
        + reattack_score * 0.18
        + max(0, ai_score - 45) * 0.35
        + max(0, 60 - risk) * 0.18
        + max(0, market_score - 50) * 0.22
        + max(0, event_score) * 0.12
        - max(0, night_risk - 60) * 0.22
    )

    if max_ret >= 8.0 or limit_distance <= 2.0:
        label = "🔥 接近漲停/高溫樣本"
    elif precursor_score >= 75:
        label = "🚀 前兆強"
    elif precursor_score >= 60:
        label = "🟡 前兆觀察"
    elif data_quality < 50:
        label = "⚪ 資料不足"
    else:
        label = "⚪ 一般樣本"

    reasons = []
    if max_ret >= 3: reasons.append("最高報酬拉升")
    if strength >= 60: reasons.append("即時強度高")
    if volume_jump_score >= 55: reasons.append("量能資料偏強")
    if market_score >= 70: reasons.append("大盤環境支持")
    if event_score >= 40: reasons.append("事件題材支持")
    if risk >= 70: reasons.append("風險偏高")
    if not reasons: reasons.append("一般蒐集")

    key = f"{date}_{code}" if code else f"{date}_{name}_{latest_time}"
    return {
        "蒐集時間": NOW,
        "樣本Key": key,
        "日期": date,
        "最新時間": latest_time,
        "代號": code,
        "名稱": name,
        "股票型態": stock_type,
        "目前狀態": status,
        "目前決策": decision,
        "結果分類": result,
        "首次價格": round(first_price, 4),
        "目前/驗證價格": round(verify_price, 4),
        "驗證報酬%": round(ret, 4),
        "驗證最高報酬%": round(max_ret, 4),
        "驗證最大回撤%": round(drawdown, 4),
        "漲停距離%": round(limit_distance, 4),
        "短線漲速分": round(short_speed_score, 2),
        "量能跳升分": round(volume_jump_score, 2),
        "二次攻擊分": round(reattack_score, 2),
        "日內振幅%": round(amp, 4),
        "是否貼近日內高": near_high_flag,
        "大盤背景分": round(market_score, 2),
        "夜盤風險分": round(night_risk, 2),
        "AI/智能分": round(ai_score, 2),
        "風險分": round(risk, 2),
        "事件分": round(event_score, 2),
        "漲停前兆分": round(precursor_score, 2),
        "漲停前兆候選": label,
        "前兆蒐集原因": " / ".join(reasons),
        "資料品質分": data_quality,
    }


def main() -> None:
    market_ctx = load_json(MARKET)
    night_ctx = load_json(NIGHT)
    journal = load_csv(JOURNAL)
    rows = []

    if not journal.empty:
        for _, r in journal.iterrows():
            rows.append(row_to_feature(r, market_ctx, night_ctx))

    # Fallback: if journal is empty, create light samples from latest rank / intraday snapshot.
    if not rows:
        rank = load_csv(LATEST_RANK)
        intra = load_csv(INTRADAY)
        base = rank if not rank.empty else intra
        for _, r in base.head(200).iterrows():
            rows.append(row_to_feature(r, market_ctx, night_ctx))

    new_df = pd.DataFrame(rows)
    if new_df.empty:
        new_df = pd.DataFrame(columns=["蒐集時間", "樣本Key", "日期", "代號", "名稱", "資料品質分", "漲停前兆分", "漲停前兆候選"])

    old = load_csv(OUT)
    if not old.empty:
        combined = pd.concat([old, new_df], ignore_index=True)
    else:
        combined = new_df.copy()

    if "樣本Key" in combined.columns:
        combined = combined.drop_duplicates(subset=["樣本Key"], keep="last")
    # Sort hot samples and latest first.
    for c in ["漲停前兆分", "資料品質分"]:
        if c in combined.columns:
            combined[c] = pd.to_numeric(combined[c], errors="coerce").fillna(0)
    if "最新時間" in combined.columns:
        combined["_sort_time"] = combined["最新時間"].astype(str)
    else:
        combined["_sort_time"] = ""
    combined = combined.sort_values(["日期", "漲停前兆分", "資料品質分", "_sort_time"], ascending=[False, False, False, False])
    combined = combined.drop(columns=["_sort_time"], errors="ignore")
    combined.to_csv(OUT, index=False, encoding="utf-8-sig")

    meta = {
        "updated_at": datetime.now(TZ).isoformat(),
        "status": "ok",
        "source_journal_exists": JOURNAL.exists(),
        "new_rows": int(len(new_df)),
        "total_rows": int(len(combined)),
        "hot_rows": int((pd.to_numeric(combined.get("漲停前兆分", pd.Series(dtype=float)), errors="coerce").fillna(0) >= 60).sum()) if not combined.empty else 0,
        "avg_quality": float(round(pd.to_numeric(combined.get("資料品質分", pd.Series(dtype=float)), errors="coerce").fillna(0).mean(), 2)) if not combined.empty else 0,
        "output": str(OUT),
    }
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
Patch current app.py to add a hard 5-second browser refresh and a live data status block.
Run from repo root:
    python tools/patch_app_autorefresh_v224.py
The script is idempotent.
"""
from __future__ import annotations

from pathlib import Path

APP = Path("app.py")
MARK = "# === v2.24 LIVE AUTO REFRESH PATCH ==="

INSERT_AFTER_IMPORTS = r'''
# === v2.24 LIVE AUTO REFRESH PATCH ===
def _v224_hard_refresh(seconds: int = 5) -> None:
    """Force browser refresh so Streamlit does not only rerun stale cached data."""
    try:
        st.components.v1.html(
            f"""
            <script>
            setTimeout(() => {{ window.parent.location.reload(); }}, {max(1, int(seconds)) * 1000});
            </script>
            """,
            height=0,
        )
    except Exception:
        pass


def _v224_show_live_status() -> None:
    import json as _json
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from pathlib import Path as _Path
    p = _Path("data/live_intraday_meta.json")
    if not p.exists():
        st.sidebar.warning("v2.24 live meta 尚未產生")
        return
    try:
        meta = _json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        st.sidebar.warning(f"v2.24 live meta 讀取失敗：{exc}")
        return
    updated = meta.get("updated_at", "")
    age = "-"
    try:
        tw = _tz(_td(hours=8))
        dt = _dt.fromisoformat(str(updated).replace("Z", "+00:00")).astimezone(tw)
        age = f"{int((_dt.now(tw) - dt).total_seconds())}s"
    except Exception:
        pass
    st.sidebar.divider()
    st.sidebar.header("⚡ v2.24 即時同步")
    st.sidebar.write("live最後更新：", updated or "尚無")
    st.sidebar.write("資料年齡：", age)
    st.sidebar.write("有效價格檔數：", meta.get("valid_price_count", 0))
    st.sidebar.caption("此處看的是 data/live_intraday_meta.json，不是單純畫面刷新時間。")
# === /v2.24 LIVE AUTO REFRESH PATCH ===
'''

CALL_BLOCK = r'''
# === v2.24 LIVE AUTO REFRESH CALL ===
_v224_hard_refresh(5)
_v224_show_live_status()
# === /v2.24 LIVE AUTO REFRESH CALL ===
'''


def main() -> int:
    if not APP.exists():
        raise SystemExit("app.py not found; run this from repo root")
    text = APP.read_text(encoding="utf-8")
    if MARK not in text:
        anchor = "import streamlit as st"
        if anchor not in text:
            raise SystemExit("Cannot find 'import streamlit as st' in app.py")
        text = text.replace(anchor, anchor + "\n" + INSERT_AFTER_IMPORTS, 1)
    if "# === v2.24 LIVE AUTO REFRESH CALL ===" not in text:
        anchor2 = "st.sidebar.header(\"資料狀態\")"
        if anchor2 in text:
            text = text.replace(anchor2, CALL_BLOCK + "\n" + anchor2, 1)
        else:
            # fallback: after title
            text = text.replace("st.title(", CALL_BLOCK + "\nst.title(", 1)
    APP.write_text(text, encoding="utf-8")
    print("patched app.py with v2.24 auto refresh/live status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

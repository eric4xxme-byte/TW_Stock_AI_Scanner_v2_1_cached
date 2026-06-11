# 台股 AI Scanner v2.1 Cached

這版把系統拆成兩層：

- `scanner_daily.py`：後台抓資料、分析、產出 CSV/JSON
- `app.py`：Streamlit 前台，只讀取 `data/latest_*.csv` 顯示畫面

這樣網站打開時不再即時掃 30 檔股票，速度更快、也更穩定。

## 手動產生資料

```bash
pip install -r requirements.txt
python scanner_daily.py --limit 30 --chip-limit 10
streamlit run app.py
```

## 建議設定

GitHub / Streamlit / 本機可設定環境變數：

```bash
FINMIND_TOKEN=你的 FinMind token
```

有 token 會讓法人與融資融券資料更穩。

## 輸出檔案

掃描後會產生：

- `data/latest_rank.csv`
- `data/latest_risk.csv`
- `data/latest_price_history.csv`
- `data/latest_meta.json`

## GitHub Actions

本專案包含 `.github/workflows/daily_scan.yml`，可每天台灣時間約 18:30 自動產生最新資料並 commit 回 GitHub。

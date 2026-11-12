# 最小報價測速程式

在專案根目錄執行（Windows / macOS）：

```sh
conda run --no-capture-output -n tss python quote_probe/quote_probe.py
```

預設商品沿用 `config/runtime.json` 的 `signal_code`，也可以指定：

```sh
conda run --no-capture-output -n tss python quote_probe/quote_probe.py --code TMFR1
```

- 沿用專案 `.env` 的 `SHIOAJI_API_KEY`、`SHIOAJI_SECRET_KEY`，不需要 CA。
- 模式只讀取 `config/runtime.json`，也可用 `--runtime-config 路徑` 指定；Mock 不登入。
- 每秒直接呼叫 `api.snapshots()` 取得最新價格，不經 DB、monitor 或 Dashboard；這是快照輪詢，並非逐筆 tick 訂閱。
- 官方將 `api.ticks()` 定位為歷史查詢，不適合盤中反覆輪詢；即時逐筆成交應使用訂閱。參見 [官方歷史行情文件](https://sinotrade.github.io/tutor/market_data/historical/)。
- `api_ms`：單次 API 呼叫耗時；`quote_at`：券商資料時間（台北）；`age_ms`：顯示時刻與資料時間的差值。
- `age_ms` 包含沒有新成交的等待時間、券商資料更新與本機時鐘誤差，不能視為純網路延遲。收盤後價格可能不變，應在交易時段測試。
- 一次只送一筆請求；若回應超過一秒，等回覆才繼續，不堆積請求。逾時或錯誤即停止，不自動重新登入。
- 會額外使用一個 session；全身分合計上限為 5，實際既有使用量未在此查核。按 Ctrl+C 停止並嘗試登出。

本程式測量快照 API 的回應速度；主系統 tick 訂閱、資料庫同步、Dashboard 顯示與下單延遲仍須分別測量。

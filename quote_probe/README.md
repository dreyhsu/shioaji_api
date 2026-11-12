# 小台即時 tick 訂閱測速

在專案根目錄執行（Windows / macOS）：

```sh
conda run --no-capture-output -n tss python quote_probe/quote_probe.py
```

預設訂閱小台近月 `MXFR1`，收到每筆成交即顯示。也可以指定其他期貨商品：

```sh
conda run --no-capture-output -n tss python quote_probe/quote_probe.py --code TMFR1
```

執行 60 秒後自動登出：

```sh
conda run --no-capture-output -n tss python quote_probe/quote_probe.py --seconds 60
```

- 沿用專案 `.env` 的 `SHIOAJI_API_KEY`、`SHIOAJI_SECRET_KEY`，不需要 CA。
- 模式只讀取 `config/runtime.json`，也可用 `--runtime-config 路徑` 指定；Mock 不登入。
- 使用 `api.set_on_tick_fop_v1_callback()` 註冊回呼，再以 `api.subscribe(contract, quote_type=sj.QuoteType.Tick)` 訂閱；不使用快照或歷史 tick 查詢，不經 DB、monitor 或 Dashboard。
- 此診斷程式的預設商品固定為 `MXFR1`，不修改主系統商品設定。啟動時顯示 `target_code`，每筆顯示 `tick_code`；比較手機價格時，請確認商品、月份及成交價欄位相同。
- `price`：逐筆成交價；`volume`：該筆成交量；`quote_at`：券商成交時間（台北）；行首為電腦接收時間。
- `receive_lag_ms`：電腦接收時間減去券商成交時間，包含券商傳送、網路、SDK 派送及兩端時鐘誤差，不能視為純網路延遲。
- `display_queue_ms`：回呼接收後至命令列格式化完成的等待時間，不包含終端實際繪製時間。回呼只入列，避免終端輸出阻塞 SDK。
- `simtrade=1` 表示試撮。沒有新成交時不重複顯示價格；這是成交價訂閱，不是五檔買賣價訂閱。
- 主執行緒持續等待 tick，預設直到 Ctrl+C 才結束；指定 `--seconds` 才會定時停止。沒有新成交不會讓程式退出。
- 最多暫存 1000 筆；終端太慢造成滿列時明確報錯並結束，不靜默遺漏行情。
- 會額外使用一個 session；全身分合計上限為 5，實際既有使用量未在此查核。停止時嘗試取消訂閱、登出，不自動重新登入。

本程式直接接收券商行情；主系統資料庫同步、Dashboard 顯示與下單延遲仍須分別測量。

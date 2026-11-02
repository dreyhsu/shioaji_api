# 獨立 trade 服務


模式改由 `config/runtime.json` 控制（預設 Mock），不接受模式命令列參數。整體啟動與設定以 [runtime_and_dashboard.md](runtime_and_dashboard.md) 為準；以下個別服務命令只會在設定為非 Mock 時連線。
2026-11-01。使用 conda `tss`，支援 macOS／Windows；正式部署於 Windows laptop。本次只做離線開發與測試，未登入券商、送單或部署。

## 分工

```text
kbar_fetcher（歷史回補） ─┐
                         ├→ market_fetchers.db → monitor → tmf.db → trade
 tick_fetcher（即時行情） ┘                                  ↑
                                                    Dashboard 交易指令
```

`src/trade.py` 是常駐本機 API，預設 `127.0.0.1:8766`，一個程序、一個券商 session。`trade_service.py` 共用既有委託／成交／部位邏輯，但不註冊行情 callback、不訂閱 tick、不查歷史行情。Dashboard 正式／模擬模式改用 `TradeClient`，不再建立 `TradingService`；`runtime.json` 的 Mock 模式 保留原來完全離線的回放引擎。

API handler 只寫指令或讀背景快照；所有券商 I/O 由同一背景執行緒依序處理。開啟 Dashboard、查詢訂單／保證金／合約不會額外登入或觸發券商查詢。背景委託狀態 RPC 依 `market_health.json` 的 `order_sync_seconds` 節流（預設 60 秒），其間讀取 SDK 主動回報維護的本機 trade cache；保證金也由背景更新。

## 啟動

在專案根目錄開不同視窗，以下命令會連線券商；`simulation` 不是離線 Mock：

```text
conda run --no-capture-output -n tss python -X utf8 -u src/trade.py --db data/tmf.db
conda run --no-capture-output -n tss python -X utf8 -u src/dashboard.py --db data/tmf.db --trade-url http://127.0.0.1:8766
```

- 模式從 `config/runtime.json` 讀取；正式環境須在檔案設定 `production` 並設定本機 CA。Dashboard 的模式、DB 路徑、商品代碼必須與 trade 一致，否則拒絕交易指令。
- `--config` 指向 `config/market_services.json`，控制排程與交易日曆；`--health-config` 控制健康驗證及委託核對間隔；`--settings` 指向自動交易／保本設定。`--no-telegram` 關閉成交通知。
- trade 只允許 loopback 位址、單一 worker，不可使用 reload／多 worker 開出多個 session。健康檔與鎖沿用 `.health/<DB檔名>/trading-<code>.json` 的既有路徑格式，與舊內嵌交易服務互斥。
- 一般登入窗口為 08:44–13:46、14:59–次日 05:01，仍須經日曆驗證。休息期間 API 常駐且不登入；登出前盡可能核對狀態，不自動平倉、撤單或刪除不明委託。SDK 阻塞可能使實際登出晚於排程。
- 非預期斷線預設暫停；只有 `trading_reconnect_enabled=true` 才啟用自動重連，啟用前仍須券商模擬驗收。重新登入必須核對券商委託、部位與新鮮 DB 行情。2026-11-02 起，登出結果僅作稽核，不再阻擋後續登入。
- 原始 session 稽核位於統一 DB 旁 `logs/trade_events.jsonl`，monitor 會彙整到 `monitor_fetch_events`／`logs/monitor.log`。沒有查詢 `api.usage()`，不代表已查核實際連線用量。

## 必要行情資料

TMFR1 曲線與實際月份合約有不同價格，不能互相當作固定停損／停利的價格。

1. tick fetcher 的 `--codes` 同時指定 `TMFR1` 與實際月份合約，產生兩者最新價與收定候選。
2. kbar fetcher 的 `--codes` 也指定同一組代碼。現在同一個 kbar 程序可為多個合約各自維護回補起點，仍只使用一個 session、不訂閱 tick。
3. 每個代碼啟動一個 monitor，使用同一個 acquisition DB 與 unified DB，分別用 `--code` 指定 TMFR1／月份合約。多個 monitor 不登入券商，各自的健康檔／鎖按商品區分。
4. 先啟動 fetcher，再啟動 monitor 與 trade。Dashboard 可從 trade 的合約快照取得實際月份合約代碼；換月時必須更新 fetcher／monitor 的代碼設定。Dashboard 的「訂閱合約」目前只更新 trade 的本機關注清單，不會自動變更 fetcher 啟動參數。

新來源的候選完整性與 broker 核對限制維持不變：尚未核對的即時分鐘會讓 monitor 保持 `staged_only`／`degraded`，trade 不會繞過健康閘門。目前 broker 修復採節流，不能宣稱每分鐘都會及時取得可交易資料；來源整體驗收與實際時效仍須後續券商模擬驗證。

## 交易判斷

- 自動曲線進場使用 DB 的相鄰已收盤 1 分 K。逐一處理未判斷分鐘，monitor 暫時 `checking` 不重設進場起點。超過收盤後 60 秒的進場訊號略過；新鮮 TMFR1 報價還須位於突破側。停用或修改曲線會取消舊啟用版本，送單前再次核對版本。
- 目前沒有完整逐筆歷史可驗證「收盤前最後 10 秒的實際合約參考價」，因此未實作舊計畫中的額外價差補單模式；不宣稱已完成該項補單條件。
- 固定停損／停利與保本改用月份合約最新已驗證收定 1 分 K，曲線出場使用 TMFR1 的相鄰收定分鐘。tick 只用於新鮮度與參考價檢查，不單靠觸價執行自動交易。新成交部位不回用進場前已收定的分鐘；不合成缺少的 K 棒。
- 缺少合約收盤資料、報價過期、monitor 未就緒或券商部位／委託不一致時暫停相應動作。手動送單在指令核對與行情閘門通過後執行，無需另等下一根訊號；保存曲線本身不等於啟用交易。
- 進場／出場沿用送單前持久化 claim；`submitting`／`unknown` 不在重啟或重連後自動重送。

## 指令與快照 API

| API | 用途 |
|---|---|
| `GET /api/snapshot` | 背景保存的 status、contracts、margin、orders；不查券商 |
| `POST /api/commands` | 接受具唯一 `id` 的指令，回傳 202 與持久化狀態 |
| `GET /api/commands/{id}` | 查詢原指令結果，不重新執行 |
| Dashboard `GET /api/trading/commands/{id}` | 代理查詢同一個指令 |

POST 格式為 `{id, name, target, payload}`。支援 `place_order`、`arm_curve`、`cancel_curve`、`subscribe`、`cancel_entry`、`update_levels`、`update_risk`、`update_curve`、`select_curve`。操作既有委託時 `target` 是 managed order ID；其他操作為 null。payload 與既有 Dashboard 欄位一致，經共用模型驗證。

`trade_commands` 保存 `accepted → processing → succeeded / failed / unknown`。同 ID／同內容回傳原結果；同 ID／不同內容回傳 409。程序中斷遺留的 `processing` 改為 `unknown`，禁止自動重送；未處理的 `accepted` 可由新程序接續。結果不明的交易需人工核對券商及本機 claim，不能直接刪除紀錄解除保護。

Dashboard 使用 `Idempotency-Key` 轉交指令 ID；瀏覽器在 sessionStorage 保留尚未確認的請求，逾時後重試沿用同 ID。存在未確認請求時阻止其他新操作，並提供指令 ID 供查詢。關閉 Dashboard 不會停止 trade 或取消已啟用的條件。

## 離線驗證

```text
conda run -n tss pytest -q src/test_independent_trade.py src/test_market_fetchers.py src/test_trading_recovery.py src/test_trading.py src/test_curve_entry.py src/test_dashboard.py
```

測試使用暫存 DB、注入時鐘與 MockBroker，封鎖真實券商及網路。Windows 原生行為、正式 CA、跨 session 互動及實際券商行情／委託核對仍待驗收。

### 本次驗證結果

- conda `tss`：獨立 trade、fetcher、Dashboard、曲線進場、委託、重連、monitor、watchdog、風控設定、DB、行情健康與 Mock routes 的相關測試 **286 項通過**（16.87 秒）。這是相關測試組合，未宣稱全套通過。
- 新 trade 整合測試涵蓋只讀查詢不登入／不查券商、同 ID 去重及內容衝突、送單已接受但回應遺失不重送、processing 重啟轉 unknown、排程外不登入、登出不平倉、登入跨登出時間先釋放 session、登出失敗仍可在下次排程登入、tick 越線不提前出場、曲線版本變更、monitor checking 不重設起點，以及實際報價與收定判斷價格分離。
- JavaScript 語法、Python 語法與 diff 空白檢查通過；另以離線瀏覽器環境替身驗證「回應遺失 → 頁面重載 → 同指令 ID 重試」，不產生新指令 ID。
- 未登入券商、未送單、未部署；實際券商多 session、CA、回報／部位跨 session 核對、資料時效與 Windows 原生行為仍待驗收。

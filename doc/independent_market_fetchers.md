# 行情回補、即時擷取與 monitor 統整（2026-11-01 更新）


模式改由 `config/runtime.json` 控制（預設 Mock），不接受模式命令列參數。整體啟動與設定以 [runtime_and_dashboard.md](runtime_and_dashboard.md) 為準；以下個別服務命令只會在設定為非 Mock 時連線。
兩個 fetcher 各自持有一個 Shioaji session，可獨立執行、不建立交易服務、不送單。`kbar_fetcher.py` 專做歷史回補與核對，`tick_fetcher.py` 專做即時 tick、最新價及已收盤 1 分 K 候選。`monitor.py` 不登入券商，統整兩者資料並更新指標；Dashboard 與 trade 統一讀 monitor 寫出的 DB。獨立 trade 與 Dashboard 代理已實作，操作見 [independent_trade.md](independent_trade.md)。

## 啟動

在專案根目錄執行，macOS 和 Windows 使用同樣參數。以下是操作說明，開發驗證沒有執行這些真實連線命令。

```text
conda run --no-capture-output -n tss python -X utf8 -u src/tick_fetcher.py --codes TMFR1 TMFK6
conda run --no-capture-output -n tss python -X utf8 -u src/kbar_fetcher.py --codes TMFR1
conda run --no-capture-output -n tss python -X utf8 -u src/monitor.py --fetcher-db data/market_fetchers.db --db data/tmf.db
```

兩者預設共用獨立的 `data/market_fetchers.db`，可用 `--db` 指定另一個測試 DB。模式由 `config/runtime.json` 設定；`simulation` 仍是**券商連線模式**，需要 `.env` 憑證，不是離線 Mock。正式模式須在 runtime 設定檔明確指定 `production`。

- `--config` 指定設定檔，預設 `config/market_services.json`。
- `--once` 做一輪生命週期後登出；tick 是非同步推送，一輪可能還沒有報價，不適合當成即時行情可用性的驗收。
- K 線的 `--no-backfill` 只測試排程登入／登出，不訂閱即時行情，也不查歷史 API；分鐘彙整測試改由 tick 服務執行。
- Tick 與 K 線均支援多個 `--codes`；K 線在同一 session 內依合約維護獨立回補游標。連續合約報價的實際 underlying code 會映射回所設定代碼。
- Ctrl+C 正常關閉；排程登出只結束連線，服務不退出。

## 設定與輸出

`scheduled_login_times` 預設 `["08:44", "14:59"]`；`scheduled_logout_times` 預設 `["13:46", "05:01"]`。時間以台北時間判斷，無論主機時區為何。設定在啟動時讀取，日曆每一輪讀取。

必須有覆蓋目前時間的已驗證日曆，且登入窗口和實際交易時段相交才會登入；週末及假日不靠星期幾猜測。日曆不足時狀態為 `calendar_unavailable`，休息為 `scheduled_closed`。

資料表：

| 表 | 內容 |
|---|---|
| `market_latest_quotes` | 每代碼／模式的最新報價，最多每秒更新一次；不存逐筆 tick 歷史 |
| `market_stream_bars` | 候選 `stream` K 棒與 `broker` 歷史 K 棒分列，含完整性、來源 instance／generation |
| `market_bar_checks` | 同分鐘兩來源 OHLCV／amount 比對，`match`／`mismatch`／`incomplete` 及差異 |
| `market_fetcher_progress` | 持久化驗證起點，重啟不因新資料而忘掉較早缺口 |

fetcher 不會覆寫策略用 `minute_bars`；由 monitor 負責統一寫入。monitor 透過唯讀連線讀取來源 DB，不會自動建立缺少的來源，也不會改用券商 API fallback。`complete=1` 是資料範圍屬性，**不代表交易已就緒**。正式遷移前仍需驗證 broker 與 stream 一致性、來源新鮮度與交易閘門。

健康檔位於 DB 旁 `.health/<DB檔名>/tick-fetcher.json` 和 `kbar-fetcher.json`；分別使用獨占鎖，一個角色／DB 只允許一個程序，兩種角色可以同時運行。健康檔含模式、PID、世代、錯誤種類、重試狀態、行情時間及 `staged_only: true`。

monitor 的例行匯入／指標更新以 `operation: derive` 表示，不將上一輪已完成的 `healthy` 改成短暫 `checking`；首次驗證前仍不宣稱就緒。共用健康閘門同時檢查 `cycle_completed_at` 的時效與 `operation_started` 的操作逾時，避免工作卡住卻因心跳刷新而永遠維持就緒。完成本輪後才發布新的就緒結果；失敗仍發布 `failed`。Dashboard 不會因剛好讀到例行更新而顯示行情未就緒。

日誌位於 DB 旁 `logs/tick_fetcher.log`、`kbar_fetcher.log`，UTC 時間標記為 `Z`，保留 14 天輪替；排程及行情時間仍為台北時間。日誌不輸出憑證或任意例外字串。

Tick 回呼只入記憶體佇列，由主執行緒寫入 DB；無新 tick 時不更新行情時間。沒有成交不等於斷線：開盤時報價過舊會顯示 `stale`、`stream_fresh: false`，不會僅因單一或全部合約安靜就重新登入。`stream_recovery_seconds` 保留為長時間無新鮮 tick 的診斷門檻（預設 60 秒），對應健康檔的 `quiet_codes`，不再觸發重連。若 SDK 未回報斷線且沒有 API 錯誤，靜默故障會維持過舊狀態，需要依健康檔排查。

佇列溢位、session-down 或可辨識的連線／認證錯誤會觸發有退避的連線重建，DB 鎖定不重登。收到「操作異常，請1分鐘後再重新登入」等明確限流錯誤時，記錄為 `rate_limit`，停止該服務的 API 工作，至少等待 60 秒才重試；再次被拒絕就重新計時，已有更長退避則保留。冷卻期間不登入、不訂閱、不查歷史資料。此冷卻由各程序管理，不會自動暫停其他程序或電腦。

Tick 服務在收盤後預設等待 2 秒才發布前一分鐘；晚到 tick 會使已發布的候選分鐘失效，等待歷史修復。啟動／中斷分鐘不標完整，不為無成交分鐘補造 OHLC；保留現有 13:45 集合競價排除慣例。歷史查詢限於啟動／重連、成熟缺口修復及收盤核對，修復間隔預設 300 秒，不逐分鐘查歷史 API。SDK 超時或阻塞可能延後排程登出，健康檔會保留操作及開始時間供監督判斷。

## Monitor 統整與稽核

monitor 預設每秒檢查來源，僅在 K 棒改變時重算指標。只匯入已收盤完整分鐘，`broker` 優先於 `stream`；同時同步最新價，保留原始行情時間。來源 DB 與模式綁定在統一 DB，切換模式或來源請使用另一個統一 DB，避免模擬與正式資料混用。策略仍以 1 分 K 收定後判斷，獨立 trade 讀取同一 DB 的指標、`trendlines` 曲線與 `minute_bars`；Dashboard order 需求交由 trade API 執行。

依 2026-11-02 決策，broker 歷史 K 接上 tick K 尾段，且日曆、行情時效、目前 tick instance／generation、fetcher 心跳與報價新鮮度檢查通過後，monitor 可為 `healthy`；不再逐根等待 broker 核對。明確標記 incomplete 的分鐘不匯入策略 K，但僅列為待修復，不單獨阻擋下單；`bars.pending_repair_count/sample` 保存這些分鐘，`missing_count/sample` 仍保留實際缺棒，`blocking_missing_count` 為排除已知 incomplete 後的阻擋數。未解釋缺棒、串流中斷／過舊或世代不符仍關閉閘門。`unverified_stream_minutes` 保留尚未核對的數量，不單獨阻擋就緒；`stream_error` 記錄無法採用 tick 尾段的原因，`staged_only` 表示候選仍被阻擋。broker 核對仍持續且優先覆蓋；來源整體驗收仍待後續完成，trade 共用此閘門。五檔 fetcher 串接未完成，monitor 不再訂閱五檔或用 snapshot fallback，健康檔明確標示。

來源 DB 旁 `logs/tick_events.jsonl`／`logs/kbar_events.jsonl` 保留原始稽核事件；獨立 trade 的事件另從統一 DB 旁 `logs/trade_events.jsonl` 讀取，使用 Asia/Taipei 時間（角色、模式、instance、generation）：

- 登入／登出：started、succeeded、failed 分開記錄；fetcher 使用嚴格登出回報，例外或明確 false 回傳不得記成功。登出失敗會標示 `logout_unconfirmed`，同一程序停止新增登入，待人工確認舊連線釋放。
- 回補：記錄查詢起訖 timestamp、排他上界、已收盤篩選截止、實際寫入資料起訖與筆數；失敗保留相同查詢區間及錯誤分類／型別。
- 即時失敗：session-down、佇列溢位、過舊或缺少 tick、API／DB 錯誤；恢復另記事件。啟動給予新鮮度寬限，持續安靜不會每輪重複記錄過舊事件，也不會自行重登。

monitor 即使晚啟動或重啟，仍會依原始時間補讀，事件 ID 去重、讀取位置持久化。彙整輸出位於統一 DB 旁 `logs/monitor.log`，並存入 `monitor_fetch_events`，可用 timestamp／role／event 查詢。資料庫稽核表是持久化查核依據，文字日誌寫入失敗時可由表內 document 重建；原始事件檔目前不自動輪替，不應刪除尚未彙整的事件。

`--no-orderbook` 保留相容參數，但 monitor 所有模式都不自行抓五檔。`--backfill-days` 在 monitor 僅是空庫尚未取得 fetcher 起點時的驗證範圍，不會發出網路回補；空庫取得 kbar fetcher 的持久化起點後跟隨該範圍。

## 測試怎麼寫

```text
conda run -n tss python -m pytest -q src/test_market_fetchers.py
conda run -n tss python -m pytest -q
```

測試使用 contextmanager 假 API、可控制的台北時鐘／單調時鐘及暫存 SQLite；根目錄 conftest 在收集測試前封鎖私人憑證、券商與網路。測試不依賴 `sleep()` 或真實交易時間。

1. **設定／排程**：HH:MM、型別、範圍、登入登出交錯、UTF-8 BOM／相對路徑；08:44、14:59、13:46、05:01 邊界，週六清晨、週末、日曆到期、客製登入時間、睡眠跨時段。
2. **Tick／來源**：連續合約 alias、帶 exchange 的 callback、時區轉換、每秒合併寫入、過期／逆序／非法／試撮 tick、不偽造新時間，舊連線 callback 被丟棄。
3. **1 分 K**：依事件時間算 OHLCV、收盤延遲、啟動半分鐘、不合成空棒、集合競價排除、04:59 最後一分鐘、晚到 tick 失效，以及 broker／stream 一致與差異記錄。
4. **故障及持久化**：兩 session 獨立、一個停止另一個仍產資料、重複程序阻擋、登入／session-down 退避、休市不重試、佇列溢位可見、DB 鎖不重登且待寫資料保留、缺口驗證起點重啟仍保留、既有策略資料不變。
5. **API 次數**：即時更新不輪詢歷史 API；啟動、缺口與收盤核對才查，失敗同樣受節流約束，不因 final-close 狀態每輪重試。

離線測試不能證明實際券商 entitlement、登入／登出跨 session 是否互相影響、stream 與 broker OHLCV 一致，或 Windows 原生行為。這些須後續在隔離券商環境及 Windows laptop 驗收，不在本次開發中自動登入或部署。

### 首次拆分驗證結果（2026-10-31）

- macOS `conda tss`：新增抓取測試 49 項通過；最終完整離線套件 **760 項通過**（178.27 秒）。
- 五個新增 Python 檔案及 JSON 設定語法檢查通過，新增檔案沒有行尾空白。
- 仍有既有 `pytest_freezegun` 的 distutils deprecation warnings；未宣稱已完成券商連線、一致性比對或 Windows 原生驗收。

### 重連修正驗證（2026-10-31）

- 抓取服務、RecoveryGate、monitor／交易重連與行情健康測試共 **108 項通過**；涵蓋限流後 60 秒邊界、連續拒絕重新計時、較長退避不縮短、無成交不重登、多合約持續寫入及實際 session-down 後重新訂閱。
- Python 語法與行尾空白檢查通過。
- 完整套件執行完成 660 項後長時間無進展，已中止；附近的一項交易一致性測試獨立執行通過。原因尚未確認，不能將首次拆分的 760 項結果當成本次完整回歸結果。
- 本次沒有登入券商或在 Windows 原生環境執行。

### 資料分工與 monitor 遷移驗證（2026-11-01）

- conda `tss`：fetcher、monitor、DB、行情健康、watchdog、Dashboard 與 mock routes 的相關離線測試 **177 項通過**（13.37 秒）。涵蓋回補起訖、session 時間、失敗原因、登出失敗不誤報成功且阻止新增登入、原始事件補讀去重、來源失效撤回與 broker 修復優先。
- 共用測試 `frame` 保持 DataFrame／session 欄位介面，watchdog 的兩項相容問題已修正並通過回歸。
- 全套回歸執行至風控一致性測試附近長時間無進展，已結束該次測試程序；本次沒有全套通過的結論。停滯前顯示的 mock 路由案例，在獨立及上述相關測試組合中通過，尚未證實全套執行順序的影響。
- 語法與 diff 空白檢查通過；未登入券商、未送單、未部署，未做 Windows 原生驗證。

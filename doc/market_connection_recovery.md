# 行情恢復、獨立監督及正式切換

本修復不會在匯入模組時登入、送單或發送通知。開發驗證僅使用 `tss`、暫存 SQLite 和注入的模擬券商；沒有停止、重啟或更動現有正式服務。

2026-10-28 驗證：完整離線套件 610 項通過，包含本次新增 53 項故障與部署驗證；前端 JavaScript 語法檢查通過。仍有既有 `pytest_freezegun` 的 deprecation warnings。券商模擬帳號整合、正式程序角色、實際成交及跨程序登入／登出影響尚未核實，不在離線測試的證明範圍內。

後續跨平台修正：macOS 的 `tss` 完整離線套件 622 項通過，五個程式入口的 `--help` 檢查通過。新增 Windows 鎖定／子程序控制分支模擬、UTF-8 BOM 與檔案分享衝突測試；尚未在 Windows 原生主機執行這批測試，不將模擬分支測試視為 Windows 正式驗收。

## 功能與責任

| 模組 | 輸入 → 輸出 | 責任 |
|---|---|---|
| `connection_recovery` | 例外圖、可注入時鐘、策略 → 分類與下一次重試時間 | 辨識兩條例外鏈及循環；DB 錯誤優先，session 快速失敗；有上限退避、慢速斷路器；不做 I/O |
| `file_lease` | 本機鎖定檔 → 程序獨占租約 | Windows 使用 `msvcrt`、macOS 使用 `fcntl`；非阻塞、關閉釋放、不刪除鎖定檔 |
| `market_health` | 已核對時段、分鐘資料、時間 → 健康結果；健康快照 → 原子檔案 | 封閉分鐘、缺口與日曆驗證；程序租約與來源指紋；不靠行情 DB 寫入健康狀態 |
| `monitor.MonitorRuntime` | API factory、DB、時鐘 → 回補、指標與健康狀態 | 重新解析合約、綁定 callback、重訂閱委託簿；保留 visits；完整驗證前關閉交易讀取閘門 |
| `trading.TradingService` | 注入券商、逐合約 tick、健康資料 → 既有交易週期 | 不重送不明委託；重連後核對訂單與雙方向部位；tick 與 K 線獨立判斷 |
| `health_watchdog` | 唯讀 DB、兩個健康檔 → 故障／恢復事件及通知 | 獨立常駐、不登入券商、不依賴 Dashboard；事件持久化、失敗重試與節流 |
| `run_market_service` | 服務角色 → 自己建立的子程序 | monitor 卡住或死亡可重啟；不自動終止／重啟交易服務；不搜尋或操作外部 PID |
| `market_services` | 設定與唯讀資料 → 前置檢查／launchd 檔案 | 不安裝、不載入服務、不改委託；輸出具體可審查的服務設定 |

## 狀態與預設值

`config/market_health.json` 控制 API timeout、連續失敗門檻、退避、行情過期及通知間隔。錯誤設定直接拒絕啟動，不默默沿用錯誤值。設定變動需重啟相應服務；交易日曆每個工作週期重新讀取。

- session／認證失效直接排入恢復；一般傳輸連續失敗預設兩次後恢復。SDK 呼叫自身的 session 錯誤不再對同一失效 session 重試三遍。
- 預設等待 5 秒，後續指數退避最高 120 秒，連續第六次嘗試起每 600 秒一次；即使認證長期無效也不密集登入。
- monitor 自動重建行情 session；交易端先修復個別訂閱。交易端完整重新登入預設 `trading_reconnect_enabled: false`，必須通過下述券商模擬驗證後再開啟。
- 健康檔位於資料庫旁 `.health/<資料庫檔名>/`，依 monitor／trading 與商品區分。含 PID、啟動時間、Python、工作目錄、Git revision、關鍵來源 SHA-256、SDK 版本、模式與實際 DB 路徑。檔案的原子替換與獨占鎖可防止兩個相同服務接管同一資料庫／商品。
- 保留最後驗證起點；回補中途已寫入較新資料後程序死亡，也不會因最新時間變新而忘記較早的缺口。SDK 舊連線 callback 不得更新新連線的資料。
- 活躍時段恢復必須看到資料超過重連前水位。每次取資料後重新讀時鐘；05:00 後仍抓取、驗證 04:59 開始的最後一分鐘，休市不把正常舊資料誤判成斷線。
- 讀取成功但資料沒有前進也會觸發恢復；新資料已前進但中間缺洞時先回補，不反覆登入。使用額度超過門檻會放慢抓取，同時保持健康心跳。
- DB 鎖住或程式錯誤不觸發券商重新登入。SDK timeout 預設 30 秒；整個操作超過 120 秒由獨立監督發現。monitor supervisor 可終止自己建立的 monitor；交易卡住時通知人工核對，保留程序。

## 交易日曆是正式啟用前置條件

**未隨修復附上未經核實的正式交易日曆。** `config/market_calendar.example.json` 是格式範例，來源字串刻意使驗證拒絕使用；不能直接改檔名冒充已驗證日曆。

請依期交所公告及實際供應商規格建立 `config/market_calendar.json`：

- `source`：公告來源、核對日期與核對者等可追溯資訊。
- `coverage_start`／`coverage_end`：已完整核對的時間範圍，起點含、終點不含；涵蓋回補起點至預期運行期間。
- `sessions`：範圍內**所有**實際開盤區間，台北時間、無時區標記、精確到分鐘、起點含終點不含。明列跨夜、休市、補班及特殊收盤；不要只套星期一至五。區間不可重疊。
- `confirmed_empty_minutes`：僅在有可稽核的無成交證據時填入 `{ "minute_start": "...", "evidence": "..." }`；程式不推定沒有成交、不補造 OHLC，也不把空缺直接視為正常。

未設定、格式錯誤、覆蓋不足或日曆到期時仍可用一般時段作為抓取提示，但**不認定已完整恢復、不接受新倉、暫停曲線進出場**。日曆未驗證時不因安靜的 tick 反覆重建交易 session；明確 session 事件仍會記錄及依設定恢復。應在日曆到期前更新。

現有 K 棒慣例保留：日盤 13:45 集合競價不納入分鐘指標，夜盤 05:00 為區間終點。1h 最後一根標記 04:00；04:59 的 1m 棒代表到 05:00 的區間。若需要不同商品／特殊交易時段，必須同時確認 `session_bars` 的彙整規則相容。

## 交易降級與委託核對

| 情況 | 行為 |
|---|---|
| K 線過期／回補中，實際合約 tick 新鮮、券商已核對 | 固定停損、固定停利與保本仍依設定執行；曲線功能與新倉暫停 |
| 某合約 tick 過期 | 該合約不使用舊價判斷；其他合約不能替它證明行情正常；獨立通知 |
| tick 恢復 | 先等待新連線的新 tick；依最新有效價判斷，無法保證補成交在中斷期間的觸發價格 |
| 曲線進場恢復 | 更新持久化檢查基準，等待恢復後的新已收盤訊號；不補送中斷期間歷史進場 |
| 曲線出場恢復 | 保留原 `curve_check_from`，驗證資料完成後檢查中斷期間的相鄰分鐘穿越；以當下新鮮 tick 及已核對部位送出一次委託，不虛構歷史成交價 |
| 重連／重啟交易服務 | 查委託及部位；未管理的未結束委託、方向／口數差異或身分不明時不宣告可自動送單 |
| 券商接受委託但回應遺失／送單後本機死亡 | 進場在 I/O 前持久化 `submitting`，出場保留既有 claim；保留 `unknown`／`submitting`，禁止自動重送及新增新倉；人工核對才能處理 |

重連不會清除 `unknown`、把找不到的委託當成未送出、按價格猜測委託身分、取消外部委託，或擅自重建券商端保護單。部分成交後取消餘量的既有流程保留；只有確認原委託終止後才可決定剩餘平倉口數。

`/api/trading/status` 及各筆委託公開受影響的保護功能；券商端保護一律標示待核對。`/api/health` 僅讀檔，打開它不登入、不刷新工作心跳；圖表就緒與交易行情就緒分開顯示。Dashboard 啟動會啟動交易服務，無需使用者打開頁面。

## 驗證及切換順序

1. **保留現況。** 核對執行中 PID 19200 的歷史角色、各服務 PID／Python／目錄／模式與 DB；將 traceback 與啟動指紋比對。401 仍是候選原因，不是已確認根因。核對 10/27 出場的券商成交價、口數與實際持倉；本機觸發報價不等於成交價。
2. **離線驗證。** `conda run -n tss python -m pytest -q`。根目錄測試防護在收集前阻擋真實券商、網路與私人憑證；測試 DB 限暫存目錄。新增案例涵蓋精確 session 例外、包裝／循環例外、傳輸退避、停止前進、DB 鎖定、缺口、中途回補重啟、05:00 收盤、舊 callback、逐合約 tick、交易部位不一致、已接受但回應遺失的進出場、通知重試與擁有的子程序終止。
3. **券商模擬整合驗證，與離線 Mock 分開。** 使用隔離 DB、模擬帳號及非正式 port；同時執行 monitor 與交易服務，驗證 SDK 自動重連與應用層重連不互相競爭。逐項驗證 monitor logout/login 是否影響另一程序的 tick、委託回報、未成交與部分成交；實際斷網、復網、API 卡住及程序終止。沒有驗證通過前不開啟交易完整重新登入。
4. **先監督，再恢復。** 先讓 watchdog 不帶 `--notify` 寫本地事件，核對休市與活躍時段判斷；驗證通知成功、失敗重試及恢復通知後才啟用通知。此流程不需實際送單。再切換 monitor，確認重疊回補與缺口修復，最後切換交易服務及開啟完整重連。
5. **維護窗口。** 券商端核對部位、工作委託及保護；明確決定過渡期間由誰監控／執行保護，避免同時執行兩個交易服務。用 SQLite backup API 做一致性備份，保留設定與舊程式版本；不要只複製正在運行的 `.db` 而漏掉 WAL。
6. **上線驗收。** 確認健康檔 PID 是新程序、來源 hash／模式與 DB 都正確；觀察至少跨過一個正常抓取週期及一次斷線恢復，並在模擬環境完成一個跨夜至 05:00 的週期。恢復條件為缺口已有解釋、資料持續前進、逐合約 tick 新鮮、委託／部位一致，且無重複送單。

唯讀前置檢查（不核對券商，不更新資料庫）：

```sh
conda run -n tss python tools/market_services.py preflight --db data/tmf.db --code TMFR1 --mode production
```

前置檢查的 `configuration_ready` 只代表日曆範圍足夠；`runtime_verified` 固定為 false，提醒仍需人工核對券商與執行中程序。舊版本沒有健康檔時顯示未辨識，不推測它執行的是目前磁碟版本。

## 常駐方式與獨立通知

專案需同時支援 macOS 與 Windows；正式區是 Windows laptop。Dashboard、monitor、watchdog 與 supervisor 使用相同 Python 入口，程序鎖在 Windows 使用 `msvcrt`，macOS 使用 `fcntl`，不需額外安裝鎖定套件。鎖定檔不刪除，程序正常關閉或終止後由作業系統釋放；這些鎖適用於同一主機的本機資料庫目錄。

以下是部署時的啟動命令，**不是本次已執行的動作**。三個服務要由作業系統分別管理，不要放在 Dashboard 內同一個執行緒。

```sh
conda run --no-capture-output -n tss python tools/run_market_service.py --role monitor --db data/tmf.db --code TMFR1
conda run --no-capture-output -n tss python tools/run_market_service.py --role trading --db data/tmf.db --code TMFR1 --port 8765
conda run --no-capture-output -n tss python src/health_watchdog.py --db data/tmf.db --code TMFR1 --mode production --notify
```

只產生可審查的 macOS launchd 設定檔，不安裝、不啟動：

```sh
conda run -n tss python tools/market_services.py render --db data/tmf.db --code TMFR1 --mode production --output out/market-services --notify
```

安裝前建立 `data/logs`、檢查產生的 Python 路徑確實屬於 `tss`、模式／DB／port 正確、沒有舊服務衝突，再依實際部署帳號用 launchd 載入。monitor／watchdog 設為獨立重啟；交易服務 `KeepAlive=false`，意外終止需核對後人工啟動，避免無人注意的交易程序循環重啟。重連開關為啟動時設定，不支援在不重啟下變更。

### Windows 正式區

`market_services.py render` 僅產生 macOS launchd 檔；Windows 仍可執行 `preflight`，並使用上方三個跨平台啟動命令。原本的 Dashboard 命令無需變更：

```bat
conda run --no-capture-output -n tss python src/dashboard.py --code TMFR1 --port 8765
```

需要常駐時，用 Windows 工作排程器建立 monitor、trading、watchdog 三個獨立工作。先取得 `tss` 的 Python 路徑：

```bat
conda run -n tss python -c "import sys; print(sys.executable)"
```

各工作的「程式」填入上一步實際輸出的 Python 完整路徑；「開始位置」填入正式區專案資料夾，例如 `C:\Users\Drey\Documents\Python_Scripts\shioaji_api`；引數分別為：

```text
-X utf8 -u tools\run_market_service.py --role monitor --db data\tmf.db --code TMFR1
-X utf8 -u tools\run_market_service.py --role trading --db data\tmf.db --code TMFR1 --port 8765
-X utf8 -u src\health_watchdog.py --db data\tmf.db --code TMFR1 --mode production --notify
```

以有權讀取專案設定及券商憑證的部署帳號執行，設定同一工作「不啟動新的執行個體」。monitor／watchdog 可設定失敗後重啟；trading 不設定自動重啟，先核對委託與部位。正式模式仍須確認 `.env` 的 `SHIOAJI_SIMULATION=0`。工作排程器的執行時間限制、電池供電及睡眠條件也應符合常駐需求。

Windows supervisor 建立自己的子程序群組，先嘗試 `CTRL_BREAK`，逾時或無主控台時才透過自己持有的子程序 handle 停止它，不呼叫 Unix 的 `killpg`／`SIGKILL`。互動執行可用 Ctrl+C 正常關閉；若強制終止排程工作，另行核對健康檔中的子程序 PID，不能假定它已一併結束。共用程序鎖仍會阻擋另一份服務重複登入。

健康 JSON 與事件檔固定 UTF-8 編碼，日曆／設定也接受 Windows Notepad 的 UTF-8 BOM。Windows 短暫檔案分享衝突會在有限次數內重試原子替換，持續失敗仍回報錯誤，保留舊快照。

watchdog 每五秒唯讀檢查，預設只記錄事件；`--notify` 才會使用既有 Telegram 設定。故障去重、預設每 15 分鐘提醒仍未解除的故障，恢復通知一次；傳送失敗預設 60 秒後重試。多個異常合併成一次有限長度訊息，避免通知失敗拖住整個巡檢。事件、派送結果與待重試狀態存放於 `.health/<db>/incidents-<code>.json` 及 `.events.jsonl`。通知失敗也寫 stdout，供 OS／外部日誌告警。

watchdog 自己的 `.health/<db>/watchdog-<code>.json` 心跳應由**另一主機或外部監測**檢查；同一台電腦無法在整機停電時替自己發訊息。外部通知備援、券商端保護及實際啟用 launchd 都需依正式環境設定，本修復沒有代為安裝或宣稱已驗證。

## 回退

- 若 monitor 恢復影響其他連線，先停用新 monitor 的自動啟動、回退該服務程式；由獨立監督持續告警，交易端資料閘門保持關閉。
- 若交易核對有差異，保持自動進場暫停，核對券商委託及保護後再決定切回舊版或人工處理。不要自動清掉 `unknown`、刪除送單 claim、把 `active` 改為 0 或以舊資料庫覆蓋目前交易狀態。
- 資料庫備份供事故比對；若備份後券商已有新委託／成交，直接還原會使本機狀態倒退，可能造成重複下單，必須先完成券商核對。此修復沒有新增不可逆 schema migration。
- 只有來源版本、健康判斷、通知及券商核對都通過才完成正式切換。離線測試通過不代表跨程序登入／登出的券商行為已驗證。

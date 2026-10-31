# 獨立 tick / 1 分 K 抓取服務（第一階段）

這兩個程式不需要啟動 `monitor.py`，不匯入或建立交易服務，也不送單。各自建立一個 Shioaji session；第一階段尚未替換原有 monitor／trade 的行情來源。

## 啟動

在專案根目錄執行，macOS 和 Windows 使用同樣參數。以下是操作說明，開發驗證沒有執行這些真實連線命令。

```text
conda run --no-capture-output -n tss python -X utf8 -u src/tick_fetcher.py --mode simulation --codes TMFR1 TMFK6
conda run --no-capture-output -n tss python -X utf8 -u src/kbar_fetcher.py --mode simulation --codes TMFR1
```

兩者預設共用獨立的 `data/market_fetchers.db`，可用 `--db` 指定另一個測試 DB。`--mode` 必填；`simulation` 仍是**券商連線模式**，需要 `.env` 憑證，不是離線 Mock。正式模式須明確指定 `--mode production`。

- `--config` 指定設定檔，預設 `config/market_services.json`。
- `--once` 做一輪生命週期後登出；tick 是非同步推送，一輪可能還沒有報價，不適合當成即時行情可用性的驗收。
- K 線的 `--no-backfill` 只訂閱／彙整，不查歷史 API，方便獨立驗證 callback 與分鐘邊界。
- Tick 支援多個 `--codes`；K 線一次只接受一個。連續合約報價的實際 underlying code 會映射回所設定代碼。
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

這些表不會覆寫策略用 `minute_bars`。`complete=1` 是資料範圍屬性，**不代表交易已就緒**。正式遷移前仍需驗證 broker 與 stream 一致性、來源新鮮度與交易閘門。

健康檔位於 DB 旁 `.health/<DB檔名>/tick-fetcher.json` 和 `kbar-fetcher.json`；分別使用獨占鎖，一個角色／DB 只允許一個程序，兩種角色可以同時運行。健康檔含模式、PID、世代、錯誤種類、重試狀態、行情時間及 `staged_only: true`。

日誌位於 DB 旁 `logs/tick_fetcher.log`、`kbar_fetcher.log`，UTC 時間標記為 `Z`，保留 14 天輪替；排程及行情時間仍為台北時間。日誌不輸出憑證或任意例外字串。

Tick 回呼只入記憶體佇列，由主執行緒寫入 DB；無新 tick 時不更新行情時間。沒有成交不等於斷線：開盤時報價過舊會顯示 `stale`、`stream_fresh: false`，不會僅因單一或全部合約安靜就重新登入。`stream_recovery_seconds` 保留為長時間無新鮮 tick 的診斷門檻（預設 60 秒），對應健康檔的 `quiet_codes`，不再觸發重連。若 SDK 未回報斷線且沒有 API 錯誤，靜默故障會維持過舊狀態，需要依健康檔排查。

佇列溢位、session-down 或可辨識的連線／認證錯誤會觸發有退避的連線重建，DB 鎖定不重登。收到「操作異常，請1分鐘後再重新登入」等明確限流錯誤時，記錄為 `rate_limit`，停止該服務的 API 工作，至少等待 60 秒才重試；再次被拒絕就重新計時，已有更長退避則保留。冷卻期間不登入、不訂閱、不查歷史資料。此冷卻由各程序管理，不會自動暫停其他程序或電腦。

K 線在收盤後預設等待 2 秒才發布前一分鐘；晚到 tick 會使已發布的候選分鐘失效，等待歷史修復。啟動／中斷分鐘不標完整，不為無成交分鐘補造 OHLC；保留現有 13:45 集合競價排除慣例。歷史查詢限於啟動／重連、成熟缺口修復及收盤核對，修復間隔預設 300 秒，不逐分鐘查歷史 API。SDK 超時或阻塞可能延後排程登出，健康檔會保留操作及開始時間供監督判斷。

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

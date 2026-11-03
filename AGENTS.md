use conda env tss

專案需同時支援 macOS 與 Windows。
正式區部署於 Windows laptop。

## Shioaji session 限制與用量

- 同一個 `person_id` 最多同時使用 **5 個連線**；每次 `api.login()` 建立一個連線。不同 API Key、程序或電腦的登入仍須按同一身分合計，不能將上限視為每個服務各有 5 個。
- 目標架構配置為 **3 / 5 個 session**：`tick_fetcher.py` 1 個、`kbar_fetcher.py` 1 個、獨立 `trade.py` 1 個；完成拆分後的 `monitor.py`、`dashboard.py` 不登入券商。
- 目前行情分工、monitor 資料統整、獨立 trade 與 Dashboard 交易代理已實作（離線驗證）；券商及 Windows 原生驗收尚未完成。因此 **3 / 5 是架構配置，不是已查核的即時使用量**。新版 Dashboard 正式／模擬模式不再登入；若另有舊版 Dashboard／monitor、既有程序或其他電腦登入，也須一併計入。
- 實際目前用量以已登入 API 的 `api.usage().connections` 查詢；未查詢時不得宣稱實際已使用幾個。檢查用量不要額外登入，避免查詢本身占用新 session。
- 啟動、重連及切換版本前須計入舊程序尚未釋放的連線；使用完畢呼叫 `api.logout()`，避免超過上限。
- 官方來源：[Shioaji 使用限制](https://sinotrade.github.io/zh/tutor/limit/)（2026-10-31 核對）。

## Script 分工與資料流（2026-11-01 決策）

本節取代 `plan/2026-10-31-independent-market-services.md` 中舊的「Kbar 服務自行訂閱 tick」與「trade 直接消費 tick 做進場」分工；不改動該計畫其他尚未完成事項。

| Script | 職責 | 券商 session |
|---|---|---|
| `src/kbar_fetcher.py` | 歷史 1 分 K 啟動回補、缺口修復、收盤核對；不訂閱即時 tick | 自有 1 個 |
| `src/tick_fetcher.py` | 即時 tick 訂閱、每秒最新價、以完整 tick 彙整已收盤 1 分 K 候選；不查歷史 API | 自有 1 個 |
| `src/monitor.py` | 統一讀取兩 fetcher 的資料、同步共用 DB、更新指標與健康狀態、彙整稽核日誌；不登入、不抓券商資料 | 無 |
| `src/trade.py` | 由本機 API 接收 Dashboard 持久化交易指令，以共用 DB 的指標、curve、已收盤 1 分 K 判斷交易，執行及追蹤委託 | 自有 1 個 |
| `src/dashboard.py` | 圖表、曲線設定、交易需求輸入與狀態顯示；交易請求交給 trade 執行 | 無 |

資料流：`kbar_fetcher + tick_fetcher → acquisition DB → monitor → unified DB → dashboard / trade`。Dashboard 委託控制流：`dashboard → trade → 券商`。monitor 不承接下單責任，trade 不自行另抓一套策略行情。

- 目前交易規則以 **1 分 K 收定後** 判斷執行；不能擅自改成 tick 碰線即進場。獨立 trade 共用既有交易與曲線計算邏輯，逐一處理未判斷的相鄰已收盤分鐘。
- Dashboard 保存曲線於共用 DB `trendlines`（控制點、商品、週期、啟用狀態）；進場啟用狀態與委託執行狀態由 trade 維護。畫線／保存本身不等於啟用進場。
- Dashboard 必須將 order／啟用／取消需求送給獨立 trade API，不在 Dashboard 程序內下單。指令須有持久化唯一 ID；逾時查狀態，不盲目重送。正式／模擬模式使用 `TradeClient`，不得回退為 Dashboard 內嵌券商連線；僅設定檔選擇的 Mock／注入測試保留原來離線交易引擎。
- fetcher 預設寫 `data/market_fetchers.db` 的 `market_*` 資料表；monitor 透過 `--fetcher-db` 唯讀取得來源，透過 `--db` 寫統一資料庫（預設 `data/tmf.db`）。Dashboard／未來 trade 使用同一個統一 DB；不要直接跳過 monitor 讀 acquisition DB 作策略判斷。
- monitor 僅匯入已收盤完整 K 棒，歷史 broker 資料優先於 tick 候選；晚到 tick／中斷造成失效的候選須撤回。來源與 simulation／production 綁定於統一 DB，同一 DB 不可切換來源或模式混用；最新價保留原始行情與接收時間。
- 依 2026-11-02 決策，broker 歷史 K 已接上 tick 彙整區間，且日曆、行情時效、目前 tick instance／generation 與健康檢查通過時，monitor 即可就緒；不要求每根 tick K 先經 broker 核對。明確標記為 incomplete 的 tick 分鐘（含啟動／中斷分鐘、晚到 tick 失效）僅列為 `pending_repair`，不單獨停用下單或讓 UI 宣稱行情過舊；不匯入策略 K、不補造價格，後續由 broker 回補。未解釋缺棒、串流中斷／過舊或世代不符仍關閉閘門；曲線突破仍須使用相鄰完整收定分鐘，不能跨缺棒判斷。broker 核對優先覆蓋。`unverified_stream_minutes` 保留待核對數量，`staged_only` 僅表示仍不可採用的候選。Windows 原生與券商一致性驗證仍待完成。
- monitor 不使用券商五檔訂閱或 snapshot fallback；五檔搬至 fetcher 的串接尚待開發，不能為了五檔在 monitor 新增登入。

## Monitor 必備稽核紀錄

- 全部事件時間為 **Asia/Taipei**，須記錄來源角色、模式、instance、generation，區分發起時間與成功／失敗完成時間。
- session：`login_started`／`login_succeeded`／`login_failed`，`logout_started`／`logout_succeeded`／`logout_failed`；不能把開始登出當作已登出。程序被強制終止時，不得補造成功登出時間。
- 回補：`backfill_started`／`backfill_completed`／`backfill_failed`，保留查詢 `from_timestamp`、`to_timestamp`（日期查詢上界為排他）、已收盤篩選截止，以及實際寫入資料的起訖與筆數；空資料不視為缺口已補齊。
- 即時資料失敗：記錄發生時間與可辨識原因，如 `session_down`、`queue_overflow`、`stale_or_missing_tick`、錯誤分類／例外型別；恢復時另記 `realtime_fetch_recovered`。沒有新成交只代表行情過舊，不單靠安靜重登。
- fetcher 在 acquisition DB 旁 `logs/tick_events.jsonl`、`logs/kbar_events.jsonl` 保存原始事件；monitor 可於稍後啟動並補讀，依事件 ID 去重、持久化讀取位置。彙整輸出為統一 DB 旁 `logs/monitor.log` 及 `monitor_fetch_events` 資料表（後者是可查詢的稽核紀錄）。原始事件檔目前不自動輪替；不可直接刪除尚未彙整的事件。
- 登出失敗保留 `logout_unconfirmed` 作稽核紀錄；依 2026-11-02 決策，不以登出確認狀態阻擋啟動或後續登入，重連仍遵循原排程與重試設定。
- 不記錄金鑰、憑證或可能含憑證的任意例外全文；使用安全的錯誤分類與型別。稽核寫入故障不得阻止既有 session 登出，健康檔須顯示 `audit_error`。

## 獨立 trade 開發約定（2026-11-01）

- `trade.py` 為 loopback 常駐 API（預設 8766），所有券商 I/O 由單一背景執行緒依序執行；API 查詢僅讀背景快照。不得多 worker／reload 啟動多個登入。
- `trade_service.py` 是既有 `TradingService` 的獨立程序適配層，不註冊行情 callback、不訂閱 tick、不抓歷史資料；`trading.py` 保留離線回放與既有委託／部位邏輯。
- 自動進出場、固定停損／停利、保本均以已驗證收定 1 分 K 判斷。曲線使用 TMFR1，固定價格風控使用實際月份合約；兩者必須各有 monitor 健康狀態與共用 DB 資料，不將連續合約價格冒充月份合約價格。手動委託通過驗證後執行。
- 2026-11-03：Dashboard 停損／停利調整區可逐單保存 `breakeven_trigger_points`（浮盈觸發點數）及 `breakeven_offset_points`（保留獲利點數）；未個別設定時沿用共用設定，預設 100／5。參數經 trade 持久化指令與風控版本檢查更新，不影響其他單。保本已觸發後，調高觸發點數不解除保護，調低保留點數不自動放寬既有 stop。
- tick／kbar fetcher 的 `--codes` 須同時包含訊號商品與實際月份合約；kbar 支援多個合約共用同一 session、獨立回補游標。每個代碼用一個無券商連線的 monitor，寫入同一統一 DB。先啟動 fetcher，再啟動 monitor／trade。
- trade 的「訂閱」指令只更新本機關注清單，尚未動態調整 fetcher 訂閱；換月須更新 fetcher／monitor 代碼。不可為每個月份合約額外建立一個券商 session。
- `trade_commands` 保存唯一 ID、正規化請求、執行狀態與結果；同 ID 同內容讀原結果，同 ID 不同內容回 409。HTTP handler 先保存 accepted，背景執行前改 processing；重啟遺留 processing 改 unknown，不自動重送。委託／指令結果不明時須人工核對，不能刪除 claim 後重送。
- Dashboard 使用 `Idempotency-Key`，瀏覽器保留未確認的指令 ID；逾時查原結果，同一操作重試沿用 ID。讀取 API 不觸發登入、同步委託或保證金查詢；關閉 Dashboard 不停止 trade。
- trade 跟隨行情排程登入／登出；收盤後停止新增委託，盡可能保存狀態但不自動平倉／撤單。排程外 API 仍常駐。非預期重連沿用 `trading_reconnect_enabled` 閘門；登出結果僅作稽核，不阻擋後續登入。
- `logs/trade_events.jsonl` 由 monitor 一併彙整，與 tick／kbar 一樣保留原始 session 時間。完整操作與限制見 `doc/independent_trade.md`；舊計畫的最後 10 秒 tick 參考價／額外價差補單尚未具備，不能宣稱已實作。
- trade 對本服務管理的進場／平倉委託，在券商確認成交口數增加並成功持久化後發送 Telegram；部分成交分次通知，依委託 ID、進出場階段與累計口數去重，重啟不重發。沿用 `telegram_enabled`／`--no-telegram`，Mock 停用。出場均價必須使用券商回報的 `avg_exit_price`，不能冒充為觸發價；通知背景傳送失敗不阻斷交易，暫停期間成交不補發。

## 模式、整體啟動與 Dashboard（2026-11-01）

- 模式唯一來源是 `config/runtime.json`，預設 `mock`；各服務與監督工具不再接受 `--mode`／`--mock`，可用 `--runtime-config` 指定另一份設定。讀取一次，變更後重啟。相對路徑以設定檔所在資料夾解析。
- Mock 沿用 Dashboard 內嵌離線回放／假券商，建立新的暫存 DB、初始暫停；不啟動 fetcher／monitor／獨立 trade，不讀券商憑證。非 Mock 必須明確配置訊號與實際月份合約。
- macOS 用 `start_all.sh` 或雙擊 `start_all.command`，自動找到 conda `tss`；共用的 `service_launcher.py` 也可於 Windows 執行。啟動器快照本次 runtime／Dashboard 設定，避免啟動途中混用模式。
- 非 Mock 啟動順序為 tick、kbar、每商品 monitor、trade、Dashboard。只管理本次建立的程序組；遇失敗或 Ctrl+C 先優雅停止，45 秒後才終止卡住者並記錄登出未確認，不自動重啟或廣泛終止其他 Python 程序。
- 啟動任何新服務前，`service_control.py` 檢查設定中兩個 DB 旁的本專案程序鎖。發現仍在執行的舊券商服務（含排程外待機者）時，以 instance 專屬停止要求通知原啟動器及服務，由原持有 API 的程序嘗試 logout；最多等 45 秒讓舊程序釋放鎖，不強制終止舊程序。只因程序仍在執行／無協作協定的舊版仍在執行而阻擋重複啟動，不再要求登出成功確認；已停止程序的未確認紀錄不阻擋啟動。單純 Mock 重複啟動仍拒絕，不停止原 Mock。
- `.health/<DB名>/*.owner.json`、`*.owner.lock` 與 `*.owner.stop.json` 保存持有者、OS 鎖與針對該 instance 的停止要求；登入意圖須在 SDK login 前持久化。取得程序鎖後可建立本次 owner 紀錄，歷史 session 事件仍保留於原始日誌，不偽造登出成功。不能憑 PID 猜測並終止其他程序。登出前若可行，僅透過既有 API 記錄 `connections_before_logout`；本機紀錄不是券商全域 session 清單。其他機器、不同 DB 路徑或未受管程序須由原持有者登出；失去原 API 物件的連線不能由新登入代為登出。
- `config/dashboard.json` 控制 UI 預設週期、歷史天數、可見根數、刷新／快取間隔與 hover 距離；預設 1h、30 天、120 根、60 秒、5 秒、8 像素。`/api/dashboard-config` 只提供顯示設定，`/api/ready` 提供本程序 PID／模式做啟動驗證。
- 1h 快速路徑須核對分鐘資料版本與完整的 monitor 衍生版本；缺少、過舊或只涵蓋部分歷史時退回分鐘重建。小週期資料依需求載入；保留完整 ATR、delta volume 與曲線來源幾何，不因加速更改交易價格或條件。
- 曲線 hover 使用畫面取樣線段距離；顯示純文字名稱／週期／ID，同距離以較小 ID 決定，繪製與拖曳期間不顯示。詳細操作見 `doc/runtime_and_dashboard.md`。

# 獨立行情服務、排程登入／登出、交易日快取與 Windows 部署

日期：2026-10-31。全部時間為 Asia/Taipei。開發及正式環境使用 conda `tss`；程式同時支援 macOS 與 Windows，正式區位於 Windows laptop。

## 已確認的目標與決策

拆為五個可獨立啟動、停止、測試、除錯的程序，不共享 Shioaji API 物件：

| Script | 責任 | Session |
|---|---|---|
| `tick_fetcher.py` | 訂閱即時 tick／五檔，提供交易及 UI 報價 | A |
| `kbar_fetcher.py` | 獨立訂閱並產生已收盤 1 分 K、歷史回補及缺口修復 | B |
| `monitor.py` | 讀取 DB 更新指標、完整性驗證、健康閘門 | 無 |
| `trade.py` | 下單、進出場監控、委託及部位核對 | C |
| `dashboard.py` | 圖表、狀態及操作介面 | 無 |

使用者確認：行情兩個 session，下單另用第三個；即時行情採訂閱推送，每秒發布最新價、每分鐘發布 K 棒；13:46、05:01 行情及 trade 三個 session 都登出。離線回放也採獨立程序。

## 行情更新與責任邊界

- Tick 持續接收，最新合約報價每秒合併寫 DB；不新增逐筆 tick 歷史。保留行情時間與接收時間，沒有新成交不得更新行情時間製造新鮮度。
- 後續 trade 從本機串流接收完整事件，不能只取每秒最後報價，避免漏掉秒內停損穿越。串流需有世代、序號、來源及逐合約新鮮度；斷流後須重新驗證。
- K 線服務擁有另一個 session，完全不依賴 tick 服務。官方當前 KBar 推送只支援證券，不支援期貨；TMF 由完整 tick 彙整，歷史 `kbars()` 只用於啟動回補、缺口修復、收盤核對，不每分鐘輪詢歷史 API。
- 只發布已收盤分鐘，保留現有 `minute_start` 與收盤集合競價慣例。分鐘中途登入、斷線、佇列溢位與晚到 tick 必須標示不完整，不補造沒有成交的 K 棒。
- 自行彙整的 OHLCV／amount 與券商歷史 K 棒須先比對，差異持久化；完成實際券商一致性驗證前，不將新來源當成可交易行情。
- 每個程序有獨立鎖、健康檔、日誌及重試狀態。Callback 只放入有界記憶體佇列，DB 與重連在所屬服務主執行緒進行；過時 callback 不得污染新 session。
- DB 錯誤不觸發券商重新登入。登入／session／傳輸故障有退避；沒有新行情但連線仍存在時也可觸發受控恢復。設定或日曆無效須明確顯示，不能假裝健康。

參考（2026-10-31 查核）：

- https://sinotrade.github.io/tutor/limit/
- https://sinotrade.github.io/tutor/market_data/streaming/stocks/#kbar
- https://sinotrade.github.io/tutor/market_data/streaming/futures/

## 設定與排程

集中使用 `config/market_services.json`：

```json
{
  "timezone": "Asia/Taipei",
  "calendar_path": "market_calendar.json",
  "scheduled_login_times": ["08:44", "14:59"],
  "scheduled_logout_times": ["13:46", "05:01"],
  "tick_publish_seconds": 1,
  "kbar_publish_seconds": 60
}
```

- 本次新增明確的 `scheduled_login_times`，取代僅用開盤前秒數推導登入時間。登入／登出時間須 HH:MM、不得重複，並在一天的循環內交錯出現。
- 登入窗口還須與已驗證交易日曆的實際開盤區間重疊。一般為 08:44–13:46、14:59–次日 05:01；週六清晨延續週五夜盤，週末／假日沒有新開盤則不登入。
- 服務於休息期間仍常駐、發布 `scheduled_closed`；不能因登出後 tick 安靜而反覆重連。重啟／睡眠喚醒須從當下絕對時間重新判定窗口。
- 最後一分鐘要在登出前完成發布或標示待修復；夜盤包括 04:59–05:00。
- Trade 登出前停止新增委託，記錄可取得的狀態；不自動平倉、撤單或清除不明委託。重新登入後核對委託與部位，取得新鮮行情後才開放交易。
- 配置於啟動時讀取，變更後重啟服務；交易日曆每輪重新讀取。底層 SDK 若阻塞，實際登出可能晚於排程，需記錄操作開始及逾時，不能宣稱指定秒必然完成。

## 交易日驗證與最近 30 日 JSON（後續階段）

- 本機 Shioaji 1.7.7 公開介面沒有專用交易日曆方法，不能假定能直接取得歷史紀錄的交易日。
- 獨立唯讀工具取得指定 DB／商品的最後一筆分鐘紀錄，以交易所日曆建立候選交易日，再用少量指定時段歷史 ticks 查詢驗證。
- 必測一般夜盤、週五至週六、連假前夜盤；不使用單純日期加一天，也不將現有 `session_date` 直接當成已確認券商交易日。API 空資料或錯誤只能標未知。
- `data/cache/trading_days-TMFR1.json` 以最後 DB 紀錄所屬交易日為上限保存最近 30 個已確認交易日；保存商品、時區、來源、對應時段、schema、更新時間及驗證狀態。
- 不足 30 日標不完整；由 K 線服務單獨維護、原子替換，共用快取命中時不查 API，只在進入新交易日後增量更新。失敗保留舊檔並標過期。
- 歷史快取不能取代未來開休市、特殊時段的 `market_calendar.json`。

## 交易拆分、漏訊號及價差（後續階段）

- Dashboard 的既有交易路由改為呼叫 localhost trade API，不再持有 TradingService／券商連線；交易查詢只讀背景快照。具狀態變更的請求使用持久化唯一指令 ID，逾時查結果，不盲目重送。
- 正常 monitor `checking` 不使已驗證資料突然失效，也不反覆重設進場起點。保存最後驗證版本與分鐘水位，逐一掃描未處理的相鄰已收盤分鐘。
- 曲線補單：收盤確認起 60 秒內、與訊號實際合約參考價差不超過 20 點，且新鮮 tick 仍在換算曲線的突破側。價差＝訊號實際合約參考價－TMFR1 訊號分鐘收盤價。缺乏收盤前 10 秒內可靠參考價不補單。
- 超時、反向穿越、曲線修改／停用或偏差過大時記錄原因，略過候選、繼續等新訊號；不追補今天或其他已過期歷史事件。
- Dashboard 顯示月份合約價、TMFR1 價、兩邊時間與即時價差（實際合約－TMFR1）；報價須各自新鮮且相差不超過 1 秒，否則顯示不同步／過期。另顯示固定訊號價差、換算曲線門檻與補單倒數。
- 進出場維持送單前持久化 claim，`submitting`／`unknown` 在重連、重啟後不能自動重送。停止每輪兩次與 UI 額外觸發的密集委託同步。
- 非預期斷線自動重新登入須完成券商模擬跨 session 驗證後才啟用；不能把已知 session 故障與行情日曆問題混為已證實根因。

## Windows 與離線回放（後續階段）

- 提供 `start_tick.bat`、`start_kbar.bat`、`start_monitor.bat`、`start_trade.bat`、`start_dashboard.bat`、`start_all.bat`、`check_services.bat`。
- `%~dp0` 定位、`cd /d`、所有路徑加引號；使用 `conda run --no-capture-output -n tss python -X utf8 -u ...`，允許指定 conda 完整路徑，缺環境時明確報錯。
- 各服務獨立視窗／排程，重複啟動由鎖阻擋；不能廣泛 `taskkill python.exe`。不將金鑰寫進 bat，也不強制改變 simulation／production。
- Mock 也使用獨立程序，由來源程序持有回放時鐘及有序事件，交易程序確認後才推進；重設切換世代，重複序號不重複成交。
- 完成 macOS 離線測試後，另做 Windows 原生檢查、券商模擬多 session 登出／登入互相影響驗證與 K 棒一致性比對，再切換正式服務。

## 本次實作範圍（第一階段）

本次僅先交付兩個獨立抓取 script、共用排程與復原元件、最新價／候選 K 棒／歷史核對資料表、CLI、日誌、健康檔及離線測試。不修改現有 monitor／Dashboard／交易服務，不執行正式登入、送單或部署。

- 預設 DB 是 `data/market_fetchers.db`；即使指定既有 DB，也只寫 `market_*` 抓取專用資料表，不覆蓋 `minute_bars` 或活動委託。
- Tick 第一階段僅做訂閱與每秒最新價存放；五檔、trade 完整事件串流、動態訂閱及 UI 整合尚未接入。
- K 棒分 `stream`／`broker` 儲存，保留完整性及比對結果；`complete` 只描述收到資料的範圍，不代表已獲准交易。健康檔明確標示 `staged_only`。
- 最近 30 交易日 API 探測／快取、bat、trade／monitor 遷移及完整 mock 拆分留待後續階段，不能將第一階段視為整個方案完成。

實作及測試操作見 `doc/independent_market_fetchers.md`。

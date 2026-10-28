# TMF 1 小時 S/R 箱體監控

用 Shioaji 每分鐘更新微型臺指期貨（TMF）價格，在 1 小時圖上畫出 Pine「Support and
Resistance (High Volume Boxes)」箱體，當 **1 分 K 價格進入箱體**時開始記錄委託簿並發
Telegram 通知，並偵測箱體內的小時 K 棒反轉型態。

包含行情監控、通知與手動期貨下單；可使用下方離線 Mock API 測試，不需券商帳密。

支援 macOS 與 Windows，統一使用 conda 環境 `tss`；正式區為 Windows laptop。
健康狀態的程序鎖依平台使用標準函式庫，不需安裝 `fcntl`。常駐服務的 Windows 工作排程器與
macOS launchd 操作方式見[行情故障恢復操作手冊](doc/market_connection_recovery.md)。

首次使用請先依[環境安裝說明](#環境)建立 `tss` 並安裝依賴；同名 conda 環境不代表套件已齊全。

## 離線 Mock API：V 型行情測試

```bash
conda run --no-capture-output -n tss python src/dashboard.py --mock --port 8766
```

開啟 [Mock Dashboard](http://127.0.0.1:8766)。不需啟動 `monitor.py`，不讀券商帳密；
每次啟動建立新的暫存資料庫，不使用 `data/tmf.db`。畫面顯示「離線 Mock API」，
與 Shioaji 的券商模擬環境是不同模式。

情境為 **60 小時下跌 → 8 小時 V 型反彈 → 2 小時回檔**（按有效交易分鐘計）。
目前 `config/mock.json` 設為 `replay.start_bar: 0`，從 **0/4200 根**空圖開始，
不預載歷史資料。播放速度依 `replay.interval_seconds`（目前 0.1 秒推進
1 分鐘），也可按 +1 / +10 / +60 分鐘。未推進的資料不參與指標與策略。

Mock 的可調參數集中在 [`config/mock.json`](config/mock.json)，**修改後需重新啟動 Mock Dashboard**。
啟動時載入並驗證；缺少的欄位沿用預設值，未知欄位、錯誤型別或不合法數值會顯示錯誤。
JSON 內每個參數旁的 `_comment_參數名稱` 是中文說明；`_comment` 與 `_comment_` 開頭的欄位會在讀取時忽略，可自由修改說明文字。

| 設定區塊 | 參數 | 用途（預設值） |
| --- | --- | --- |
| `market` | `start_time` | 資料起始時間（`2026-10-05 08:45`，使用本地交易時間，不帶時區）；非交易分鐘自動跳過 |
| `market` | `decline_minutes` / `rebound_minutes` / `pullback_minutes` | 下跌／反彈／回檔的有效交易分鐘數（3600／480／120） |
| `market` | `start_price` | 起始價格（27000） |
| `market` | `decline_points_per_minute` / `rebound_points_per_minute` / `pullback_points_per_minute` | 各階段每分鐘下跌／上漲／回檔點數（0.8／2／2，均填正數）；轉折價格隨前一階段自動銜接 |
| `market` | `wave_amplitude` / `wave_scale_minutes` | 正弦波動振幅（0.6，可設 0）與時間尺度（7；完整週期為 `2π × wave_scale_minutes` 分鐘） |
| `market` | `wick_points` | K 棒實體上下各加的影線點數（2） |
| `market` | `volume_base` / `volume_cycle_minutes` | 每分鐘成交量為 `volume_base + 分鐘索引 % volume_cycle_minutes`（10／17） |
| `replay` | `start_bar` | 直接指定初始已載入的 K 棒數，優先於相對起點。`0` 從空圖開始、`4020` 從 4020/4200 開始；範圍為 0 到情境總根數。設為 `null` 或省略才使用 `start_at` |
| `replay` | `start_at` | 僅在 `start_bar` 為 `null` 時使用：`before_bottom` 為谷底前，適合突破買入；`before_pullback` 為反彈結束前，適合回檔曲線停利。省略時沿用 `before_bottom` |
| `replay` | `minutes_before_bottom` | `start_at=before_bottom` 時，初始停在谷底前幾個有效交易分鐘（60，須小於下跌分鐘數） |
| `replay` | `minutes_before_pullback` | `start_at=before_pullback` 時，初始停在反彈結束前幾個有效交易分鐘（60，不得大於反彈分鐘數）；之前的 K 棒作為歷史資料 |
| `replay` | `interval_seconds` | 每播放一根 1 分 K 等待的真實秒數（1）；設為 60 即約每真實分鐘更新一根 |
| `replay` | `browser_poll_seconds` | 網頁查詢回放進度的間隔秒數（1.5） |
| `broker` | `equity` | 模擬帳戶權益（1000000） |
| `broker` | `initial_margin_per_contract` / `maintenance_margin_per_contract` | 每口原始／維持保證金（22000／18000） |

例如想放慢為每 5 秒更新一根 1 分 K，將 `replay.interval_seconds` 改成 `5`。
初始仍為暫停，按「播放」才推進；「重設情境」沿用本次啟動載入的設定。

### 從第零根開始

將 `replay.start_bar` 設為 `0` 並重啟。初始顯示 **0/4200**，圖表為空、現價顯示「—」；
按「+1 分鐘」後顯示 **1/4200** 與第一根 K 棒，按「播放」則持續逐根加入。
尚未累積足夠的 1 小時 ATR 前，下單按鈕會停用；不會使用未回放的資料來補指標。
按「重設情境」會再次回到 0 根。

想從任意位置開始，只需改同一個 `start_bar`，例如 `4020` 用於下方回檔停利測試。

### 回檔曲線停利測試

設定 `replay.start_bar: 4020`；或將 `start_bar` 設為 `null`，再設定
`start_at: "before_pullback"`、`minutes_before_pullback: 60`。重啟後行情暫停在
約 **24958**，還有 60 根 1 分 K 反彈至約 **25080**，之後按每分鐘約 2 點回檔。
以下價格以目前的 `market` 設定為準，調整行情參數後需重新選擇曲線價格。

1. 保持暫停，用市價 **Buy 1 口**建立多單。
2. 切至 **1h**，按「曲線」；在近期歷史 K 棒範圍依序點起點、終點、中間控制點，
   三點價格都放在約 **25040**，先用水平曲線確認觸發流程。
3. 點選右側已成交部位，在圖表上方的出場編輯器選該曲線，按「儲存」。
   此時固定 +2 ATR 停利停用，原停損保留。
4. 按 **+60 分鐘**：反彈接近高點，部位應仍未平倉；再按 **+10 分鐘**三次，
   回檔的 1 分 K 收盤向下穿越約 25040 時，觸發曲線出場並模擬賣出平倉。
   預期出場價高於進場價、持倉歸零，觸發原因為 `curve`。

測試完成後按「重設情境」即可回到同一起點（會清除本次 Mock 委託、曲線及進場設定）。
這個流程使用持倉的出場設定「儲存」；下單卡片「曲線突破」分頁是另一個進場測試。

1. **手動下單與 ±2 ATR**：選月份、方向與市價，確認下單。Mock 立即按當前價格成交；
   點選右側部位，在圖上檢查成交均價、停損與停利。多單為均價 −2 ATR / +2 ATR，空單相反。
2. **手動曲線移動停利**：切到 1h，按「曲線」，依提示點起點、終點與中間控制點。
   點選右側已成交部位，在圖表上方選該曲線並按「儲存」。固定停利停用，原停損保留；
   多單於已收盤 1 分 K 向下穿越曲線時平倉，空單反向。圖上的 target 跟隨最新曲線價、不可拖動；stop 可獨立調整，按「儲存」才生效。
3. **曲線突破進場**：先保存曲線，在下單卡片的「曲線突破」分頁選曲線、方向與口數，按「啟用一次突破做多／做空」。
   多單於啟用後的相鄰已收盤 1 分 K 從曲線下方（含相等）向上穿越時買進；空單於從上方（含相等）向下穿越時賣出。
   最新收盤僅碰到曲線不觸發。觸發後只送一筆市價 MKT / IOC，成交後使用固定 ±2 ATR；進場曲線不會自動成為停利曲線，可再按流程 2 設定。
   測試此流程時，先將 `replay.start_bar` 改成 `null`、`start_at` 改成 `before_bottom`，
   並設 `minutes_before_bottom: 60` 後重啟；
   可畫接近 **24150** 的水平曲線，再推進約 90 分鐘，觀察下跌後反彈的突破。

三種流程在 Mock、券商 Simulation 與正式模式共用交易服務、ATR、設定開關、持倉記錄與進出場邏輯。
曲線突破自動進場統一使用 `/api/trading/curve-entry`。啟用時不補做歷史訊號，取消或刪除曲線後不進場，
錯誤時停止且不自動重送。「重設情境」確認後會清除本次 Mock 委託、曲線與進場設定。

Mock 市價與範圍市價按最新回放收盤成交；限價只有報價符合時成交，否則 ROD 等待、
IOC/FOK 取消。模擬採無限流動性、全額成交，不模擬部分成交、手續費、滑價或券商延遲。
停損停利是程式監控後送出平倉委託，不是預掛在券商端的兩張委託。

## 單筆 PnL 與出場設定

點選「本介面委託 / 部位」的單據，圖表右側會顯示進場、target、stop 損益區塊，
平常只顯示透明損益窗格；滑鼠移入或拖曳時，才在窗格最右側顯示價格、每口正負點數／百分比、剩餘口數、浮盈虧點數與報酬風險比（不含費用與台幣換算）。
持倉可拖曳 target／stop 邊界或輸入價格；限價未成交單只提供預覽。
進場價固定，以實際成交均價計算；預設 ±2 ATR 價格依合約最小跳動單位對齊。

所有調整先預覽，按「儲存」才一起套用；「還原」取回最新生效設定。
切換單據保留各自草稿，重新載入頁面不保留未儲存內容。
曲線模式的 target 唯讀，按最新 1 分 K 位置計算曲線價，仍依 1m 收盤穿越出場；
stop 可獨立修改。取消曲線時帶出成交均價 ±2 倍進場 ATR，可再改 target／stop 後儲存。
曲線不可用時不代填固定 target，原停損持續監控。

目前持倉與新單預設啟用自動保本：多單即時價減均價、空單均價減即時價，
**嚴格超過設定門檻（預設 100 點）**後，stop 移至均價往獲利方向的保留點數（預設 **5 點**，依 tick 向保護側對齊），
已有較佳 stop 則保留，不會跟隨行情持續追價。口數不影響觸發點數。
保本啟用且已觸發時，stop 只能收緊；需先關閉並儲存，才能再次放寬 stop。
取消曲線的 ATR 預設 stop 也受此限制。重新開啟時若目前浮盈已超過設定門檻，立即重新套用。
程式重啟保留保本狀態，不回補離線期間的行情。

`PUT /api/trading/orders/{order_id}/risk-settings` 原子儲存 `curve_id`、`stop_price`、
`target_price`、`breakeven_enabled`、`risk_revision`（曲線模式固定 target 為 null）。
設定、保本或成交變動會更新風控版本；過期草稿回應 409 `risk_conflict`，請還原重新核對。
行情已達新設定的即時出場條件時回應 409 `confirmation_required`，確認後帶 `confirmed: true` 重送。
自動出場開關仍決定是否送出平倉單。鎖利 stop 的報酬風險比顯示「—／已保本」。

## 三種模式的共用交易操作

Mock 只替換行情來源、時鐘與委託撮合，提供播放／暫停／快轉及重設；所有模式的
突破進場、一次觸發、ATR、固定停損停利、曲線出場與操作介面都使用相同交易服務。

非 Mock 使用 `.env` 的 `SHIOAJI_SIMULATION=1` 連券商模擬環境，`0` 為正式環境。
需由 `monitor.py` 持續寫入相同資料庫的已收盤 1 分 K，Dashboard 負責交易監控。
例如在兩個終端機分別執行（先設定好 `.env` 與必要帳戶權限）：

```bash
conda run --no-capture-output -n tss python src/monitor.py --code TMFR1 --no-telegram
conda run --no-capture-output -n tss python src/dashboard.py --code TMFR1 --port 8765
```

三種模式的操作相同：在下單卡片選月份合約，在 1h 圖保存曲線，切至「曲線突破」分頁選曲線、方向與口數（1–100），
按「啟用一次突破做多／做空」，確認目前環境／合約／曲線／方向／口數。每次只能啟用一筆等待條件；向上突破做多、向下突破做空，
觸發後送出市價 MKT / IOC。以送單時保存的 1h ATR 與實際成交均價設定停損停利：多單停損 −2 ATR、停利 +2 ATR，空單相反。
可由「取消等待進場」停止等待；進場曲線不會自動成為出場曲線，需選取持倉，在圖表上方選曲線並按「儲存」。

買進／賣出實際進場成交後，Dashboard 的交易服務會發送 Telegram，包含交易環境、合約、方向、
累計成交口數、成交均價、進場 ATR、停損停利及委託編號；手動下單與曲線突破都適用。
分批成交時，每次累計成交口數增加會再通知一次；未成交、取消未成交委託與重複同步不發送。
通知在背景發送，傳送失敗不影響送單或停損停利監控，結果記錄於 `telegram_alerts`（`kind = entry_fill`），
同一筆委託的相同累計口數不會因重新啟動而重複發送。離線 Mock 模式停用實際通知。
沿用 `.env` 的 Telegram 帳密及 `config/alerts.json` 的 `telegram_enabled` 總開關；
關閉期間的成交不補發。`monitor.py --no-telegram` 僅關閉該行情監控程序的通知，
Dashboard 的進場通知仍由上述總開關控制。

「手動下單」與「曲線突破」只共用月份合約，方向與口數獨立且切換分頁會保留。修改表單或月份不會改變已啟用條件，
要更改須取消後重新啟用；卡片共用區持續顯示實際等待的合約、曲線、方向與口數。
`POST /api/trading/curve-entry` 接受 `action: "Buy" | "Sell"`（省略時為 `Buy`），狀態回傳包含 `action`；既有資料庫升級後的舊進場條件沿用 `Buy`。

`config/trading.json` 的交易設定在全部模式即時讀取（修改後不需重啟）：

| 設定 | 行為 |
| --- | --- |
| `auto_entry_enabled` | 預設 `true`，允許手動確認後等待突破；設為 `false` 時禁止新啟用，既有等待在下個監控週期取消，重新開啟後需再次手動啟用 |
| `auto_exit_enabled` | 預設 `true`，停損停利觸發後自動平倉；設為 `false` 時只記錄觸發，不送平倉單；Mock 同樣遵守 |
| `breakeven_trigger_points` | 預設 `100`，每口浮盈嚴格超過此點數時觸發保本 |
| `breakeven_offset_points` | 預設 `5`，多單 stop 設為成交均價加此點數、空單減此點數；設為 `0` 為均價保本 |

保本點數接受非負數及小數；缺少欄位或個別數值無效（負數、字串、布林、非有限數）時，該欄沿用預設值。
已生效的保本 stop 不會因調小設定而放寬。介面保本開關的說明會同步顯示目前門檻與保留點數。

等待與觸發狀態存入 SQLite，送單前以原子更新取得一次送單資格。重新啟動也不重送已觸發／失敗的進場；
若程序在送單期間中斷，保留「送單中」狀態供人工核對。不同交易環境的等待與持倉分開查詢。
交易環境變更須重啟服務並重新確認。Mock 預設使用獨立暫存資料庫，重設會清除本次狀態。

跨模式離線一致性測試（含正式模式的注入假券商，不連外送單）：

```bash
conda run --no-capture-output -n tss python -m pytest src/test_trading_parity.py -q
```

## Mock 流程的一鍵自動測試

在專案資料夾執行：

```bash
conda run --no-capture-output -n tss python tools/test_mock_flows.py
```

腳本會自動進場、建立並套用曲線、推進 1 分 K、檢查成交與持倉，最後逐項印出
`PASS`／`FAIL`、進出場價格、ATR、觸發原因與剩餘部位。

| 流程 | 自動驗證內容 |
| --- | --- |
| `fixed-atr` | 多單成交後，停利設定為進場價 +2 ATR；價格到達後以 `target` 原因平倉、獲利且部位歸零 |
| `curve-exit` | 多單在反彈尾段進場，套用約 25040 的曲線；固定停利停用、原停損保留；先漲後回檔跌破曲線，以 `curve` 原因獲利平倉 |
| `curve-entry` | 啟用曲線突破買入後不立即進場；後續反彈向上突破才買入一次，成交後建立 ±2 ATR，繼續推進不重複買入 |
| `curve-entry-stop` | 向上突破曲線後自動買入，固定停損為成交價 −2 倍進場 ATR；短暫反彈後回檔，觸及停損以 `stop` 原因賣出平倉，部位歸零且不重複下單 |

不需先啟動 Dashboard。測試透過 FastAPI 的記憶體內測試用戶端使用相同 Mock API 與交易服務，
每個流程使用獨立暫存資料庫與固定行情；不讀取 `config/mock.json`，也不操作目前開啟的回放。
回放直接逐分鐘推進，不必等待 `interval_seconds`。這些測試涵蓋 API 與交易流程；瀏覽器畫線手勢不在此腳本範圍內。

可單獨執行一個流程，或另存 JSON 報告：

```bash
conda run --no-capture-output -n tss python tools/test_mock_flows.py --case fixed-atr
conda run --no-capture-output -n tss python tools/test_mock_flows.py --case curve-exit
conda run --no-capture-output -n tss python tools/test_mock_flows.py --case curve-entry
conda run --no-capture-output -n tss python tools/test_mock_flows.py --case curve-entry-stop
conda run --no-capture-output -n tss python tools/test_mock_flows.py --report out/mock-flow-report.json
```

全部通過時結束碼為 `0`；失敗、跳過或未完成的測試會回傳 `1`，並顯示 pytest 診斷，方便接入自動檢查。

`curve-entry-stop` 使用獨立的「反彈 30 分鐘、再回檔 120 分鐘」測試情境，
讓進場後先回檔觸及 −2 ATR，而不先碰到 +2 ATR 停利。
停損依進場時 ATR 固定，後續 ATR 變化不會移動停損。Mock 在第一根觸及／跌破停損的分鐘收盤價成交，
因此實際損失可能略大於 2 ATR；報告會列出停損門檻和實際出場價。

## 獨立測試與開發流程

```bash
# 不開 Dashboard、不登入券商，單獨測試下單
conda run -n tss python -m pytest src/test_place_order.py -q

# 三個 Mock 操作流程的整合測試，不啟動網頁伺服器
conda run -n tss python -m pytest src/test_mock_runtime.py -q

# 完整離線回歸測試，或僅跑某個層級
conda run -n tss python -m pytest -q
conda run -n tss python -m pytest -m unit -q
conda run -n tss python -m pytest -m integration -q

# 開發期間自動監看，存檔後執行相關測試
conda run --no-capture-output -n tss python tools/test_runner.py --watch

# 手動依修改的模組挑測試
conda run --no-capture-output -n tss python tools/test_runner.py --related src/place_order.py
```

測試預設禁止真實券商初始化、外部網路與私人 `.env` 載入；資料庫僅允許暫存目錄。
FastAPI 測試使用記憶體內的 TestClient，券商一律使用 Fake/Mock。正式模式顯示的測試也只使用 Fake。

如需提交前自動檢查，請自行執行一次：

```bash
git config --local core.hooksPath .githooks
```

之後每次提交會在 `tss` 執行完整離線測試，失敗即阻止提交。本次未自動變更 Git 設定。
架構責任、模組介面與新增功能步驟見 [模組化與測試規範](doc/modular_testing.md)。

---

## 快速開始

全部指令都要在 conda env `tss` 下執行。

```bash
# 0. 探測 API（第一次務必先跑，結論寫進 doc/probe_findings.md）
conda run -n tss python src/probe_history.py --code TMFR1

# 1. 回補 60 天 1 分 K 並跑一輪（不發通知）
conda run -n tss python src/monitor.py --code TMFR1 --backfill-days 60 --once --no-telegram

# 2. 產生靜態圖，拿去跟 TradingView 對照
conda run -n tss python src/snr_1_hour_chart.py --code TMFR1 --boxes-csv out/boxes.csv

# 3. 開儀表板（另開一個終端機）
conda run -n tss python src/dashboard.py --port 8765

# 4. 盤中常駐監控
conda run -n tss python src/monitor.py --code TMFR1

# 5. 回補歷史一檔委託簿（只抓價格在箱體內的時段）
conda run -n tss python src/backfill_orderbook.py --code TMFR1 --days 30 --dry-run
conda run -n tss python src/backfill_orderbook.py --code TMFR1 --days 30
```

測試：`conda run -n tss python -m pytest src -q`

---

## 觸發邏輯

閘門是 **1 分 K**，不是小時棒：

```
outside ──1分K 進入箱體──▶ inside ──1分K 離開箱體──▶ outside
            │                   │
            │                   ├─ 每分鐘：持續寫委託簿到 DB
            │                   └─ 每分鐘：寫委託簿到 DB
            └─ 進箱瞬間：開 box_visit、
               回寫進箱前 30 筆委託簿作為前文
```

- **進箱判定**用 1 分 K 的 `[low, high]` 區間與箱體相交，不是收盤價 —— 只用影線插進
  箱體又彈走也算，那正是最值得留委託簿紀錄的時刻。
- **箱體選擇**：每側取最新且 `state != 'broken'` 的那一個（Pine 本身每側也只維護一個）。
- **小時反轉型態是加料不是閘門**：只在 `inside` 期間偵測，命中時標記該筆委託簿（只記錄，不發通知）。
- **Telegram 通知**由 `config/alerts.json` 的規則決定，見下節。

### 為什麼預設 `--exit-buffer-atr 0.1`

價格在箱緣來回會讓 visit 每分鐘開開關關。回放 30 天 TMF 資料實測：

| 緩衝 | visit 數 | 箱內總分鐘 | API 呼叫（≥3 分鐘的窗口） |
|---|---|---|---|
| 0.00 | 146 | 2,502 | 97 |
| **0.10** | **84** | **2,745** | **73** |
| 0.20 | 55 | 2,961 | 55 |

0.1 讓 visit 少 42%，涵蓋的分鐘數反而**多 10%** —— 因為破碎的片段被合併成連續窗口。

---

## 手畫趨勢線 / 曲線

圖表右上兩個工具：

* **畫線**：點 K 棒放錨點（2 點直線，3+ 點折線）→「完成」/ Enter 儲存。沿最後一段斜率向右延伸。
* **曲線**（同 TradingView Curve）：點起點 → 點終點 → 移動滑鼠拉彎（即時預覽）→ 點一下儲存。
  二次 Bézier，曲線經過起點與終點，第三點是**控制點**（只把曲線拉向它，不經過它），
  必須落在起點與終點之間。過了終點沿終點切線（控制點→終點）向右延伸，警報才有線可比。

Esc 取消。都在 **bar index 空間**計算（跨盤別空檔不會彎折），與圖上看到的一致。

線的延伸段不參與價格軸自動縮放：圖表永遠以 K 棒（與兩個進行中的箱體）為準，
不會因為某條線延伸得很遠就把整張圖縮小。圖表只有兩個窗格：價格與成交量。

---

## Telegram 通知規則（`config/alerts.json`）

所有可調參數都在這一個檔案：通知規則與時區（`utc_offset_hours`，見下節）。

`monitor.py` 每分鐘用 **最新 1 分 K 收盤** 檢查下列規則。檔案存檔後下一輪就生效，不必重啟；
JSON 格式錯誤時沿用上一次的設定並在 log 警告。

全域開關 `telegram_enabled` 控制所有 Telegram 通知（預設 `true`）。在 `config/alerts.json`
將它設為 `false` 即可暫停通知，改回 `true` 即可恢復，下一輪生效，不必重啟。
暫停期間仍持續收集行情、記錄委託簿與偵測型態，但不新增通知紀錄，也不消耗冷卻時間或每條線的一次性通知。
恢復後只檢查當下最新的 1 分 K，不補發暫停期間的通知。請使用 JSON 布林值 `true` / `false`，不要加引號。
`monitor.py` 啟動時若使用 `--no-telegram`，即使設定為 `true`，該程序仍不會發送；需移除此旗標並重啟才能恢復發送。
Dashboard 的進場成交通知也遵守 `telegram_enabled`，詳見上方交易操作說明。

| 規則 | 條件 | 參數 |
|---|---|---|
| `support_bottom_third` | 收盤在支撐箱內、位於下緣 1/3（0 = 箱底，1 = 箱頂） | `zone_max`（預設 0.3333） |
| `resistance_top_third` | 收盤在阻力箱內、位於上緣 1/3 | `zone_min`（預設 0.6667） |
| `box_in_out` | 前一根 1 分 K 收盤在箱內、最新一根收盤在箱外（上下皆算：支撐箱向上＝守住、向下＝跌破；阻力箱向上＝突破、向下＝守住）。只看收盤，不套用 `--exit-buffer-atr` | — |
| `trendline_near` | 收盤距離畫線 ≤ 門檻 | `distance_points`（預設 30）；設了 `distance_atr` 則改用 `distance_atr × 小時 ATR` |
| `trendline_break_below_up` | 收盤跌破**上升**的線（**每條線只通知一次**） | `buffer_points` |
| `trendline_break_above_down` | 收盤突破**下降**的線（**每條線只通知一次**） | `buffer_points` |

* 每條規則都有 `enabled`；趨勢線規則另有 `kinds`（`["curve", "line"]`，只要曲線就填 `["curve"]`）。
* 冷卻時間 `cooldown_minutes`（預設 10）以「規則 × 箱體／線」計算，以觸發的那根 1 分 K 時間為準，
  重啟後仍有效；個別規則可再寫自己的 `cooldown_minutes` 覆蓋。
* 線的值與斜率取在最新一根小時棒（通常是正在形成的那根），與圖上畫出來的一致；過了終點沿切線延伸。
* `--alerts-config` 可指定別的檔案。

## 時區（同一個 `config/alerts.json` 的 `utc_offset_hours`）

DB 與盤別時段都是台北時間。程式裡的「現在」一律用 **UTC + `utc_offset_hours`**（預設 8），
不看電腦本身的時區，所以人在日本（UTC+9）也不必改；只有在盤別判斷、落後分鐘或通知時間
看起來整體偏移時才需要調整。改完需重啟 `monitor.py` 與 `dashboard.py`。

---

## 反轉型態

只在箱體內偵測，且**方向必須對應箱體側**（做多型態只算在支撐箱，做空只算在阻力箱）。
前置濾網 `range >= min_range_atr × 小時ATR`（預設 0.3）擋掉雜訊小棒。

| 型態 | 方向 | 條件 |
|---|---|---|
| `long_lower_tail` 長下影／錘子 | long | 下影 ≥ 2×實體、≥ 0.5×全長，收盤位於上 40% |
| `long_upper_tail` 長上影／射擊之星 | short | 鏡像 |
| `bullish_engulfing` 多頭吞噬 | long | 前根陰線、本根陽線且完整包覆前根實體 |
| `bearish_engulfing` 空頭吞噬 | short | 鏡像 |
| `piercing_line` 貫穿線 | long | 開在前根收盤之下、收在前根實體中點之上但未過前根開盤 |
| `dark_cloud_cover` 烏雲罩頂 | short | 鏡像 |

小時棒**未收完**時型態是暫定的，所以 `reversal_events` 是 append-only，每筆記錄
`bar_closed` 與 `minutes_into_bar`，事後可分析「暫定型態最後有沒有成形」。

---

## 委託簿資料

| 資料 | 能否取得 | 來源 |
|---|---|---|
| **歷史五檔（深度）** | ❌ **不行** | Shioaji 整份 Data API 只有 11 個方法，無任何深度歷史端點 |
| **即時五檔** | ⚠️ 待驗證 | `subscribe(QuoteType.BidAsk)` + `BidAskFOPv1`，含 `diff_bid_vol`/`diff_ask_vol` |
| **歷史一檔** | ✅ **≥120 交易日** | `api.ticks()` 每筆成交附帶當下最佳買賣價量 |
| 即時一檔（降級） | ✅ | `api.snapshots()`，每分鐘一列 |

**五檔無法回補，只能從現在開始累積** —— 所以訂閱是常駐的（不隨進出箱開關），只有
「落地」受 visit 控制，這樣進箱瞬間才有前文可回寫。

`orderbook_snapshots.source` 與 `levels` 標示每一列的來源（`bidask_v1` 五檔 /
`ticks` 歷史一檔 / `snapshot` 即時一檔），日後分析不會混淆。

### 配額

每日上限 **500 MiB**。`api.ticks(AllDay)` 每交易日約 **10 MB**，所以：

- **不能**放進每分鐘迴圈。
- 歷史回補用 `TicksQueryType.RangeTime`，只抓價格在箱內的分鐘 —— 實測 3 個窗口
  共 18,120 筆 ticks，配額幾乎沒動。
- `monitor.py` 的 `UsageGuard` 在超過 80% 時自動把輪詢間隔加倍。
- `api.usage()` **回報有延遲**，無法用前後夾擠量測單次呼叫成本。

---

## 模組

| 檔案 | 職責 |
|---|---|
| `src/snr_core.py` | Pine 指標數學（`pine_rma`/`pine_atr`/`collect_snr_boxes`）＋ Plotly 圖，零 shioaji 依賴 |
| `src/session_bars.py` | 台指盤別時段 ＋ 1分K → 時段對齊 60分K |
| `src/db.py` | sqlite schema 與存取 |
| `src/shioaji_client.py` | 登入、合約解析、kbars/ticks 取得（含 API 怪癖處理） |
| `src/place_order.py` + `src/shioaji_order_adapter.py` | 獨立委託驗證與券商介面轉換，可只用 Mock 測試 |
| `src/trading.py` | 交易協調、持倉同步與停損停利監控，可注入時鐘、API 與停用 worker |
| `src/curve_entry.py` + `src/curve_entry_controller.py` | 共用突破訊號、持久化的一次進場狀態與送單控制 |
| `src/mock_market_data.py` + `src/mock_broker.py` | 固定 V 型行情、純記憶體委託撮合 |
| `src/mock_runtime.py` + `src/mock_routes.py` | 離線回放組裝、一次曲線買入與操作 API |
| `src/box_watch.py` | 進/出箱狀態機 |
| `src/patterns.py` | 小時 K 棒反轉型態 |
| `src/orderbook.py` | 五檔訂閱、環形緩衝、降頻、歷史 tick 轉換 |
| `src/trendline.py` | 手畫趨勢線／Bézier 曲線：bar-index 計算、向右延伸 |
| `src/alert_rules.py` | Telegram 通知規則判斷與 `config/alerts.json` 載入 |
| `src/market_clock.py` | 市場時鐘（UTC + `config/alerts.json` 的 `utc_offset_hours`） |
| `src/telegram_notify.py` | 通知格式與發送 |
| `src/monitor.py` | 每分鐘主迴圈（寫 DB、發通知） |
| `src/dashboard.py` + `src/web/` | 行情圖表與期貨下單介面；圖表用 lightweight-charts |
| `src/backfill_orderbook.py` | 歷史一檔委託簿回補（只抓箱體期間） |
| `src/probe_history.py` | API 能力探測 |
| `src/snr_1_hour_chart.py` | 靜態 HTML 繪圖 CLI |

正式行情使用兩個常駐行程：`monitor.py` 寫行情，`dashboard.py` 讀圖表並處理下單／畫線。sqlite 跑 WAL 模式；
儀表板掛掉不影響資料收集。

行情 session 恢復、逐合約 tick 健康、獨立告警與部署步驟見
[行情故障恢復操作手冊](doc/market_connection_recovery.md)。正式啟用前需設定已核對的
`config/market_calendar.json`；缺少日曆或回補未完成時暫停新倉與曲線功能，固定保護仍需新鮮 tick
及可核對的券商狀態。交易端完整重新登入預設關閉，待模擬驗證跨程序影響後再開啟。

### 儀表板路由

| 路由 | 內容 |
|---|---|
| `/` | lightweight-charts 圖表與期貨下單頁（`src/web/lwc.html`） |
| `/api/chart` | `bars` + `boxes` + `series`，同一次 DB 讀取產生，所以三者必然一致 |
| `/api/bars`、`/api/boxes`、`/api/series` | `/api/chart` 的單項版本，除錯用 |
| `/api/trendlines` | `GET` 列表、`POST` 新增（`anchors:[{time,price},…]`）、`DELETE /{id}` 軟刪除 |
| `/api/state` | 現價、落後分鐘、兩側箱體、近期停留／型態／通知 |
| `/api/trading/*` | Shioaji 合約、委託、保證金、即時出場監控 |

下單介面使用 `.env` 的 `SHIOAJI_SIMULATION` 決定模擬或正式環境，頁面會顯示目前模式；正式下單需設定 `SHIOAJI_CA_PATH`、`SHIOAJI_CA_PASSWD`、`SHIOAJI_PERSON_ID`。新倉送出前會顯示確認摘要。`config/trading.json` 的 `auto_exit_enabled` 預設為 `true`；設為 `false` 時停損／停利觸發只提醒，不會自動送平倉單。

停損與固定停利預設為實際成交均價上下各 2 倍 1 小時 ATR，可在圖上拖曳或輸入調整，按「儲存」才生效。持倉成交後可由該筆部位選擇已保存的 1 小時曲線，曲線取代固定 +2 ATR 停利，但保留原停損；多單在 1 分 K 收盤由曲線上方穿越到下方時觸發停利，空單反向。曲線 target 唯讀並跟隨最新曲線位置，stop 可獨立修改；自動保本依 `config/trading.json` 的門檻與保留點數收緊 stop（預設超過 100 點後保留 5 點）。停損依即時 tick 監控，曲線停利依已收盤的 1 分 K 監控，觸發後送市價 IOC 平倉；服務、行情 tick 與分鐘 K 資料需持續更新。委託回報不明時會停止自動重送，須核對券商端委託。

委託列表可取消尚未成交的委託；部分成交時只取消剩餘未成交口數，已成交部位會保留。`Submitted` / `PreSubmitted` 代表委託尚未成交，需等待模擬撮合回報成為 `Filled` 才會是模擬持倉並啟動出場監控。成交部位可在出場編輯器切換固定價格或已保存的 1 小時曲線停利，確認價格並儲存後生效。點選委託或持倉後，圖表顯示該單損益區塊；未成交限價單以唯讀預覽呈現。模擬環境的 margin 回傳值可能為零，不能用它判斷成交或持倉，應以委託／成交回報為準。

取消委託後，介面會顯示「等待取消確認」並停用該筆取消按鈕；取消 API 回覆後最多追蹤 10 秒，只有券商狀態為 `Cancelled` 才顯示確認成功。尚未確認或查詢失敗會在該筆委託旁顯示提示，背景監控仍會更新狀態，不會自動重送取消。操作結束會重新查詢保證金。取消介面的延遲回報、逾時與重複點擊測試可執行 `node tools/test_cancel_ui.cjs`。

圖表右上可切換 **1m / 5m / 15m / 1h**，預設 1h，選擇會保留在網址的
`timeframe` 參數。K 棒與成交量依所選週期彙整，各週期都以日／夜盤開盤時間
對齊，不補休市或缺資料的空棒。「最近」顯示最新 120 根 K 棒。
支撐／阻力箱與 ATR 仍依 1h 計算。1m／5m／1h 圖可畫曲線，直線工具限 1h；
15m 圖僅檢視。各週期曲線都會顯示在其他週期圖上，標籤註明來源週期，曲線形狀依來源週期的 K 棒索引計算。
進場選單可選任一 1m／5m／1h 曲線，一次啟用一條，統一以相鄰已收盤 1 分 K 穿越判斷多空進場，
僅觸發一次、不補做啟用前訊號。成交後預設以成交均價設定停損、停利各 2 倍進場 1h ATR，
進場曲線不會自動成為停利曲線；既有曲線停利與小時通知仍僅使用 1h 曲線。
`/api/trendlines` 的新增輸入及列表回傳包含 `timeframe=1m|5m|1h`；省略時預設 `1h`，舊資料庫升級後的畫線也保留為 `1h`。
`/api/chart` 及其單項版本也接受 `timeframe=1m|5m|15m|1h`。
畫線週期限制、儲存請求、進場選單與確認摘要的前端測試可執行 `node tools/test_curve_ui.cjs`。

兩件圖表層容易踩到、已經在 code 裡註解過的事：

1. **時間戳不做時區轉換。** lightweight-charts 把每個 `UTCTimestamp` 一律當 UTC 畫，
   而 DB 裡全是 naive 台北時間，所以直接把 naive 值當 UTC 送出去，軸上印出來才會是
   台北時間。多加 8 小時反而會變成 16:45。
2. **箱體靠 logical index 定位，不是靠時間座標。** `timeToCoordinate()` 內部寫死
   `findNearest=false`，右緣不剛好落在某根 K 棒上的箱體（例如 #18 收在 05:00，但最後
   一根小時棒是 04:00）會直接回 `null` 而整個消失。前端改用
   `timeToIndex(t, true)` 解析成整數 index，再用 `logicalToCoordinate()` 換算 —— 注意
   後者只吃整數，傳 `index - 0.5` 會回 0 而不是報錯，箱體會無聲塌成零寬。

---

## 資料表

| 表 | 內容 |
|---|---|
| `minute_bars` | 1 分 K（`PRIMARY KEY (code, minute_start)`，重抓覆蓋） |
| `hourly_bars` | 時段對齊小時棒，`bar_closed` 標示是否收完 |
| `snr_boxes` | 當前箱體表（每輪整表重寫） |
| `box_visits` | 一次進出箱一列，`source` 區分 `live` / `backfill` |
| `orderbook_snapshots` | 委託簿，`UNIQUE (code, event_ts, source)` 讓重複 flush 冪等 |
| `reversal_events` | 反轉型態（append-only） |
| `telegram_alerts` | `kind` = 規則名、`box_type` = 對象（`support:7`、`line:3`）、`hour_start` = 觸發的 1 分 K；冷卻與「只通知一次」都從這張表讀回 |
| `trendlines` | 手畫線錨點（JSON）與 `kind`（`line`/`curve`），`enabled=0` 為軟刪除 |
| `backfill_progress` | 回補進度，中斷可續 |

---

## API 怪癖（已實測，見 `doc/probe_findings.md`）

1. **`kbars` 的 `ts` 是收盤分鐘**，不是開盤分鐘。`shioaji_client.fetch_minute_bars`
   統一 −1 分鐘校正。校正後日盤 `08:45..13:44`（300 根）、夜盤 `15:00..04:59`
   （840 根），每交易日 1140 根 → **19 根小時棒**。
   原本 `13:46` 那根是 13:45 收盤集合競價，校正後被 `session_fields` 丟掉 —— 這與
   sibling repo 的逐筆版行為一致，箱體能對得上。
2. **`kbars` 依曆日切，`ticks` 依交易日（盤別）切**。`ticks(date=10-08)` 第一筆是
   `10-07 15:00`。兩者日期視窗不可混用。
3. **`api.usage()` 回報有延遲**。

---

## 環境

macOS 與 Windows 都使用 conda env `tss`，以 Python 3.10 驗證。先在專案根目錄確認實際執行環境：

```sh
conda run -n tss python --version
conda run -n tss python -m pip --version
```

**Python 低於 3.10（例如 3.8.5）時，先升級或依下方 Windows 流程重建環境，不能直接安裝本專案的版本清單。**

若 Python 已為 3.10 以上，而啟動 Dashboard 出現 `ModuleNotFoundError: No module named 'fastapi'`，在**既有** `tss` 補上 Dashboard 套件：

```sh
conda run -n tss python -m pip install -r requirements-dashboard.txt
conda run -n tss python src/dashboard.py --help
```

清單包含已驗證版本的 FastAPI、Uvicorn 與 Pydantic 2；專案使用 `field_validator` 等 Pydantic 2 介面，不能只安裝舊版 Pydantic。這份補裝清單不指定或主動更換 Shioaji SDK。`--help` 可檢查匯入及啟動參數，不登入券商、不啟動伺服器。

Python 需至少 3.10；若現有 `tss` 太舊，先安排環境升級。**全新環境**的完整安裝方式為：

```sh
conda create -n tss python=3.10 pip
conda run -n tss python -m pip install -r requirements.txt
conda run -n tss python src/dashboard.py --help
```

`requirements.txt` 包含行情、券商 SDK、圖表與 Dashboard 的完整直接依賴，版本以本機通過測試的 `tss` 為基準；它不是所有間接依賴的完整 lockfile。既有正式環境要採用這份完整基準時，也包含 SDK 版本變更，須另行完成相容性驗證。不要用未指定環境的 `pip`，以免套件被裝到其他 Python。

### Windows：刪除 Python 3.8 的 tss，以 Miniforge 重建 Python 3.10

`requirement_windows.txt` 是 Windows x86-64／Python 3.10 的完整執行依賴，包含券商 SDK 與 Dashboard；版本與專案已測試的基準一致，尚未在 Windows 原生主機完成正式驗收。

先關閉使用舊 `tss` 的 monitor、Dashboard、watchdog、Python 終端及排程工作，再於**原本的 Conda／Anaconda Prompt**確認並刪除舊環境。下列刪除步驟會移除 `tss` 裡的 Python 與套件；不會刪除專案目錄中的 `.env` 或 `data`。

```bat
conda env list
conda deactivate
conda env remove -n tss -y
conda env list
```

刪除前確認 `env list` 中的 `tss` 是要移除的 Python 3.8 環境。若還顯示 `(tss)`，先再次 `conda deactivate`，再進行刪除；移除失敗時先處理錯誤，不繼續混用新舊環境。

從 [Miniforge 官方 Windows 安裝程式](https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Windows-x86_64.exe) 安裝 Miniforge3（已安裝可略過）。安裝完成後關閉舊終端，從開始功能表開啟 **Miniforge Prompt**；使用這個新視窗建立環境，避免繼續使用舊 Anaconda 的 `conda`。

```bat
conda info --base
conda create -n tss --override-channels -c conda-forge python=3.10 pip -y
conda activate tss
python --version
where python
```

`conda info --base` 應指向此次 Miniforge 安裝目錄；`python --version` 應顯示 `3.10.x`，`where python` 第一個結果應為新 `tss` 的 Python。若仍顯示 `3.8.5`，先修正視窗／環境選擇，不要繼續安裝套件。

切換至專案目錄，安裝並驗證依賴：

```bat
cd /d C:\Users\Drey\Documents\Python_Scripts\shioaji_api
python -m pip install -r requirement_windows.txt
python -m pip check
python src/dashboard.py --help
```

依賴與匯入檢查都成功後，再執行原本的正式啟動命令：

```bat
conda run --no-capture-output -n tss python src/dashboard.py --code TMFR1 --port 8765
```

若原先 Windows 工作排程器／IDE 指向舊環境的 Python 絕對路徑，也需依 `where python` 更新為新 Miniforge `tss` 的路徑。

需要執行離線測試或測試監看時，額外安裝測試依賴：

```sh
conda run -n tss python -m pip install -r requirements-dev.txt
conda run -n tss python -m pytest -q
```

`sqlite3`、Windows 的 `msvcrt` 與 macOS 的 `fcntl` 都是標準函式庫，不另用 pip 安裝。

前端沒有 node／npm／打包步驟，JS 一律用 `<script>` 直接載入本地檔案：

* `src/web/lightweight-charts.standalone.production.js`（195 KB，v5.2.1）是手動
  vendored 的，不走 CDN —— 監控頁斷網就變白畫面比沒有頁面更糟。檔案不在時
  `dashboard.py` 會直接報錯並印出補抓指令：

  ```bash
  curl -fsSL -o src/web/lightweight-charts.standalone.production.js \
    https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.1/dist/lightweight-charts.standalone.production.js
  ```

`.env` 需要：`SHIOAJI_API_KEY`、`SHIOAJI_SECRET_KEY`、`SHIOAJI_SIMULATION`、
`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`。

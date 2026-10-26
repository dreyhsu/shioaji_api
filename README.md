# TMF 1 小時 S/R 箱體監控

用 Shioaji 每分鐘更新微型臺指期貨（TMF）價格，在 1 小時圖上畫出 Pine「Support and
Resistance (High Volume Boxes)」箱體，當 **1 分 K 價格進入箱體**時開始記錄委託簿並發
Telegram 通知，並偵測箱體內的小時 K 棒反轉型態。

包含行情監控、通知與手動期貨下單；可使用下方離線 Mock API 測試，不需券商帳密。

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
3. 在右側已成交部位的停利選單選該曲線，按「套用停利」並確認。
   此時固定 +2 ATR 停利停用，原停損保留。
4. 按 **+60 分鐘**：反彈接近高點，部位應仍未平倉；再按 **+10 分鐘**三次，
   回檔的 1 分 K 收盤向下穿越約 25040 時，觸發曲線出場並模擬賣出平倉。
   預期出場價高於進場價、持倉歸零，觸發原因為 `curve`。

測試完成後按「重設情境」即可回到同一起點（會清除本次 Mock 委託、曲線及進場設定）。
這個流程使用持倉的「套用停利」；上方「啟用一次突破買入」是另一個進場測試。

1. **手動下單與 ±2 ATR**：選月份、方向與市價，確認下單。Mock 立即按當前價格成交；
   點選右側部位，在圖上檢查成交均價、停損與停利。多單為均價 −2 ATR / +2 ATR，空單相反。
2. **手動曲線移動停利**：切到 1h，按「曲線」，依提示點起點、終點與中間控制點。
   在右側已成交部位選該曲線，按「套用停利」。固定停利停用，原停損保留；
   多單於已收盤 1 分 K 向下穿越曲線時平倉，空單反向。可拖曳圖上移動停利線平移曲線。
3. **曲線突破買入**：先保存曲線，在 Mock 控制區選曲線與口數，按「啟用一次突破買入」。
   只有啟用後的已收盤 1 分 K 從曲線下方（含相等）向上穿越才會買入一次；
   成交後使用 ±2 ATR。進場曲線不會自動成為停利曲線，可再按流程 2 設定。
   測試此流程時，先將 `replay.start_bar` 改成 `null`、`start_at` 改成 `before_bottom`，
   並設 `minutes_before_bottom: 60` 後重啟；
   可畫接近 **24150** 的水平曲線，再推進約 90 分鐘，觀察下跌後反彈的突破。

三種流程共用既有交易服務、ATR 計算、持倉記錄與出場邏輯。曲線自動買入本次僅接到
Mock API，正式模式不提供該自動進場端點。啟用時不補做歷史訊號，取消或刪除曲線後不買入，
錯誤時停止且不自動重送。「重設情境」確認後會清除本次 Mock 委託、曲線與進場設定。

Mock 市價與範圍市價按最新回放收盤成交；限價只有報價符合時成交，否則 ROD 等待、
IOC/FOK 取消。模擬採無限流動性、全額成交，不模擬部分成交、手續費、滑價或券商延遲。
停損停利是程式監控後送出平倉委託，不是預掛在券商端的兩張委託。

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
| `src/curve_entry.py` | 已收盤 1 分 K 向上突破 1h 曲線的純訊號判斷 |
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

停損與固定停利預設為實際成交均價上下各 2 倍 1 小時 ATR，可在圖上拖曳調整。持倉成交後可由該筆部位選擇已保存的 1 小時曲線，曲線取代固定 +2 ATR 停利，但保留 -2 ATR 停損；多單在 1 分 K 收盤由曲線上方穿越到下方時觸發停利，空單反向。拖曳移動停利線會平移已保存曲線。停損依即時 tick 監控，曲線停利依已收盤的 1 分 K 監控，觸發後送市價 IOC 平倉；服務、行情 tick 與分鐘 K 資料需持續更新。委託回報不明時會停止自動重送，須核對券商端委託。

委託列表可取消尚未成交的委託；部分成交時只取消剩餘未成交口數，已成交部位會保留。`Submitted` / `PreSubmitted` 代表委託尚未成交，需等待模擬撮合回報成為 `Filled` 才會是模擬持倉並啟動出場監控。成交部位可直接切換固定 +2 ATR 或已保存的 1 小時曲線停利。點選委託或持倉後，圖表顯示限價委託／成交均價、停損和停利價位；未成交限價單以預覽線呈現。模擬環境的 margin 回傳值可能為零，不能用它判斷成交或持倉，應以委託／成交回報為準。

圖表右上可切換 **1m / 5m / 15m / 1h**，預設 1h，選擇會保留在網址的
`timeframe` 參數。K 棒與成交量依所選週期彙整，各週期都以日／夜盤開盤時間
對齊，不補休市或缺資料的空棒。「最近」顯示最新 120 根 K 棒。
支撐／阻力箱、ATR 與已儲存的趨勢線仍依 1h 計算；畫線工具限 1h，維持與通知規則一致。
`/api/chart` 及其單項版本也接受 `timeframe=1m|5m|15m|1h`。

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

conda env `tss`，Python 3.10。Mock、測試監看與 Git hook 不需安裝新套件（sqlite3 是 stdlib）。

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

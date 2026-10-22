# TMF 1 小時 S/R 箱體監控

用 Shioaji 每分鐘更新微型臺指期貨（TMF）價格，在 1 小時圖上畫出 Pine「Support and
Resistance (High Volume Boxes)」箱體，當 **1 分 K 價格進入箱體**時開始記錄委託簿並發
Telegram 通知，並偵測箱體內的小時 K 棒反轉型態。

**只做監控與通知，不下任何單。**

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
            │                   └─ 每小時：Telegram 提醒（仍在箱內）
            └─ 進箱瞬間：開 box_visit、立即發 Telegram、
               回寫進箱前 30 筆委託簿作為前文
```

- **進箱判定**用 1 分 K 的 `[low, high]` 區間與箱體相交，不是收盤價 —— 只用影線插進
  箱體又彈走也算，那正是最值得留委託簿紀錄的時刻。
- **箱體選擇**：每側取最新且 `state != 'broken'` 的那一個（Pine 本身每側也只維護一個）。
- **小時反轉型態是加料不是閘門**：只在 `inside` 期間偵測，命中時標記該筆委託簿並發強提示。

### 為什麼預設 `--exit-buffer-atr 0.1`

價格在箱緣來回會讓 visit 每分鐘開開關關。回放 30 天 TMF 資料實測：

| 緩衝 | visit 數 | 箱內總分鐘 | API 呼叫（≥3 分鐘的窗口） |
|---|---|---|---|
| 0.00 | 146 | 2,502 | 97 |
| **0.10** | **84** | **2,745** | **73** |
| 0.20 | 55 | 2,961 | 55 |

0.1 讓 visit 少 42%，涵蓋的分鐘數反而**多 10%** —— 因為破碎的片段被合併成連續窗口。

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
| `src/box_watch.py` | 進/出箱狀態機 |
| `src/patterns.py` | 小時 K 棒反轉型態 |
| `src/orderbook.py` | 五檔訂閱、環形緩衝、降頻、歷史 tick 轉換 |
| `src/telegram_notify.py` | 通知格式與發送 |
| `src/monitor.py` | 每分鐘主迴圈（寫 DB、發通知） |
| `src/dashboard.py` + `src/web/` | 唯讀儀表板，每 60 秒更新；圖表用 lightweight-charts |
| `src/backfill_orderbook.py` | 歷史一檔委託簿回補（只抓箱體期間） |
| `src/probe_history.py` | API 能力探測 |
| `src/snr_1_hour_chart.py` | 靜態 HTML 繪圖 CLI |

兩個常駐行程：`monitor.py` 寫、`dashboard.py` 唯讀。sqlite 跑 WAL 模式，互不阻塞；
儀表板掛掉不影響資料收集。

### 儀表板路由

| 路由 | 內容 |
|---|---|
| `/` | lightweight-charts 圖表頁（`src/web/lwc.html`） |
| `/plotly` | 舊的 Plotly 頁，留著對照箱體幾何用 |
| `/api/chart` | `bars` + `boxes` + `series`，同一次 DB 讀取產生，所以三者必然一致 |
| `/api/bars`、`/api/boxes`、`/api/series` | `/api/chart` 的單項版本，除錯用 |
| `/api/state` | 現價、落後分鐘、兩側箱體、近期停留／型態／通知 |
| `/api/figure` | 舊的 Plotly figure JSON，只有 `/plotly` 在用 |

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
| `telegram_alerts` | `UNIQUE (code, hour_start, box_type, kind)` **就是**每小時每側每類最多一則的限流 |
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

conda env `tss`，Python 3.10。不需安裝任何新套件（sqlite3 是 stdlib）。

前端沒有 node／npm／打包步驟，JS 一律用 `<script>` 直接載入本地檔案：

* `src/web/lightweight-charts.standalone.production.js`（195 KB，v5.2.1）是手動
  vendored 的，不走 CDN —— 監控頁斷網就變白畫面比沒有頁面更糟。檔案不在時
  `dashboard.py` 會直接報錯並印出補抓指令：

  ```bash
  curl -fsSL -o src/web/lightweight-charts.standalone.production.js \
    https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.1/dist/lightweight-charts.standalone.production.js
  ```

* `src/web/plotly.min.js`（4.8 MB）由 `plotly.offline.get_plotlyjs()` 從已安裝套件
  取出，只有 `/plotly` 對照頁還在用。

`.env` 需要：`SHIOAJI_API_KEY`、`SHIOAJI_SECRET_KEY`、`SHIOAJI_SIMULATION`、
`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`。

# TMF 1 小時 S/R 監控（簡化版）— 實作計畫

## Context（為什麼要做）

`shioaji_api` 目前幾乎是空骨架：

- `notebook/test.ipynb` 證明取價路徑可行（login → `api.Contracts.Futures.TXF["TXFR1"]` → `api.kbars()` → logout），但**從不存檔**，所以 `data/` 是空的。
- `src/snr_1_hour_chart.py` 是 `/Users/chihjuihsu/Documents/python_script/snr_strategy/src/pine_snr_boxes_1h_chart.py` 的逐位元複本，**在本 repo 無法執行**：第 61-79 行 import 的 `backtest_tmf_hourly_diamond_short`、`backtest_tmf_dense_long`、`backtest_tmf_hourly_support_long` 都不存在於此，`data/txf`、`data/btcusdt_bars`、`txf_tick` 也不存在。
- `src/telegram_sample.py` 提供可直接沿用的 Telegram 發送寫法（`requests.post` 到 `sendMessage`、`parse_mode=HTML`、token 從 `.env` 讀）。

要做四件事：

1. **低頻價格更新** — 每 1 分鐘抓一次，寫進 DB 表。
2. **進箱即錄** — **1 分 K 價格回落進支撐箱、或漲進阻力箱**時，開始把**委託簿寫進 DB** 供日後分析，並開始 Telegram 通知。
3. **1 小時反轉型態** — 在箱體內的未完成 1 小時 K 棒出現長下影／長上影／吞噬線型等反轉型態時標記出來（圖上、DB、Telegram 訊息中）。
4. **儀表板** — TMF 1 小時圖 + S/R 指標，每分鐘更新。

**只做監控與通知，不下單。**

---

## 觸發邏輯（本計畫的核心）

閘門是 **1 分 K**，不是 1 小時棒：

```
每分鐘取最新 1 分 K
  ↓
對「每側最新且 state != broken 的箱體」各跑一次狀態機（支撐、阻力各一份）
  ↓
outside ──1分K 進入箱體──▶ inside ──1分K 離開箱體──▶ outside
            │                   │
            │                   └─ 每分鐘：持續寫委託簿到 DB
            │                      每小時：Telegram 提醒（仍在箱內）
            └─ 進箱瞬間：開 box_visit、立即發 Telegram、
               回寫進箱前 30 筆委託簿作為前文
```

- **「進入箱體」判定**：`bar.low <= box.top and bar.high >= box.bottom`（1 分 K 的高低區間與箱體相交）。用區間而非收盤價，是為了抓到只用影線插進箱體就彈走的情況 —— 那正是最值得留下委託簿紀錄的時刻。
- **「回到／漲到」的語意**由狀態機的 `outside → inside` 轉換體現，所以價格在箱內上下震盪不會重複觸發開場。
- **箱體選擇**：每側取**最新且 `state != 'broken'`** 的那一個（Pine 本身每側也只維護一個當前箱體）。已跌破/突破的箱體不再視為有效區域。
- 兩側各自獨立跑狀態機（箱體有可能重疊）。
- **1 小時反轉型態**是**加料**，不是閘門：它只在 `inside` 期間偵測，命中時標記該筆委託簿、寫 `reversal_events`、並讓 Telegram 訊息升級為強提示。

**Telegram 節奏**：進箱**立即**一則，之後**每小時最多一則**（仍在箱內的提醒），靠 `telegram_alerts` 的 `UNIQUE (code, hour_start, box_type)` 天然去重 —— 重啟 monitor 也不會重複發。

---

## 先回答：歷史委託簿拿不拿得到？

我把安裝的 `shioaji 1.7.7` 的 `_core.pyi` 整份 Data API 列出來了，**總共只有 11 個方法**：
`snapshots`、`ticks`、`kbars`、`credit_enquires`、`short_stock_sources`、`scanners`、`punish`、`notice`、`daily_quotes`、`index_components`、`fetch_contracts`。

| 資料 | 能否取得 | 說明 |
|---|---|---|
| **歷史五檔（深度）** | ❌ **不行** | shioaji 沒有任何歷史委託簿深度端點。五檔只存在即時串流 `BidAskFOPv1`。 |
| **歷史一檔（最佳買賣）** | ✅ **可以** | `api.ticks(contract, date, query_type=AllDay)` 每筆成交都附帶 `bid_price / bid_volume / ask_price / ask_volume` —— 即該筆成交當下的最佳買賣價量。唯一可回補的委託簿類資料。 |
| TAIFEX Daily 逐筆檔 | ❌ | 欄位只有 `成交日期/商品代號/到期月份/成交時間/成交價格/成交數量(B+S)/近月價格/遠月價格/開盤集合競價`，無委託簿。 |

**所以：五檔無法回補，只能從現在開始累積。** 這正是需求 2 的真正價值 —— 它是唯一的取得途徑，所以計畫採「進箱後持續錄每一筆五檔更新」，而不是只存一張觸發快照。

`probe_history.py` 會實測 `api.ticks` 能往回抓多久、`bid_*`/`ask_*` 是否真的有值 → 決定一檔歷史能做多少回溯分析。

---

## 已驗證的環境事實

- conda env `tss`、Python 3.10.19。一律 `/opt/homebrew/Caskroom/miniconda/base/bin/conda run -n tss python ...`。
- 已安裝：pandas 2.3.3、numpy 2.2.6、plotly 6.5.2、shioaji 1.7.7、python-dotenv、**requests 2.32.5**、**fastapi 0.128.0**、**uvicorn 0.40.0**、jinja2 3.1.6、**sqlite3 3.51.0（stdlib）**、pytest 9.0.1。
- 缺 pyarrow/dash/streamlit → **DB 用 sqlite3，儀表板用 FastAPI + Plotly.js，不需安裝任何新套件**。
- 前端 Plotly.js 由 `plotly.offline.get_plotlyjs()` 從已安裝套件取出寫成 `src/web/plotly.min.js`，離線可用、不依賴 CDN。
- `.env` 已有 `SHIOAJI_API_KEY`、`SHIOAJI_SECRET_KEY`、`SHIOAJI_SIMULATION`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`（另有未使用的 CA / FINMIND / FUGLE 欄位）。

### 相關 shioaji 介面

| 用途 | 呼叫 | 關鍵欄位 |
|---|---|---|
| 歷史/當日 1 分 K | `api.kbars(contract, start, end)` | `ts, Open, High, Low, Close, Volume, Amount`（`start`/`end` 只到**日**粒度） |
| 歷史逐筆（含一檔） | `api.ticks(contract, date, query_type, last_cnt)` | `ts, close, volume, bid_price, bid_volume, ask_price, ask_volume, tick_type` |
| 快照（一檔） | `api.snapshots([contract])` | `close, open, high, low, volume, total_volume, buy_price, buy_volume, sell_price, sell_volume` |
| 即時五檔 | `subscribe(QuoteType.BidAsk, version=QuoteVersion.v1)` + `set_on_bidask_fop_v1_callback` | `bid_price[5], bid_volume[5], diff_bid_vol[5], ask_price[5], ask_volume[5], diff_ask_vol[5], bid_total_vol, ask_total_vol, simtrade` |

---

## 模組佈局

```
src/
  snr_core.py         指標數學（從 snr_1_hour_chart.py 移植，零 shioaji 依賴）
  session_bars.py     台指盤別/時段 + 1分K → 時段對齊 60分K
  db.py               sqlite schema + upsert / 查詢
  shioaji_client.py   login/logout context manager + 合約解析 + usage 監看
  box_watch.py        進/出箱狀態機（純邏輯，可測）
  patterns.py         1 小時 K 棒反轉型態偵測（純函式，可測）
  orderbook.py        五檔訂閱 + 環形緩衝 + 落地（含一檔 fallback）
  telegram_notify.py  通知（沿用 telegram_sample.py 的 send_message 寫法）
  monitor.py          1 分鐘迴圈，把上面全部串起來
  dashboard.py        FastAPI 唯讀儀表板（每分鐘更新）
  probe_history.py    實測歷史資料深度與行情權限
  snr_1_hour_chart.py 改寫：以 DB 為來源的靜態 HTML 繪圖 CLI
  web/index.html, app.js, style.css, plotly.min.js（自動產生）
  test_snr_core.py, test_session_bars.py, test_box_watch.py, test_patterns.py, test_db.py
data/
  tmf.db
```

兩個常駐行程：**`monitor.py`**（寫 DB、發 Telegram）與 **`dashboard.py`**（唯讀 DB）。職責分離，儀表板掛掉不影響資料收集。

---

## 步驟

### 步驟 0 — `src/probe_history.py`（先做，結果決定後面的參數）

單檔 CLI，用現有 `.env` 登入，依序回報後 logout：

1. `api.usage()` —— 流量配額基準線（**關鍵**：決定 1 分鐘輪詢 kbars 是否可行）。
2. `api.Contracts.Futures.TMF["TMFR1"]` 印出 `FuturesInfo`，確認 TMF 近月存在、`tick`、`multiplier`。
3. **解決 kbars `ts` 標記問題**：抓一個完整交易日，印出日盤段最後一根的 `ts`。`13:44` ⇒ `ts` 是**開盤分鐘**；`13:45` ⇒ 是**收盤分鐘**，後續全部要 `-1min` 校正。錯了會讓時段歸屬整體位移。
4. **歷史深度**：對 1/5/20/60/120 個交易日前分別試 `api.ticks(contract, date=D, query_type=AllDay)`，印出成功/失敗與筆數 → 得到實際可回溯天數。同時印出前 3 筆的 `bid_price/bid_volume/ask_price/ask_volume`，確認一檔欄位**真的有值**而非 0。
5. **即時五檔是否可用**：訂閱 `QuoteType.BidAsk` 15 秒，計數並印出前 3 筆（含 `simtrade`），**並算出每秒平均更新筆數** → 用來推估 DB 成長速度與決定 `--orderbook-max-hz`。`SHIOAJI_SIMULATION=1` 是否推送真實行情未知；若收不到，需求 2 會降級成 `api.snapshots()` 的一檔。
6. **量測 kbars 單次請求成本**：抓一天前後各呼叫一次 `api.usage()`，算出單次 kbars 的位元組數 → 推算每分鐘輪詢一整天會用掉多少配額。

結論寫進 `doc/probe_findings.md`，後續常數依此設定。

### 步驟 1 — `src/snr_core.py`（純指標，從現有檔案移植）

行為必須與 `src/snr_1_hour_chart.py` 完全一致，但移除所有 sibling import：

- `pine_rma(values, length)` / `pine_atr(bars, length)` — 逐位元照抄 `snr_strategy/src/backtest_tmf_hourly_diamond_short.py:56,81`（SMA 起始 + Wilder 遞迴的**迴圈版本**）。**不可**換成 `ewm`；sibling 的 `wilder_atr` 是不同 seeding，兩者不可互換。
- `delta_volume`、`_crossed_over`、`_crossed_under`、`collect_snr_boxes`、`clip_boxes`、`session_rangebreaks`、`_box_style`、`_add_boxes`、`build_figure`、`BOX_COLUMNS`、配色常數（`#20ca26` / `#e92929`）—— 照抄現有實作。
- `collect_snr_boxes` **新增參數 `creation_limit: int | None = None`**：`index > creation_limit` 時跳過建箱區塊，但仍跑 break/hold 交叉與右緣延伸。未完成的小時棒因此不會生成新箱體（符合 Pine 只在 bar confirm 建箱），但仍會觸發 `broken`/`holds` 改色。
- 預設值沿用：`DEFAULT_LOOKBACK=20`、`DEFAULT_VOLUME_LENGTH=2`、`DEFAULT_BOX_WIDTH=1.0`、**TMF 用 `atr_length=20`**（不是 Pine 的 200；時段對齊小時棒一天只有 19 根）。
- 新增 `zone_pct(price, box) -> float` = `(price − bottom) / (top − bottom)`
- 新增 `bar_in_box(bar, box) -> bool` = `bar.low <= box.top and bar.high >= box.bottom`
- 新增 `current_box(boxes, box_type) -> Series | None` = 該側 `state != 'broken'` 中 `box_id` 最大者

### 步驟 2 — `src/session_bars.py`

照抄 sibling 的時段規則，**不可改**（要與既有回測一致）：

| 時鐘 | session_type | session_date |
|---|---|---|
| `08:45 ≤ t < 13:45` | `day` | `ts.normalize()` |
| `t ≥ 15:00` | `night` | `ts.normalize() + 1d` |
| `t < 05:00` | `night` | `ts.normalize()` |
| 其他 | 丟棄 | |

`session_id = f"{session_date:%Y-%m-%d} {session_type}"`；`session_anchor`：day → `date + 08:45`，night → `date − 1d + 15:00`。

- `session_fields(ts)` / `add_sessions(frame)` / `session_anchor(session_id)`
- `hour_start_of(ts)` — O(1)：`session_fields` → `session_anchor` → `anchor + floor((ts − anchor)/1h)`
- `minute_bars_to_hourly(minute_bars)` — 以 `(session_id, hour_start)` 分組：`open`=首根 open、`high`=max、`low`=min、`close`=末根 close、`volume`/`amount`=sum。**自我檢查：完整一天應得 19 根**（日盤 5 根 08:45/09:45/10:45/11:45/12:45 + 夜盤 14 根 15:00…04:00）。
- `session_rangebreaks(frame, bar_span)` — 照抄；日盤落 `:45`、夜盤落 `:00` 的兩相位問題由原演算法處理。
- `is_trading_time(ts)` — monitor 用來決定是否輪詢（非交易時段休息，省配額）。

### 步驟 3 — `src/db.py`（sqlite，需求 1 的「DB 表」）

`data/tmf.db`，開啟時 `PRAGMA journal_mode=WAL`（讓 dashboard 在 monitor 寫入時可併行讀取）。

```sql
CREATE TABLE minute_bars (
  code TEXT, minute_start TEXT, session_id TEXT,
  open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
  PRIMARY KEY (code, minute_start));

CREATE TABLE hourly_bars (                      -- 物化，dashboard/telegram 直接讀
  code TEXT, hour_start TEXT, session_id TEXT,
  open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
  bar_closed INTEGER, updated_at TEXT,
  PRIMARY KEY (code, hour_start));

CREATE TABLE snr_boxes (
  code TEXT, box_id INTEGER, box_type TEXT, state TEXT,
  top REAL, bottom REAL, pivot_price REAL, atr_width REAL, delta_volume REAL,
  left_time TEXT, right_time TEXT, created_time TEXT, state_time TEXT,
  holds INTEGER, breakouts INTEGER, updated_at TEXT,
  PRIMARY KEY (code, box_id));

-- 一次「進箱 → 出箱」為一列；委託簿與反轉型態都掛在 visit 上
CREATE TABLE box_visits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT, box_id INTEGER, box_type TEXT,
  box_top REAL, box_bottom REAL, box_state_at_entry TEXT,
  entered_at TEXT, entry_minute_start TEXT, entry_price REAL, entry_zone_pct REAL,
  exited_at TEXT, exit_minute_start TEXT, exit_price REAL,
  exit_reason TEXT,                             -- 'left_box' | 'box_broken' | 'session_end' | 'shutdown'
  minutes_in_box INTEGER, min_zone_pct REAL, max_zone_pct REAL,
  min_price REAL, max_price REAL,
  reversal_count INTEGER, orderbook_rows INTEGER,
  is_open INTEGER);                             -- 1 = 仍在箱內
CREATE INDEX idx_visits_open ON box_visits(code, is_open);

CREATE TABLE orderbook_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visit_id INTEGER, reversal_id INTEGER,        -- reversal_id 可為 NULL
  code TEXT, captured_at TEXT, event_ts TEXT,
  source TEXT,          -- 'bidask_v1'(五檔) | 'snapshot'(一檔)
  levels INTEGER,       -- 5 或 1
  phase TEXT,           -- 'pre_entry' | 'in_box' | 'at_reversal'
  bid_total_vol INTEGER, ask_total_vol INTEGER,
  bid_price TEXT, bid_volume TEXT, diff_bid_vol TEXT,   -- JSON array
  ask_price TEXT, ask_volume TEXT, diff_ask_vol TEXT,
  underlying_price REAL, simtrade INTEGER,
  UNIQUE (code, event_ts, source));             -- 防止跨輪重複落地
CREATE INDEX idx_ob_visit ON orderbook_snapshots(visit_id);

CREATE TABLE reversal_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visit_id INTEGER,
  code TEXT, detected_at TEXT, hour_start TEXT, bar_closed INTEGER, minutes_into_bar INTEGER,
  pattern TEXT, direction TEXT,
  box_id INTEGER, box_type TEXT, box_state TEXT, box_top REAL, box_bottom REAL, zone_pct REAL,
  bar_open REAL, bar_high REAL, bar_low REAL, bar_close REAL, bar_volume REAL,
  prev_open REAL, prev_high REAL, prev_low REAL, prev_close REAL,
  upper_wick REAL, lower_wick REAL, body REAL, close_pos REAL, range_atr REAL, hourly_atr REAL,
  detail TEXT);                                 -- JSON：各型態的量化細節

CREATE TABLE telegram_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT, hour_start TEXT, box_type TEXT, box_id INTEGER, visit_id INTEGER,
  kind TEXT,                                    -- 'entry' | 'hourly' | 'reversal'
  sent_at TEXT, ok INTEGER, message TEXT,
  UNIQUE (code, hour_start, box_type, kind));   -- UNIQUE 即免費去重
```

函式：`connect`、`init_schema`、`upsert_minute_bars`、`upsert_hourly_bars`、`replace_boxes`、`open_visit`、`close_visit`、`get_open_visit`、`insert_orderbook_rows`（`INSERT OR IGNORE`，回傳實際插入數）、`insert_reversal`、`record_alert`（`INSERT OR IGNORE`，回傳是否真的插入 → 決定要不要發送），以及 dashboard 用的 `load_hourly_bars`、`load_boxes`、`load_open_visits`、`load_recent_visits`、`load_recent_reversals`、`load_recent_alerts`。

兩個 `UNIQUE` 是整個設計的去重骨幹：`orderbook_snapshots(code, event_ts, source)` 讓每輪重複 flush 緩衝不會重複落地；`telegram_alerts(code, hour_start, box_type, kind)` 讓「每小時每側每類最多一則」免寫額外邏輯，且 monitor 重啟不重複發。

### 步驟 4 — `src/shioaji_client.py`

- `login()` context manager：讀 `.env`、`sj.Shioaji(simulation=...)`、`login`、`yield api`、`finally: api.logout()`。
- `resolve_contract(api, code)` — 由 code 前 3 碼取 root：`api.Contracts.Futures.TMF["TMFR1"]`，支援 `TXFR1`/`MXFR1`。
- `fetch_minute_bars(api, contract, start, end)` — `api.kbars` → DataFrame → `ts` 轉 datetime →（若步驟 0 判定 ts 是收盤分鐘則 `-1min`）→ 欄名轉小寫 → `add_sessions` 丟掉非交易時段。
- `UsageGuard` — 每 N 輪記一次 `api.usage()`，超過配額 80% 時印警告並自動把輪詢間隔加倍（避免打爆配額）。

### 步驟 5 — `src/box_watch.py`（進/出箱狀態機）

純邏輯、不碰 IO、好測。

```python
@dataclass
class BoxWatchConfig:
    exit_buffer_atr: float = 0.0   # 出箱需超出箱體多少 ATR 才算真的離開（防邊界抖動）
```

`BoxWatcher`（每側一個實例）：

- `step(minute_bar, box, hourly_atr) -> Transition | None`
- 回傳 `Transition(kind="enter"|"exit"|"stay", box_id, zone_pct, exit_reason)`。
- 規則：
  - `box is None`（該側無有效箱體）→ 若原本 `inside` 則發 `exit(reason="box_broken")`。
  - `box.box_id` 與目前 visit 不同 → 先 `exit(reason="box_broken")` 再視情況 `enter`（換箱體就換 visit）。
  - `outside` + `bar_in_box` → `enter`。
  - `inside` + `not bar_in_box`（且超出 `exit_buffer_atr` 緩衝）→ `exit(reason="left_box")`。
  - `inside` + `bar_in_box` → `stay`（更新 `min/max_zone_pct`、`minutes_in_box`）。
- `exit_buffer_atr` 預設 0；若實測發現價格在箱緣抖動造成 visit 一直開關，調成 0.05~0.1 即可，不用改程式。
- 冷啟動時由 `db.get_open_visit()` 還原狀態，所以 monitor 重啟不會憑空多開一個 visit。

### 步驟 6 — `src/patterns.py`（1 小時反轉型態，加料用）

純函式 registry，簽名 `(bar, prev_bar, hourly_atr, cfg) -> dict | None`，回傳量化細節（寫進 `reversal_events.detail`）。

前置濾網：`range >= cfg.min_range_atr * hourly_atr`（預設 0.3），濾掉雜訊小棒。

| 型態 | 方向 | 條件 |
|---|---|---|
| `long_lower_tail` 長下影／錘子 | long | `lower_wick ≥ 2×body` 且 `lower_wick ≥ 0.5×range` 且 `close_pos ≥ 0.6` |
| `long_upper_tail` 長上影／墓碑 | short | 鏡像（`upper_wick`、`close_pos ≤ 0.4`） |
| `bullish_engulfing` 多頭吞噬 | long | 前根陰線、本根陽線、`close > prev_open` 且 `open < prev_close`、`body > prev_body` |
| `bearish_engulfing` 空頭吞噬 | short | 鏡像 |
| `piercing_line` 貫穿線 | long | 前根陰線、本根開在 `prev_close` 之下、收在前根實體中點以上但未過 `prev_open` |
| `dark_cloud_cover` 烏雲罩頂 | short | 鏡像 |

其中 `body = |close − open|`、`range = high − low`、`close_pos = (close − low) / range`。

- **只在 `inside` 期間偵測**，且**方向必須對應箱體側**：long 型態只在支撐箱的 visit 計入，short 型態只在阻力箱的 visit 計入。
- 對象是**未完成的 1 小時棒** + 前一根**已收**的小時棒。未完成棒的型態是**暫定**的，所以 `reversal_events` 為 **append-only**，每筆記 `bar_closed` 與 `minutes_into_bar`；同一 `(hour_start, pattern)` 在棒收完前每分鐘命中就新增一列 —— 事後才看得出型態何時成形、有沒有變形消失。
- 命中時同步把當下的委託簿以 `phase='at_reversal'` 落地並回填 `reversal_id`。
- `PatternConfig` dataclass 集中所有門檻（`min_range_atr`、`wick_body_ratio`、`wick_range_ratio`、`close_pos_long`、`close_pos_short`），可由 CLI 覆寫。

### 步驟 7 — `src/orderbook.py`（五檔收集與落地）

- `BookBuffer` — `collections.deque(maxlen=1200)`。五檔 callback（Rust 執行緒）**只做一件事**：把 dict append 進 deque，絕不碰 DB 或其他共享狀態。
- `start(api, contract)` / `stop(api, contract)` — `set_on_bidask_fop_v1_callback` + `subscribe(QuoteType.BidAsk, version=QuoteVersion.v1)`。**訂閱是常駐的**（不隨進出箱開關），因為進箱瞬間要能回寫前 30 筆作為前文；只有**落地**才受 visit 控制。
- `drain_since(event_ts)` → 取出 `event_ts` 之後的更新，依 `--orderbook-max-hz` 做時間分桶降頻（每桶留最後一筆），`0` 表示不降頻全存。
- `snapshot_fallback(api, contract)` — 若步驟 0 判定五檔不可用，改用 `api.snapshots([contract])` 組一列 `source='snapshot', levels=1`，每分鐘一列。訊息與儀表板都要標示「僅一檔」，否則日後分析會誤判資料品質。
- `simtrade=True`（試撮）照樣落地但標記，方便日後過濾。

**DB 成長估算**（步驟 0 第 5 項量到實際 Hz 後據此調整）：五檔列約 300 bytes。
`--orderbook-max-hz 5` → 1,500 B/s → 一次 30 分鐘的 visit ≈ **2.7 MB**；不降頻（假設 15 Hz）≈ 8 MB。預設 **5 Hz**，在微結構細節與檔案大小間取平衡。

### 步驟 8 — `src/telegram_notify.py`

沿用 `src/telegram_sample.py:25-39` 的寫法（`requests.post` 到 `https://api.telegram.org/bot<TOKEN>/sendMessage`、`parse_mode="HTML"`、`timeout=10`、token/chat_id 從 `.env`）。

- `send_message(text) -> bool` — 缺 token 時印警告並回 `False`（通知失敗不可以弄掛 monitor）。失敗重試 2 次（指數退避），結果寫 `telegram_alerts.ok`。
- `format_entry_alert(...)` — 進箱即時通知：合約、時間、1 分 K 收盤、箱體類型（🟢支撐 / 🔴阻力）、上下緣、`state`、`zone_pct`、小時 ATR、五檔是否可用。
- `format_hourly_alert(...)` — 仍在箱內的每小時提醒：加上本次 visit 已停留分鐘數、`min/max_zone_pct`、已累積的委託簿筆數、已命中的反轉型態清單。
- `format_reversal_alert(...)` — 反轉型態命中：型態名稱與量化細節（影線/實體比、`close_pos` 等）。

### 步驟 9 — `src/monitor.py`（核心迴圈）

```bash
conda run -n tss python src/monitor.py --code TMFR1 --backfill-days 60
```

旗標：`--code`（預設 `TMFR1`）、`--backfill-days`（預設 60）、`--poll-seconds`（預設 60）、`--db`、`--once`、`--no-telegram`、`--no-orderbook`、`--orderbook-max-hz`（預設 5）、`--exit-buffer-atr`（預設 0），以及 `PatternConfig` 的各門檻。

**啟動**：`init_schema` → `minute_bars` 為空則回補 `--backfill-days` 天 kbars（切 ≤ 7 天視窗依序請求，每塊重試 3 次）→ 從 `db.get_open_visit()` 還原兩側 `BoxWatcher` 狀態 → 若步驟 0 確認五檔可用則 `orderbook.start()` 常駐訂閱。

**每 60 秒一輪**（`is_trading_time()` 為 False 時只睡覺）：

1. `api.kbars(contract, start=今天−1, end=今天)` → upsert `minute_bars`。（重抓前一天：夜盤跨午夜，且吸收資料修訂。）
2. `minute_bars_to_hourly` → upsert `hourly_bars`，最後一根標 `bar_closed=0`。
3. `collect_snr_boxes(hourly, creation_limit=len(closed)-1)` → `replace_boxes`；取兩側 `current_box`。
4. 取最新 1 分 K，兩側各跑 `BoxWatcher.step()`：
   - **`enter`** → `open_visit` → `orderbook.drain_since(entry − 30 筆)` 以 `phase='pre_entry'` / `'in_box'` 落地 → `record_alert(kind='entry')`，插入成功才發 Telegram。
   - **`stay`** → `orderbook.drain_since(上次落地位置)` 以 `phase='in_box'` 落地 → 更新 visit 統計 → 若 `hour_start` 變了則 `record_alert(kind='hourly')`，插入成功才發。
   - **`exit`** → `close_visit(exit_reason)` → 最後 flush 一次委託簿 → 停止落地（訂閱保持）。
5. **`inside` 期間**跑 `patterns` registry（未完成小時棒 + 前一根已收棒，方向須對應箱體側）：命中 → `insert_reversal` → 當下委託簿以 `phase='at_reversal'` 落地並回填 `reversal_id` → `record_alert(kind='reversal')`。
6. 例外一律記 log 後繼續下一輪（單輪失敗不可以弄死常駐行程）。收到 SIGINT/SIGTERM → `close_visit(exit_reason='shutdown')`、flush、`orderbook.stop()`、logout。

**為什麼每分鐘重抓整天 kbars**：`api.kbars` 的 `start`/`end` 只到**日**粒度，無法只要「最近 5 分鐘」。步驟 0 第 6 項會量出實際配額成本；若太高，改用 `api.ticks(contract, date=今天, query_type=LastCount, last_cnt=500)` 自行聚合分鐘棒（`--source ticks` 旗標）。**這是計畫中最可能需要調整的一點。**

### 步驟 10 — `src/dashboard.py` + `src/web/`（需求 4）

FastAPI，**唯讀 DB**，不碰 shioaji。

- `GET /` → `src/web/index.html`
- `GET /static/*` → `StaticFiles`（含首次啟動時由 `plotly.offline.get_plotlyjs()` 產生的 `plotly.min.js`）
- `GET /api/figure?code=TMFR1&days=30` → 從 DB 讀 `hourly_bars` + `snr_boxes`，用 `snr_core.build_figure` 產圖後回 `fig.to_dict()`；反轉型態以三角標記疊加
- `GET /api/state?code=TMFR1` → `{last_price, last_update, hour_start, boxes[], open_visits[], recent_visits[], recent_reversals[], recent_alerts[], orderbook_rows_today, book_source}`

前端 `app.js`：`setInterval(60_000)` 兩個 fetch → `Plotly.react('#chart', fig.data, fig.layout)` 重畫主圖，DOM 表格渲染箱體表、進行中的 visit（高亮）、最近 visit、最近反轉事件、最近通知。頂部狀態列顯示現價、所在箱體與 `zone_pct`、委託簿來源（五檔/一檔）、資料最後更新時間（超過 3 分鐘未更新標紅 → monitor 可能掛了）。

一分鐘一次的 `Plotly.react` 對 ~760 根棒完全無感，不需任何增量更新技巧 —— 這是簡化版最大的好處。

```bash
conda run -n tss python src/dashboard.py --port 8765
```

### 步驟 11 — `src/snr_1_hour_chart.py` 改寫

現狀是無法執行的死碼。把可重用核心搬進 `snr_core.py` / `session_bars.py` 後，**改寫成以 DB 為來源的靜態 HTML 繪圖 CLI**（檔名保留，不留死檔）：

```bash
conda run -n tss python src/snr_1_hour_chart.py --code TMFR1 --start-date 2026-09-01 --end-date 2026-10-07 --boxes-csv out/boxes.csv
```

保留 `--lookback/--volume-length/--atr-length/--box-width/--boxes-csv`。原始的 TAIFEX-tick / BTCUSDT 版本在 sibling repo 仍有逐位元複本（`snr_strategy/src/pine_snr_boxes_1h_chart.py`），需要時可對照。

---

## 測試

`conda run -n tss python -m pytest src -q`

- **`test_snr_core.py`** — `pine_rma` 對手算 Wilder 序列；`pine_atr`；`delta_volume` 在 doji 棒維持方向；一組手工 60 根棒必須產生**恰好一個**支撐箱、`top`/`bottom` 為已知值；`creation_limit` 壓掉最後一根建箱但仍觸發交叉；`zone_pct`、`bar_in_box`（恰好相切算在內）、`current_box`（跳過 broken、取 box_id 最大）。
- **`test_session_bars.py`** — `session_fields` 邊界：`08:45:00`、`13:44:59`、`13:45:00`（丟）、`14:59:59`（丟）、`15:00:00`、`04:59:59`、`05:00:00`（丟）；跨午夜夜盤歸入同一 `session_id`；合成完整一天的 1 分 K → `minute_bars_to_hourly` 得到 **19 根**且 OHLCV 正確。
- **`test_box_watch.py`** — 一串手工 1 分 K 走「箱外 → 進箱 → 箱內停留 → 出箱」必須得到恰好一組 `enter`/`exit`；箱緣反覆抖動在 `exit_buffer_atr=0.1` 下不應產生多個 visit；箱體換 `box_id` 要先 `exit` 再 `enter`；`box` 變 `None` 要發 `exit(reason="box_broken")`。
- **`test_patterns.py`** — 每個型態各一組「必中」與一組「差一點不該中」的手工棒；`min_range_atr` 濾網擋掉小棒；方向與箱體側不符時不計入。
- **`test_db.py`** — `minute_bars` 重複 `minute_start` 以新值覆蓋；`record_alert` 第二次同 `(code, hour_start, box_type, kind)` 回報未插入（去重生效）；`insert_orderbook_rows` 重複 `event_ts` 只插一次；`open_visit`/`close_visit`/`get_open_visit` 的生命週期正確。

## 端到端驗證順序

1. `python src/probe_history.py --code TMFR1` → kbars `ts` 標記、`api.ticks` 可回溯天數、一檔欄位是否有值、即時五檔是否可用與每秒筆數、kbars 配額成本 → 寫 `doc/probe_findings.md`。
2. `python src/monitor.py --code TMFR1 --backfill-days 60 --once --no-telegram --no-orderbook` → 檢查 `minute_bars` 日期連續性、`hourly_bars` 每日 19 根、`snr_boxes` 數量合理。
3. `python src/snr_1_hour_chart.py --code TMFR1` → 用 TradingView 的同一指標目視對照箱體位置。
4. `python src/telegram_notify.py --message "test"` → 確認頻道收得到。
5. `python src/dashboard.py --port 8765` → 開 `http://127.0.0.1:8765`，確認圖、箱體表、狀態列正常且一分鐘後自己更新。
6. 盤中（夜盤 15:00 後最方便）跑 `python src/monitor.py --code TMFR1` 常駐 → 觀察 `box_visits` / `orderbook_snapshots` / `reversal_events` / `telegram_alerts` 是否如預期累積，並確認進箱當下真的收到 Telegram。

## 風險與未知（步驟 0 負責確認）

1. **歷史五檔不存在** —— 已確認（shioaji Data API 只有 11 個方法，無深度端點）。五檔只能從現在起累積；歷史分析只能用 `api.ticks` 的**一檔**。這是資料上的硬限制，不是實作選擇。
2. **`SHIOAJI_SIMULATION=1` 是否推送即時行情** —— 未知。若不行，需求 2 的五檔會降級成 `api.snapshots()` 的一檔（每分鐘一列）；要五檔就得 `SIMULATION=0` 且帳號有 API 行情權限。這是最關鍵的未知，因為它直接決定需求 2 的資料品質。
3. **kbars `ts` 是開盤還是收盤分鐘** —— 未知。判斷錯會整體位移 1 分鐘，最糟把 13:45 的棒丟進錯誤時段、或讓 08:45 第一根被丟棄。
4. **每分鐘重抓整天 kbars 的配額成本** —— 未知。`api.kbars` 的 `start`/`end` 只到日粒度，無法只要最近幾分鐘。量測後若太貴改走 `--source ticks`。
5. **`api.ticks` 可回溯天數** —— 未知，直接影響一檔委託簿的回溯分析能做多久。
6. **委託簿 DB 成長** —— 取決於實際更新頻率。預設 `--orderbook-max-hz 5`（約 2.7 MB / 30 分鐘 visit）。步驟 0 量到真實 Hz 後再定；若要做細緻微結構分析可設 `0` 不降頻，但要留意檔案大小。
7. **1 分 K 的進箱判定有最多 1 分鐘延遲** —— 低頻輪詢的必然結果。價格插進箱體又馬上彈出若發生在同一分鐘內，仍會被 `bar.low/high` 的區間判定抓到（這是用區間而非收盤價的原因），但委託簿的「進箱前文」只能靠 deque 的前 30 筆回補。
8. **未完成小時棒的型態會變** —— 設計上接受：`reversal_events` append-only 並記 `bar_closed`/`minutes_into_bar`，事後可分析暫定型態最後有沒有成形。
9. **從 kbars 算出的箱體不會與 sibling repo 用 TAIFEX 逐筆檔算出的完全一致**（棒聚合邊界與 kbars 資料修訂的差異）。成交量「單邊口數 vs TAIFEX 的 B+S 雙邊」的倍數差**不影響**建箱判斷（`delta > rolling_max(delta/2.5, 2)` 是相對比較，等比例縮放兩邊同步），但步驟 2 仍應對比一天的輸出把差異量化。
10. 本系統**只做監控與通知，不下任何單**。

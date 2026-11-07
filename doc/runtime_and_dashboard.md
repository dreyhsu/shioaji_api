# 設定檔與一鍵啟動

## macOS

雙擊專案根目錄的 `start_all.command`，或在 Terminal 執行：

```bash
./start_all.sh
```

會自動尋找 conda、啟用 `tss` 後直接執行 Python 啟動器並開啟瀏覽器，避免長時間執行的 `conda run` 攔截 Ctrl+C。缺少 conda／環境會明確報錯，可用 `CONDA_EXE` 指向 conda 的完整路徑。若不需要自動開瀏覽器：`./start_all.sh --no-browser`。

## Windows

雙擊專案根目錄的 `start_all.bat`，或在 PowerShell 執行 `./start_all.bat`（Command Prompt 使用 `start_all.bat`）。會自動尋找 conda、啟用 `tss` 並啟動共用 Python 啟動器；沿用 `config/runtime.json` 的模式，不需手動啟動各服務。可傳入相同參數，例如 `./start_all.bat --no-browser`。

找不到 conda 時，從 Anaconda／Miniforge Prompt 執行，或將 `CONDA_EXE` 設為 `conda.exe` 的完整路徑。啟動失敗會保留視窗顯示錯誤。保持啟動視窗開啟；按 Ctrl+C 後等待服務正常停止。

也可手動執行：

```text
conda activate tss
python -X utf8 -u src/service_launcher.py
```

本次 macOS 離線驗收不代表已完成 Windows 原生驗收。

## 模式唯一來源

修改 `config/runtime.json` 的 `mode`，然後停止並重新啟動。預設為 `mock`，不再接受 `--mock`／`--mode`：

JSON 不支援一般註解；`mode_comment` 為純說明欄位，不影響模式選擇。正式模式的值是 `production`，不是 `prd`。macOS 正式啟動前，將 `mode` 設為 `production`，在 `contracts` 保留 `signal_code` 並加入實際交易月份合約，設定本機券商憑證與 CA，並將兩個 DB 路徑指向正式模式專用資料庫（不可沿用已綁定 simulation 的 DB）；再執行 `./start_all.sh` 或雙擊 `start_all.command`。

| mode | 執行內容 |
|---|---|
| `mock` | 僅 Dashboard 內嵌離線回放與假券商；每次建立新暫存 DB，初始暫停，不讀券商憑證 |
| `simulation` | 券商模擬環境；兩個 fetcher、每商品 monitor、trade、Dashboard |
| `production` | 正式環境；相同分工，trade 須有 CA 設定 |

`simulation` 仍會登入券商。切離 Mock 前，`contracts` 必須同時包含 `signal_code` 與實際月份合約，不能以連續合約取代月份價格。兩個 fetcher 各共用一個 session、多個 monitor 不登入，trade 另用一個 session；這是配置，不是實際用量查詢。

runtime 設定包含商品、acquisition／unified DB、trade 位址，以及行情、健康、交易、Dashboard、Mock 設定檔路徑。相對路徑一律以 runtime JSON 所在目錄解析，不受執行目錄影響。舊的非模式診斷參數仍可單獨使用；一般使用啟動器即可。可用 `--runtime-config /absolute/path/runtime.json` 選擇另一份設定。

啟動器會保存本次模式／路徑與 Dashboard 設定的快照，所有子程序讀同一份快照。執行中修改原 JSON 不會讓不同程序混用模式。直接啟動 fetcher／monitor／trade／watchdog 時若模式為 Mock，會提示使用整體啟動器後結束。

## 程序與日誌

非 Mock 依序啟動 tick → kbar → 每個商品的 monitor → trade → Dashboard。啟動前驗證設定與 port；各步驟檢查屬於本次 PID 的健康檔或 `/api/ready`，不將「程序已啟動」當成「資料已驗證可交易」。日曆、資料完整性與委託／部位核對閘門維持原規則。

每次日誌位於統一 DB 旁 `logs/launcher/<時間與識別碼>/`，包含各服務 `.log`、本次設定快照與 `run.json`。原 fetcher／monitor／trade 的健康檔與 session 稽核仍保留。啟動器以獨占鎖序列化「檢查舊程序 → 停止 → 啟動」，避免同時啟動兩份。

### 啟動前停止舊程序（2026-11-02 更新）

`start_all.sh`／`.command` 與 Windows Python 入口共用此檢查。啟動器讀取設定中 acquisition／unified DB 旁的 owner 紀錄與程序鎖。發現仍在執行的舊券商服務時，要求原啟動器及服務正常停止，由原本持有 Shioaji API 的程序嘗試 `logout()`；等待舊程序釋放鎖後啟動新服務。排程外待機的舊券商服務也須停止。僅有 Mock 執行時仍拒絕重複啟動，保留原 Mock。

停止要求含原程序的 instance 識別碼，不根據 PID 搜尋或終止其他 Python 程序。最多等待 45 秒；舊程序仍持有鎖才停止新啟動。登出結果只作稽核，不再要求登出成功確認；已結束程序留下的登入中／登出未確認紀錄，以及舊事件日誌，都不阻擋重啟。各服務後續登入也不再被這項確認狀態鎖住，仍遵守原排程、重試與交易核對規則。原始日誌保留，不補造登出成功。

這個流程不會為了檢查而登入券商。若舊程序已有 API，登出前可記錄 `usage().connections` 為 `connections_before_logout`；該數字是查詢當時的用量，不代表清理後為零。Shioaji 的 `logout()` 只釋放該 API 的 session，不能由新 API 代登出其他電腦、未受管理程序或已失去原 API 物件的連線。不同設定使用不同 DB 路徑時，也須在原啟動視窗停止原服務；啟動器不宣稱能列出或清除全域 session。

按 Ctrl+C 或遇到啟動／程序失敗時，只停止本次建立的程序組，不搜尋其他 Python 程序。先優雅關閉，最多等待 45 秒；仍未結束者才發出強制終止要求。`run.json` 的 `forced_pids`／`logout_unconfirmed` 記錄未確認登出；不能據此宣稱券商 session 已釋放，也不會自動重啟。

## Monitor 歷史處理效能（2026-11-06）

monitor 維持每秒同步最新報價、檢查 tick 健康／instance／generation 與行情時效。acquisition DB 新增每商品、每模式的 `market_bar_versions` 與資料庫識別碼；K 棒新增、修正、完整性失效或刪除才推進版本，單純報價更新與相同資料重寫不推進版本。

啟動時讀取完整來源範圍；之後僅在 K 棒版本改變時重讀該範圍，以保留任意較舊分鐘的 broker 修正及 tick 撤回。寫入 unified DB 時只處理新增／內容或來源改變的分鐘，撤回失效的 tick 候選；未變動的歷史不再每秒重新讀寫。已快取但尚未收盤的分鐘仍會在收盤時重新篩選。重啟會重新建立快取，不以最後一根分鐘的時間當作修正水位。

缺棒驗證的歷史集合只在資料、日曆、收定分鐘或延遲容忍邊界改變時重算；lag、開盤寬限與 ready 每輪依目前時間重算。未解釋缺棒、過舊行情、tick 世代變更仍依原規則關閉閘門，broker 優先與 `pending_repair` 規則不變。指標仍使用原有完整計算範圍，不縮短 ATR／曲線幾何的歷史。

更新後正常停止並重新執行 `start_all.bat`（macOS 為 `start_all.sh`／`.command`），由 fetcher 初始化來源版本表與觸發器；monitor 仍僅唯讀來源 DB。若來源尚未升級，monitor 會回退完整來源讀取以保留正確性。這是離線回歸驗證的效能調整；實際 CPU、溫度與風扇改善須在執行正式行情時另行量測。

Windows `tss` 的測試網路隔離另補上 asyncio 內部 socketpair 支援；一般 socket、HTTP 與真券商連線仍封鎖。額外 API 檢查有三項既有 trade 測試失敗（斷線時指令處理、快照時效、背景失敗顯示）；以優化前的 `market_health.py`／`fetcher_storage.py` 在記憶體載入重測亦同樣失敗，未於本次修改 trade 行為。2,880 根合成 K 棒的十次輪詢測試中，完整驗證平均約 68 ms／次，暖快取約 0.25 ms／次；此數字僅代表驗證函式，不代表整套服務或實際行情的效能。

## Dashboard 設定

`config/dashboard.json` 預設：

| 欄位 | 預設 | 用途 |
|---|---|---|
| `host`／`port` | `127.0.0.1`／`8765` | 網頁監聽位置 |
| `default_timeframe` | `1h` | 初次開啟的 K 棒週期 |
| `history_days` | `30` | 回溯日曆天數 |
| `initial_bars` | `120` | 初始與「最近」按鈕的可見根數 |
| `refresh_seconds` | `60` | 一般圖表刷新間隔；Mock 另沿用回放進度通知 |
| `cache_seconds` | `5` | 後端快取上限 |
| `curve_hover_pixels` | `8` | 游標命中曲線的距離 |

修改後需重新啟動。網址明確指定的 `timeframe`、`days` 優先於預設。`GET /api/dashboard-config` 只回傳顯示設定，不包含檔案路徑、券商憑證或 trade 位址。

1h 圖優先讀取 monitor 已完成且來源版本一致的小時資料；分鐘價格被修正、插入或刪除時會使舊衍生版本失效。狀態摘要只查最新分鐘與筆數，小週期圖或小週期曲線才載入分鐘資料。舊 DB、Mock 或尚未完成完整衍生資料的情況會退回原來的分鐘重建，避免改變 ATR、delta volume、控制點幾何與時間格線。監控資料只涵蓋部分歷史時也不冒充完整快取。

資料庫會新增版本／衍生水位的輔助表與觸發器，不改寫既有分鐘價格或交易紀錄。曲線修改和 Mock 回放／重設同樣會使畫面快取失效。

## 曲線與名稱提示

### 自動曲線

預設 `config/mock.json` 為 15m 自動曲線測試提供可辨識的波浪行情：每段基準趨勢使用 `trend_curvature=0.65` 的二次曲線，疊加振幅 100 點、約 94 個交易分鐘（約 6 根 15m 棒）一週期的波動，上升及下降均有局部高低點。回放初始暫停於第 4020 根分鐘棒，切換 15m 後最近 120 根可同時看到下跌與反彈。修改設定後須重啟 Mock Dashboard；頁面「重設情境」沿用啟動時載入的設定。`trend_curvature=0` 保留原線性基準，預設模型及既有固定測試情境仍使用原設定。

1m／5m／15m／1h 的「自動曲線」工具採兩次點擊：點起點後移動滑鼠顯示半透明紅框，再點終點。紅框同時限制時間與價格；只使用已收盤的來源週期 K 棒，排除當前未收盤棒。框內相交 K 棒的收盤價回歸決定上升／下降，上升擬合框內局部低點、下降擬合局部高點。轉折以兩側各兩根判斷，等價平台合併；三個以上轉折以二次 Bézier 最小平方法擬合，兩個轉折使用直線擬合但仍存為曲線。允許少數影線穿越；不足三根已收盤棒、兩個轉折點或趨勢平坦時保留紅框，可移動後再點終點重試。

自動擬合另限制方向及彎曲：上升段只能向上彎或為直線，下降段只能向下彎或為直線；整段曲線與末端延伸均不能反向。二次擬合不符合起迄切線及曲率條件時，改用同組高／低點的方向限制最小平方直線；若轉折點的直線斜率與收盤價趨勢相反，使用水平線。此限制僅適用新產生的自動曲線，不重寫已保存曲線或限制手動畫線。

生成後為未儲存草稿，可拖曳曲線整體平移；水平以來源 K 棒為單位，三個控制點均限制在已載入歷史內。按「儲存曲線」或 Enter 才寫入共用 DB；「取消」或 Esc 放棄，切換週期也會取消。儲存失敗保留草稿。既有已儲存曲線不新增拖曳編輯功能，儲存也不啟用交易。選取／調整期間固定圖表資料快照，帳戶及健康狀態仍可更新；結束後恢復圖表刷新。

曲線末端沿原切線延伸，圖上額外以虛線提示接下來八根圖表 K 棒方向，不建立假 K 棒或未來時間戳。草稿顯示「參考 Reference ±角度° · ±點數/來源週期 candle」；儲存後 hover 顯示相同資訊與未來方向。參考角採生成時的價格／K 棒畫面比例並保存在可選欄位 `angle_reference_points_per_bar`，斜率以來源週期每根 K 棒的價格點數表示。縮放、平移與草稿整體移動不改變數字；畫面上看見的角度仍會隨縮放改變。舊曲線此欄位為 null，維持原有提示。

離線前端驗證：`node tools/test_auto_curve.cjs`（擬合／幾何／34,200 根分鐘棒效能）、`node tools/test_auto_curve_ui.cjs`（選取、拖曳、取消、縮放、儲存失敗與刷新隔離）；Python 與瀏覽器幾何一致性、API 往返與資料庫遷移由既有對應 pytest 模組驗證。Windows 原生滑鼠驗收需在 Windows laptop 執行。

自動曲線與手動曲線共用 Telegram 接近／突破提醒、突破進場及逐單曲線停利流程；畫線或儲存不等於啟用進場。Mock 僅驗證提醒規則與模擬成交，不傳送 Telegram。

### 曲線名稱提示

1m／5m／15m／1h 圖均可畫三點曲線，保存時記錄來源週期；切換圖表時仍以該來源週期投影幾何與 hover。各週期曲線皆可選作一次突破進場條件或每單的停利方式；交易仍以相鄰收定 1 分 K 穿越判斷，不等待來源週期收盤。停利選單、委託狀態及確認訊息顯示來源週期。直線仍限 1h，固定風控預設仍使用 1h ATR；既有委託不會自動換成其他曲線。舊資料庫首次以新版初始化時會原子遷移 `trendlines` 的週期限制，保留 ID、控制點、索引、觸發器與 AUTOINCREMENT 水位。

游標靠近已儲存曲線，顯示名稱、來源週期與 ID，例如 `突破曲線 · [1h] · #12`；沒有名稱時為 `曲線 · [1h] · #12`。重疊曲線選最近者，同距離選 ID 較小者。名稱以純文字顯示。

Telegram 的曲線接近／突破提醒支援 1m／5m／15m／1h，以曲線保存的來源週期計算最新來源 K 棒的線值及斜率，仍於 1 分 K 收定時檢查，不等待 5m／15m／1h 收盤。訊息標示來源週期及對應斜率單位。沿用既有提醒條件（上升線下方／下降線上方），每條線每種突破規則僅通知一次；接近提醒沿用冷卻時間與 1h ATR 設定。提醒與自動進場啟用分開，受曲線保存狀態、Telegram 設定及 monitor 健康閘門控制。更新後須重啟 monitor 才會載入新程式。

離開圖表、切換週期、刪除曲線、縮放／平移或開始畫線／拖曳時，會隱藏舊提示。曲線使用與圖表一致的取樣線段做距離判斷，不改變下單或進出場條件。

## 本次驗收（2026-11-01）

- 2026-11-02 更新：移除登出確認閘門；離線測試改驗證未確認紀錄不阻擋啟動／後續登入、舊程序仍執行時等待或拒絕重複啟動，以及保留真實登出失敗日誌。
- macOS 實際以隔離暫存 DB 啟動 Mock，驗證 instance 停止要求由啟動器傳給 Dashboard，兩者正常結束並釋放鎖及 port；`forced_pids`、`pending_pids` 皆為空，驗收服務已停止。此測試不登入券商，未驗證券商端實際連線數。
- conda `tss` 的設定／啟動器、小時圖、Dashboard、trade、fetcher、monitor、資料庫、健康檢查與既有交易相關測試共 **387 項通過**（21.49 秒）；這是相關測試組合，未宣稱全套通過。
- `node tools/test_curve_hover.cjs`：**8 項**命中幾何測試通過，涵蓋重疊曲線、距離門檻、空資料、平移／縮放與縮小後端點附近命中。
- Python、JavaScript、shell 語法與 diff 空白檢查通過；含空白的專案／conda 路徑經假執行檔測試。
- macOS 已實際以預設 Mock 啟動，確認僅 Dashboard、初始暫停、預設 1h；實際畫曲線後 hover 顯示週期／ID，切換 5m 會隱藏舊提示。
- 實測重複啟動遭拒，原服務仍正常。改用啟用 conda 後直接執行 Python 的方式後，Ctrl+C 正常結束，`run.json` 記錄 `stopped`、`forced_pids: []`。驗收服務已停止。
- 未登入券商、未送單或正式部署；Windows 僅保留相容設計與替身測試，尚未原生驗收。


行情狀態採上一輪完成的健康驗證，例行更新指標不暫時切成 `checking`／未就緒；工作逾時、已完成結果過舊或最新 K 過舊時仍會關閉閘門。健康檔的 `operation` 與 `cycle_completed_at` 可用於區分更新中和工作卡住。

### 跳空缺口與 Telegram

圖表以灰色半透明矩形顯示 **1h 跳空缺口**，切換 1m／5m／15m 時仍顯示同一組 1h 缺口，與 SNR box 的來源週期一致。相鄰兩根完整、已收定的 1h K 棒，高低價範圍完全不重疊時形成缺口（向上：前高至後低；向下：後高至前低）。僅開盤價跳動但影線重疊不算缺口；同盤中間缺棒或不足 60 根分鐘資料不建立缺口。

灰框從缺口形成的 K 棒向右延伸，保留原始上下界；後續已收定 1h K 棒的影線首次碰到近端視為進入，碰到遠端視為完全回補。完全回補後，灰框停在回補 K 棒，不再延伸。灰框不可拖曳，也不影響價格軸自動縮放或交易規則。

monitor 透過既有 Telegram 流程發出 `gap_formed`（形成）、`gap_entered`（首次進入）、`gap_filled`（完全回補），各缺口各事件只提醒一次，紀錄存於 `telegram_alerts`，重啟不重送；提醒包含商品、方向、價格區間與台北確認時間。三項均可在 `config/alerts.json` 單獨開關，並遵守 `telegram_enabled`、`--no-telegram` 與 monitor 行情健康閘門。只提醒當前收定確認，不補發啟動前的歷史事件。Dashboard 不發送訊息，關閉瀏覽器不影響 monitor 提醒。Mock 不啟動 monitor，因此不發送這些提醒。

排查顯示時先檢查 `/api/chart?code=TMFR1&days=30&timeframe=1h` 的 `gaps`：若已有對應 `box_id` 且 `open: true`，代表缺口已計算並送往前端；再檢查瀏覽器腳本快取、可視價格範圍及灰框是否被其他圖層遮蓋。2026-11-02 唯讀核對：10/30 08:45 向下缺口為 45,579–45,856，09:45 收定確認，API 有回傳未回補灰框；`telegram_alerts` 無 gap 紀錄。今日歷史回補不會補發該歷史事件，亦不能僅憑今日日誌推定 10/30 當時的通知執行狀況。前端 gap 相關腳本已更新版本參數，重新載入頁面可取得最新版。

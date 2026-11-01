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

每次日誌位於統一 DB 旁 `logs/launcher/<時間與識別碼>/`，包含各服務 `.log`、本次設定快照與 `run.json`。原 fetcher／monitor／trade 的健康檔與 session 稽核仍保留。啟動器以獨占鎖序列化「檢查舊連線 → 清理 → 啟動」，避免同時啟動兩份。

### 啟動前先登出舊 session

`start_all.sh`／`.command` 與 Windows Python 入口共用此檢查。啟動器先讀取設定中 acquisition／unified DB 旁的 `.health/<DB名>/*.owner.json` 與程序鎖。發現本機本專案的舊券商服務時，會要求原啟動器及服務正常停止，由原本持有 Shioaji API 的程序呼叫 `logout()`；確認舊服務停止、券商 owner 留下登出成功（或從未登入）紀錄後，才啟動新服務。排程外待機的舊券商服務也須停止，避免稍後再次登入。僅有 Mock 執行時仍拒絕重複啟動，保留原 Mock。

停止要求含原程序的 instance 識別碼，不根據 PID 搜尋或終止其他 Python 程序。最多等待 45 秒；逾時或登出失敗就停止新啟動，保留舊程序，不以強制終止當作登出成功。若程序已崩潰但紀錄仍是登入中／登出未確認，或舊版服務無法回覆確認，畫面會提示在原視窗 Ctrl+C 或人工核對。不可刪除未確認的 owner 紀錄來繞過此檢查。

這個流程不會為了檢查而登入券商。若舊程序已有 API，登出前可記錄 `usage().connections` 為 `connections_before_logout`；該數字是查詢當時的用量，不代表清理後為零。Shioaji 的 `logout()` 只釋放該 API 的 session，不能由新 API 代登出其他電腦、未受管理程序或已失去原 API 物件的連線。不同設定使用不同 DB 路徑時，也須在原啟動視窗停止原服務；啟動器不宣稱能列出或清除全域 session。

按 Ctrl+C 或遇到啟動／程序失敗時，只停止本次建立的程序組，不搜尋其他 Python 程序。先優雅關閉，最多等待 45 秒；仍未結束者才發出強制終止要求。`run.json` 的 `forced_pids`／`logout_unconfirmed` 記錄未確認登出；不能據此宣稱券商 session 已釋放，也不會自動重啟。

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

## 曲線名稱提示

游標靠近已儲存曲線，顯示名稱、來源週期與 ID，例如 `突破曲線 · [1h] · #12`；沒有名稱時為 `曲線 · [1h] · #12`。重疊曲線選最近者，同距離選 ID 較小者。名稱以純文字顯示。

離開圖表、切換週期、刪除曲線、縮放／平移或開始畫線／拖曳時，會隱藏舊提示。曲線使用與圖表一致的取樣線段做距離判斷，不改變下單或進出場條件。

## 本次驗收（2026-11-01）

- 啟動前 session 清理新增驗證：相關離線回歸 **402 項通過**，另加測同一 owner 登出未確認後不得重登。涵蓋先登出再啟動、崩潰／逾時／登出失敗阻擋、舊版紀錄、instance 過期要求隔離、既有 API 用量查詢與清理時不強制終止。
- macOS 實際以隔離暫存 DB 啟動 Mock，驗證 instance 停止要求由啟動器傳給 Dashboard，兩者正常結束並釋放鎖及 port；`forced_pids`、`pending_pids` 皆為空，驗收服務已停止。此測試不登入券商，未驗證券商端實際連線數。
- conda `tss` 的設定／啟動器、小時圖、Dashboard、trade、fetcher、monitor、資料庫、健康檢查與既有交易相關測試共 **387 項通過**（21.49 秒）；這是相關測試組合，未宣稱全套通過。
- `node tools/test_curve_hover.cjs`：**8 項**命中幾何測試通過，涵蓋重疊曲線、距離門檻、空資料、平移／縮放與縮小後端點附近命中。
- Python、JavaScript、shell 語法與 diff 空白檢查通過；含空白的專案／conda 路徑經假執行檔測試。
- macOS 已實際以預設 Mock 啟動，確認僅 Dashboard、初始暫停、預設 1h；實際畫曲線後 hover 顯示週期／ID，切換 5m 會隱藏舊提示。
- 實測重複啟動遭拒，原服務仍正常。改用啟用 conda 後直接執行 Python 的方式後，Ctrl+C 正常結束，`run.json` 記錄 `stopped`、`forced_pids: []`。驗收服務已停止。
- 未登入券商、未送單或正式部署；Windows 僅保留相容設計與替身測試，尚未原生驗收。

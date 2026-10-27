# 模組化與獨立測試規範

每個模組負責一個明確功能，並說明輸入、輸出、驗證規則、例外與外部依賴。
「獨立」指可只執行該模組的測試，不要求每個模組都是可直接啟動的 CLI。
匯入新模組不得登入、讀取私人帳密、送單、發通知或啟動背景執行緒。

## 依賴方向與責任

| 模組 | 輸入 → 輸出 | 責任與限制 |
|---|---|---|
| `place_order` | OrderRequest + OrderContext + BrokerPort → OrderResult | 純驗證、跳動單位與一次送單；不使用 DB、ATR、Dashboard 或 Shioaji |
| `shioaji_order_adapter` | 已連線 API、合約與中立委託 → 券商委託結果 | 建立 SDK 委託並轉換回報；不登入、不監控 |
| `trading` | 使用者委託／持倉操作 → 持久化委託與狀態 | 全模式共用進出場週期、ATR、設定與執行；注入 API、SDK、時鐘、模式與 worker 開關 |
| `entry_notify` | 進場成交口數增加後的委託快照 → Telegram 通知紀錄 | 沿用通知總開關，SQLite 依委託及累計口數去重；背景傳送失敗不影響交易；可注入派送器作離線測試，Mock 停用 |
| `curve_entry` | 已收盤 1m K、1h 曲線錨點、時間 → 是否突破 | 純訊號，不送單；相鄰已收盤 K 由下向上穿越，缺資料或休市間隔不觸發 |
| `curve_entry_controller` | 確認啟用／取消／市場更新 → 一次進場狀態 | 全模式共用；SQLite 保存等待、檢查基準與送單資格；錯誤不重送 |
| `mock_market_data` | 固定情境 → DataFrame | 產生可重現的長跌、V 型反彈與回檔，不存 DB |
| `mock_broker` | 委託與報價 → 成交／等待／取消、帳戶狀態 | 純記憶體撮合，沒有 Shioaji、網路或憑證 |
| `mock_runtime` | 推進／重設 → 回放狀態 | 只組裝行情、撮合、隔離 DB 與時鐘；所有交易決策交給共用服務 |
| `mock_routes` | HTTP 請求 → 回放結果 | 只保留 Mock 回放控制；舊進場路由為相容轉接，共用介面使用 trading 路由 |
| `dashboard` | HTTP → 圖表／交易 API 回應 | 保持既有路由，委派功能模組，不自行定義撮合或訊號規則 |

既有指標、K 棒彙整、箱體與型態模組繼續沿用。本次未全面拆分監控與資料存取。

## 下單介面

`OrderRequest` 使用明確欄位：contract_code、action、quantity、price、price_type、order_type、
simulation、confirmed。`OrderContext` 提供已解析合約識別碼、tick 與實際模式；
`BrokerPort.submit_order(request, context, price)` 接收驗證並調整後的資料，回傳
`OrderResult(trade_id, status, price)`。

`validate_request` 可單獨驗證；無效資料拋出 ValueError，券商呼叫次數必須是零。
`place_order` 最多呼叫券商一次；失敗或沒有委託編號時拋出 OrderSubmissionError
（RuntimeError 子類別），由上層核對狀態，不自動重送。ATR、曲線存在檢查與持久化仍由交易服務協調。

## 測試層級與資料

- 每個新增或修改的重要功能模組都有 `test_<module>.py`。正常、錯誤、邊界與回歸情境都需涵蓋。
- `unit` 測純函式、參數轉換或單一 adapter；外部依賴以 `unittest.mock` 或 Fake 注入。
- `integration` 串接真實模組與暫存 SQLite，以 Mock 券商取代傳輸；不需要啟動整套系統。
- 新測試可明確加上標記；既有 Dashboard、監控、交易與 MockRuntime 測試自動歸為 integration，其他預設 unit。
- 根目錄 conftest 在測試收集前阻擋網路、Shioaji 初始化與 dotenv 載入，移除繼承的敏感憑證；各案例的 DB 僅可放在系統暫存目錄。
- 時間與價格應固定；透過注入時鐘與單次監控重現行為，不以 sleep 等待成交。背景執行緒只在專門驗證生命週期時啟用並於結束清理。
- MockRuntime 測試涵蓋手動委託的 ±2 ATR、手動畫曲線切換停利及出場、突破曲線買入一次、錯誤不重送、取消／刪除曲線與重設。

## 新增功能的步驟

1. 確定單一責任，先寫清楚輸入、輸出、預期行為與錯誤。
2. 分離純計算與外部操作；以參數或 Protocol 注入外部依賴，不依賴 UI 全域狀態。
3. 建立對應 pytest，優先使用預先建立的 K 棒、固定時鐘與 Mock API。
4. 用 `conda run -n tss python -m pytest src/test_<module>.py -q` 單獨驗證。
5. 將模組接入交易服務或其他組裝入口，補上協作測試。
6. 執行完整離線套件；確認既有回傳介面與行為相容。
7. 開發時啟動 `tools/test_runner.py --watch`；需要時自行安裝 `.githooks/pre-commit`。

監看工具沿本地 import 的反向依賴選測試，包含間接依賴。刪除檔案、JSON、前端、
共用設定、無測試映射、解析失敗與無法靜態判讀的動態 import 採完整套件。
連續存檔會合併，測試依序執行；執行期間的變更下一輪再跑。import 圖是保守選擇工具，
提交前完整套件仍是回歸檢查基準。此階段不設定遠端 CI。

use conda env tss

專案需同時支援 macOS 與 Windows。
正式區部署於 Windows laptop。

## Shioaji session 限制與用量

- 同一個 `person_id` 最多同時使用 **5 個連線**；每次 `api.login()` 建立一個連線。不同 API Key、程序或電腦的登入仍須按同一身分合計，不能將上限視為每個服務各有 5 個。
- 目標架構配置為 **3 / 5 個 session**：`tick_fetcher.py` 1 個、`kbar_fetcher.py` 1 個、獨立 `trade.py` 1 個；完成拆分後的 `monitor.py`、`dashboard.py` 不登入券商。
- 目前僅完成兩個獨立 fetcher；monitor／trade／Dashboard 尚未完成遷移。因此 **3 / 5 是架構配置，不是已查核的即時使用量**。若同時執行新 fetcher 與舊 monitor、Dashboard 內嵌交易服務，通常會使用 4 個 session；其他既有程序或電腦仍可能額外占用連線。
- 實際目前用量以已登入 API 的 `api.usage().connections` 查詢；未查詢時不得宣稱實際已使用幾個。檢查用量不要額外登入，避免查詢本身占用新 session。
- 啟動、重連及切換版本前須計入舊程序尚未釋放的連線；使用完畢呼叫 `api.logout()`，避免超過上限。
- 官方來源：[Shioaji 使用限制](https://sinotrade.github.io/zh/tutor/limit/)（2026-10-31 核對）。

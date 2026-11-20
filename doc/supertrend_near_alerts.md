# 1 分 K 收盤接近 1h Supertrend：Telegram 通知

訊號商品 monitor 使用 unified DB 的完整 1 分 K 收盤，對比當時最新已收定的標準 1h Supertrend（ATR 10／倍數 3，與 Dashboard 相同）。不使用形成中的小時 K 或 15m SuperTrend AI。

| 1h 方向 | 接近條件 |
|---|---|
| 空頭 Downtrend | 收盤在線下或碰線，距離不超過門檻，提示接近壓力 |
| 多頭 Uptrend | 收盤在線上或碰線，距離不超過門檻，提示接近支撐 |

距離百分比為 `abs(close - ST) / ST * 100`，沿用先前開盤距離計算。只要求收盤在指定側且接近，不要求這根收盤比上一根更高／低；影線觸及但收盤太遠不提醒，收盤已越線不提醒。每個有效新分鐘都檢查，持續符合時依冷卻時間再次通知。

```json
"supertrend_near": {
  "enabled": true,
  "distance_percent": 0.5,
  "cooldown_minutes": 10
}
```

設定在 `config/alerts.json`；`0.5` 代表 0.5%，不是 50%。ST 為 40,000 時門檻約 200 點。百分比與冷卻時間須為有限非負數；0% 僅碰線通知，冷卻 0 仍每分鐘最多一筆。無效設定保留上一份完整有效設定，透過 `AlertConfig.last_error` 顯示錯誤。設定可熱載入；新增程式需重啟訊號商品 monitor 才生效。

空頭／多頭分開計算冷卻，沿用持久化 `telegram_alerts` claim，重複輪詢、程序重啟不重發同一分鐘。同方向持續接近時，預設每 10 分鐘最多通知一次。訊息包含商品、趨勢、ST、1m 收盤、距離點數／百分比、門檻及指標／分鐘收定時間（Asia/Taipei）。

沿用 monitor 日曆、行情同步及訊號健康閘門；缺失／不完整小時或 ATR 暖機不足不猜測趨勢。只處理分鐘收定後未滿 60 秒的行情，正常同步延遲可接受，過期歷史資料不補發。指標在完整小時版本改動時重算，形成中小時每分鐘的變化不觸發重算。

僅 `runtime.signal_code` 的非 Mock monitor 產生本提醒，避免月份合約重複通知。沿用 `telegram_enabled`、`--no-telegram`、背景傳送與結果紀錄；規則與原有「1h／15m AI 方向相反」及開盤通知分別運作。僅新增觀察通知，不改交易規則或券商連線。

離線驗證：`conda run -n tss python -m pytest src/test_supertrend_near_alerts.py -q`。測試使用合成行情／暫存 DB／注入通知傳輸，不登入券商或發真實 Telegram。正式行情／Telegram 與 macOS 原生驗收另行執行。

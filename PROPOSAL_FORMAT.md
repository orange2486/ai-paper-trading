# 提案格式（`<帳戶>/ledger/proposals/YYYY-MM-DD.json`）

AI 每天讀完簡報後寫一份。風控（`src/papertrade/risk.py`）只檢查硬性條件，不讀文字內容；
文字欄位只檢查「有沒有寫」。提案檔 commit 後不能改。

```json
{
  "date": "2026-10-15",
  "buys": [
    {
      "code": "2330",
      "amount": 200000,
      "technical": "K 線／均線／量能：寫具體看到什麼",
      "institutional": "三大法人：寫具體數字與天數",
      "material_info": "重大訊息：引用簡報裡的公告（代號＋公告時間），沒有就寫「無相關重大訊息」",
      "expected_path": "預期怎麼走、多久內（例如：10 個交易日內站上 2,600）",
      "target_price": 2600,
      "confidence": "中",
      "data_as_of": "2026-10-15"
    }
  ],
  "sells": [
    {"code": "2317", "reason": "為什麼現在賣"}
  ],
  "stop_updates": [
    {"code": "2454", "new_stop": 1450, "why": "只准提高"}
  ],
  "reviews": [
    {"trade_id": "T0003", "category": "時機錯", "analysis": "對照買進時寫的預期，說明哪裡對、哪裡錯"}
  ],
  "no_action_reason": "只有在 buys／sells／stop_updates 都空的時候必填"
}
```

## 風控會擋下的情況

| 條件 | 值 |
|---|---|
| 新買筆數 | 每天最多 1 筆（`buys` 第 2 筆起一律作廢） |
| 今天不能新買 | 漏跑、非準時執行、行情或 PCF 缺、期末 |
| 標的 | 必須在當日元大 PCF 成分股；不能加碼已持有的股票 |
| 金額 | `amount` ≤ 總資產 20%；估計成本 ≤ 可用現金（總曝險 ≤ 100%） |
| 持股數 | 買進後 ≤ 5 檔（同日賣單會先扣掉） |
| 停損 | 不能自訂（有 `stop`／`stop_loss` 欄位就作廢）；成交後自動設為成交價 × 0.92 |
| 必填 | `technical`、`institutional`、`material_info`、`expected_path` 不能空白；`target_price` > 今日收盤；`confidence` 為 低／中／高；`data_as_of` = 當日 |
| 停損調整 | `new_stop` 必須高於現值 |
| 檢討 | `category` 必須是：理由對、理由錯、時機錯、執行問題、運氣、被動出場；每筆只能檢討一次 |

`amount` 以今日收盤價＋1 檔估算股數；實際以 T+1 開盤價＋1 檔成交，現金不夠時自動減少股數。

## 資料限制（PLAN.md 第 5 節）

只能用簡報與 `data/` 裡的資料：價量、元大 PCF、三大法人、公開資訊觀測站重大訊息。
**不准**用搜尋引擎、新聞網站、論壇、社群、融資融券。

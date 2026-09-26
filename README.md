# AI 紙上交易

規則見 `PLAN.md`（v1 鎖定）；AI 提案格式見 `PROPOSAL_FORMAT.md`。只用公開資料，不含 TEJ。

## 每日流程

```bash
# 環境：Python 3.11+，程式只用標準函式庫；測試要 pytest
export PYTHONPATH=src

python -m papertrade prepare  --account live   # 抓資料、結算、強制出場委託、寫簡報
#   → AI 讀 live/ledger/briefings/YYYY-MM-DD.md，寫 live/ledger/proposals/YYYY-MM-DD.json
python -m papertrade finalize --account live   # Python 風控 → 委託
git add -A && git commit -m "live YYYY-MM-DD" && git push
```

第一次：

```bash
python -m papertrade init      --account live --date <第一個決策日>
python -m papertrade bootstrap --account live --until <第一個決策日的前一個交易日>   # 約 70 個交易日歷史，約 7 分鐘
```

試跑用 `--account trial`；重播過去某天可加 `--now 2026-09-24T20:00`（`live` 不准用）。

## 帳戶資料夾

```
<帳戶>/
  lessons.md                 提案前自問的檢查問題
  ledger/state.json          現金、持股、待成交委託、對照組
  ledger/briefings/          每日簡報（給 AI）
  ledger/proposals/          AI 提案原文
  ledger/trades/T0001.json   每筆交易：提案理由、成交、停損紀錄、出場、檢討
  ledger/orders.csv          委託、取消、出場觸發
  ledger/fills.csv           成交、股利
  ledger/daily.csv           每日總資產、曝險、對照組
  ledger/risk_log.csv        風控結果
  ledger/runs.csv            每次執行：是否準時、能否新買、註記
  data/raw/                  所有原始回應（gzip，含網址與抓取時間）
  data/prices/ institutional/ news/   解析後資料
```

## 計畫書沒寫到、程式採用的做法

這些是執行細節，不是新規則；如有疑問以 PLAN.md 為準。

1. **不加碼**：已持有的股票不能再買（一檔一筆交易，停損才有唯一的成交價）。
2. **20% 以決策時估算**：以當日收盤價＋1 檔估股數；隔天開盤跳高時實際金額可能略超過 20%。
   現金不夠時自動減少股數，所以總曝險永遠 ≤ 100%。
3. **除權息**（已寫入 PLAN v1.1）：現金股利除息日入帳；股票股利改股數，成交價與停損等比例換算；
   除權息當天不判斷停損。對照組 0050 同樣處理。明細抓取失敗時當天不判斷停損並提醒人工處理。
4. **分割／面額變更／減資**：收盤對前一日收盤跳動超過 11%（超過漲跌幅限制）且不在除權息表上時，
   當天不判斷停損，並在 `state.json` 的 `alerts` 記錄、每次執行都提醒「需人工處理」。
   簡報的報酬、均線只用跳動之後的資料（除權息造成的跳動也一樣，因為價格未還原）。
5. **漲跌停判斷**：開盤價 ≥ 漲停價不買、≤ 跌停價不賣（近似，實際開盤漲停仍可能成交）。
   強制出場遇跌停：委託保留到下一個交易日；AI 自選賣出遇跌停：取消。
6. **漏跑後的委託**：若執行時已過當天 09:00，委託記在當天，下一個交易日開盤才成交（不回頭用舊價）。
7. **重大訊息時間窗**：上一次執行的截止時間 ~ 本次執行時間；第一次執行從決策日 00:00 起。
8. **對照組 0050**：初始化日之後第一個交易日開盤買進（與 AI 第一筆可能成交的時間相同）。

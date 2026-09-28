# AI 紙上交易

規則見 `PLAN.md`（v1.2 鎖定）；AI 提案格式見 `PROPOSAL_FORMAT.md`；鎖定先驗見 `prompt_prior.md`。
只用公開資料，不含 TEJ。

四本帳（PLAN 第 0 節）：**真帳**（AI 只提案買，出場只有公式）、**影子帳**（同一批，AI 可早賣，只記錄）、
**賣出盤**（第 1 個成交日全成分各 1 萬，AI 可早賣，對照公式基準，只記錄）、**對照組 0050**。

## 每日流程

```bash
# 環境：Python 3.11+，程式只用標準函式庫；測試要 pytest
export PYTHONPATH=src

python -m papertrade prepare  --account live   # 抓資料、結算四本帳、強制出場委託、寫簡報
#   → AI 讀 live/ledger/briefings/YYYY-MM-DD.md，寫 live/ledger/proposals/YYYY-MM-DD.json
#     （Prompt A 真帳買進；Prompt B 影子帳／賣出盤賣不賣）
python -m papertrade finalize --account live   # Python 風控 → 真帳委託；影子帳／賣出盤早賣單
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
  lessons.md                 買進提案前自問的檢查問題
  ledger/state.json          真帳現金、持股、待成交委託、對照組、影子帳、alerts
  ledger/panel.json          賣出盤：公式基準的部位與交易、AI 早賣紀錄
  ledger/briefings/          每日簡報（給 AI）
  ledger/proposals/          AI 提案原文（Prompt A／B）
  ledger/trades/T0001.json   真帳每筆交易：提案理由、成交、停損紀錄、出場、檢討
  ledger/orders.csv          真帳委託、續掛、出場觸發
  ledger/fills.csv           真帳成交、股利、對照組
  ledger/daily.csv           每日真帳權益、曝險、對照組、影子帳／賣出盤累計損益
  ledger/shadow.csv          影子帳：每檔賣／不賣、早賣、follow_real、損益
  ledger/sell_panel.csv      賣出盤：進場、賣／不賣、公式出場、損益
  ledger/risk_log.csv        風控結果（含 Prompt B 每檔是否有寫）
  ledger/runs.csv            每次執行：是否準時、能否新買、註記
  data/raw/                  所有原始回應（gzip，含網址與抓取時間）
  data/prices/ institutional/ news/   解析後資料
```

## 計畫書沒寫到、程式採用的做法

這些是執行細節，不是新規則；如有疑問以 PLAN.md 為準。

1. **20% 以決策時估算**：以當日收盤價＋1 檔估股數；隔天開盤跳高時實際金額可能略超過 20%。
   現金不夠時自動減少股數，所以總曝險永遠 ≤ 100%。
2. **除權息**：現金股利除息日入帳；股票股利改股數，成交價與停損等比例換算；除權息當天不判斷停損。
   對照組 0050、賣出盤同樣處理。明細抓取失敗時記 alert、當天不判斷停損。
   除息日開盤就賣掉的部位不計該次股利（簡化；影子帳、賣出盤同一套）。
3. **公司行動 alert**：收盤對前收跳動超過 11% 且不在除權息表上（分割／面額變更／減資），或除權息明細抓不到，
   記在 `alerts`。**alert 未處理期間不判斷停損**（股數與停損還沒換算，判斷會誤觸發；到期照常）。
   人工處理＝調整 `state.json`（或 `panel.json` 的 `state`）裡的股數與停損，再把該筆 alert 刪掉。
   alert 在第 N 個交易日產生，第 N+3 個交易日結算後仍在 → 下一次開盤整筆出清（`corp_action_unhandled`）。
4. **漲跌停判斷**：開盤價 ≥ 漲停價不買、≤ 跌停價不賣（近似）。所有賣單（真帳強制出場、影子帳／賣出盤早賣）
   遇跌停都續掛到下一個交易日。賣出盤進場日開盤漲停的那檔不買，也不補買。
5. **漏跑後的委託**：若執行時已過當天 09:00，委託記在當天，下一個交易日開盤才成交（不回頭用舊價）。
6. **重大訊息時間窗**：上一次執行的截止時間 ~ 本次執行時間；第一次執行從決策日 00:00 起。
7. **對照組 0050、賣出盤進場**：初始化日（第一個決策日）之後第一個交易日開盤。賣出盤用初始化日當晚的 PCF；
   那晚若沒有 PCF，改在第一次拿到 PCF 的那晚下單，並在簡報註記「晚於對照組」。
8. **Prompt B 沒寫到的部位**：視為不賣，`risk_log.csv` 記「未填」。
9. **`prompt_prior.md`** 在 repo 根目錄，trial／live 共用；以「- 」開頭的行算一條，超過 10 條會在簡報註記違規。

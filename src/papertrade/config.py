"""鎖定參數（PLAN.md v1）。前進期內不准修改；要改＝新版本。"""

from __future__ import annotations

import datetime as dt

TZ = dt.timezone(dt.timedelta(hours=8))  # 台北時間，無夏令時間

CAPITAL = 1_000_000

# 第 2 節 風控
MAX_NEW_BUYS_PER_DAY = 1
MAX_POSITIONS = 5
MAX_POSITION_FRACTION = 0.20
MAX_GROSS = 1.00  # 由「現金不能為負」保證，這裡只用於檢查與紀錄

# 第 3 節 出場
STOP_FACTOR = 0.92
MAX_HOLD_SESSIONS = 60

# 第 4 節 成本
FEE_RATE = 0.001425  # 無最低金額
TAX_STOCK = 0.003
TAX_ETF = 0.001
SLIPPAGE_TICKS = 1

BENCHMARK = "0050"

# 第 6 節：幾點以後才算「當天準時」的執行
ON_TIME_FROM = dt.time(17, 0)

# 第 5 節：重大訊息每檔最多幾則
MAX_NEWS_PER_STOCK = 5

REVIEW_CATEGORIES = ("理由對", "理由錯", "時機錯", "執行問題", "運氣", "被動出場")
CONFIDENCE_LEVELS = ("低", "中", "高")


def is_etf(code: str) -> bool:
    return code.startswith("00")

"""帳本與逐日結算（PLAN.md v1.2 第 3、4 節）。

一個「session」＝一個交易日：開盤撮合前一次的委託 → 除權息入帳 → 收盤評價與出場觸發。
這裡只做機械規則，不讀任何文字理由；AI 的提案由 risk.py 檢查後才變成委託。
真帳與賣出盤的公式基準（books.py）共用這套引擎。
"""

from __future__ import annotations

import copy
import datetime as dt

from . import config, costs

CA_JUMP = 0.11  # 台股漲跌幅 10%；收盤對前收超過 11% 必有公司行動


def new_state(init_date: dt.date, capital: float = config.CAPITAL) -> dict:
    return {
        "version": 1,
        "capital": capital,
        "init_date": init_date.isoformat(),
        "cash": float(capital),
        "last_session": None,        # 最後結算的交易日
        "session_no": 0,             # 已結算的交易日數
        "last_news_cutoff": None,    # 上一次抓重大訊息的截止時間
        "alerts": [],                # 公司行動待人工處理（處理完從這裡刪掉）
        "positions": {},             # code -> position
        "orders": [],                # 待成交委託
        "next_trade": 1,
        "next_order": 1,
        "benchmark": {"code": config.BENCHMARK, "shares": 0, "cash": float(capital),
                      "entry_date": None, "entry_price": None, "last_close": None,
                      "pending_sell": False, "closed": False},
        "final": False,
        "day": None,                 # 本次執行的狀態（prepare 寫、finalize 讀）
    }


# ---------------------------------------------------------------- 委託

def add_order(state: dict, events: list, *, decision_date: dt.date, side: str, code: str,
              shares: int, kind: str, trade_id: str, delay_sessions: int = 0,
              budget: float | None = None) -> dict:
    """budget：以成交當天開盤價買 budget 元（買不到 1 股則買 1 股），給賣出盤用；shares 此時忽略。"""
    o = {"order_id": f"O{state['next_order']:05d}", "decision_date": decision_date.isoformat(),
         "side": side, "code": code, "shares": int(shares), "kind": kind,
         "trade_id": trade_id, "delay_sessions": delay_sessions}
    if budget is not None:
        o["budget"] = budget
    state["next_order"] += 1
    state["orders"].append(o)
    events.append({"type": "order_created", "date": decision_date.isoformat(), **o})
    return o


def pending_sell_codes(state: dict) -> set[str]:
    return {o["code"] for o in state["orders"] if o["side"] == "sell"}


def new_trade_id(state: dict) -> str:
    t = f"T{state['next_trade']:04d}"
    state["next_trade"] += 1
    return t


# ---------------------------------------------------------------- 評價

def mark_price(state: dict, code: str, quotes: dict | None) -> float | None:
    q = (quotes or {}).get(code)
    if q and q.get("close") is not None:
        return q["close"]
    pos = state["positions"].get(code)
    return pos["last_close"] if pos else None


def equity(state: dict, quotes: dict | None = None) -> float:
    v = state["cash"]
    for code, p in state["positions"].items():
        px = mark_price(state, code, quotes)
        v += p["shares"] * (px if px is not None else p["entry_price"])
    return round(v, 2)


def bench_equity(state: dict) -> float:
    b = state["benchmark"]
    px = b["last_close"] or b["entry_price"] or 0.0
    return round(b["cash"] + b["shares"] * px, 2)


# ---------------------------------------------------------------- 一個交易日

def _ref_price(q: dict, prev_close: float | None) -> float | None:
    return q.get("ref") if q.get("ref") is not None else prev_close


def run_session(state: dict, d: dt.date, quotes: dict, exrights: dict[str, dict],
                trades: dict[str, dict], late_sessions: int = 0,
                max_positions: int | None = config.MAX_POSITIONS) -> list[dict]:
    """結算交易日 d。會修改 state 與 trades（trade_id -> 交易紀錄）。回傳事件列表。

    exrights：當日除權息的持股 {code: {"cash": 每股現金股利, "stock_per_1000": 每千股配股}}，
    明細抓不到時為 {code: {"error": 原因}}。
    max_positions：None＝不限檔數（賣出盤）。
    """
    ev: list[dict] = []
    ds = d.isoformat()
    sn = state["session_no"] + 1  # 本交易日的序號
    due = [o for o in state["orders"] if o["decision_date"] < ds]
    keep = [o for o in state["orders"] if o["decision_date"] >= ds]

    # 1) 賣單先成交（釋放現金），再成交買單
    for o in sorted(due, key=lambda o: o["side"] != "sell"):
        q = quotes.get(o["code"])
        pos = state["positions"].get(o["code"])
        if o["side"] == "sell":
            if pos is None:
                ev.append({"type": "order_cancelled", "date": ds, **o, "why": "無持股"})
                continue
            op = q.get("open") if q else None
            why = None
            if op is None:
                why = "當日無開盤價（停牌或無成交）"
            else:
                _, lim_dn = costs.limit_prices(_ref_price(q, pos["last_close"]) or op, o["code"])
                if op <= lim_dn:
                    why = "開盤跌停，不成交"
            if why:  # v1.2 只剩強制出場：委託保留到下一個交易日開盤，直到成交
                keep.append(o)
                ev.append({"type": "order_carried", "date": ds, **o, "why": why})
                continue
            px = costs.sell_fill_price(op, o["code"])
            proceeds = costs.sell_proceeds(px, pos["shares"], o["code"])
            state["cash"] = round(state["cash"] + proceeds, 2)
            amount = px * pos["shares"]
            t = trades[pos["trade_id"]]
            t["exit"] = {"date": ds, "open": op, "price": px, "shares": pos["shares"],
                         "fee": costs.fee(amount), "tax": costs.tax(amount, o["code"]),
                         "proceeds": proceeds, "kind": o["kind"], "order_id": o["order_id"],
                         "delay_sessions": o["delay_sessions"]}
            divs = sum(x["amount"] for x in t.get("dividends", []))
            t["pnl"] = round(proceeds + divs - t["entry"]["cost"], 2)
            t["return"] = round(t["pnl"] / t["entry"]["cost"], 6)
            t["sessions_held"] = pos["sessions_held"]
            t["status"] = "closed"
            del state["positions"][o["code"]]
            if state.get("alerts"):  # 出清後，該檔的公司行動提醒不再需要處理
                state["alerts"] = [x for x in state["alerts"] if x["code"] != o["code"]]
            ev.append({"type": "fill", "date": ds, "side": "sell", "code": o["code"],
                       "shares": pos["shares"], "open": op, "price": px, "cash_change": proceeds,
                       "order_id": o["order_id"], "trade_id": t["trade_id"], "kind": o["kind"]})
        else:
            t = trades[o["trade_id"]]
            op = q.get("open") if q else None
            why = None
            if op is None:
                why = "當日無開盤價（停牌或無成交）"
            else:
                lim_up, _ = costs.limit_prices(_ref_price(q, None) or op, o["code"])
                if op >= lim_up:
                    why = "開盤漲停，不成交、不追價"
            if why is None and o["code"] in state["positions"]:
                why = "已持有同一檔"
            if why is None and max_positions is not None and len(state["positions"]) >= max_positions:
                why = f"持股已滿 {max_positions} 檔（同日賣單未成交）"
            shares = 0
            if why is None:
                px = costs.buy_fill_price(op, o["code"])
                if o.get("budget"):
                    shares = max(1, costs.max_shares(o["budget"], px))
                else:
                    shares = min(o["shares"], costs.max_shares(state["cash"], px))
                if shares <= 0:
                    why = "現金不足"
            if why:
                t["status"] = "cancelled"
                t["cancel_reason"] = why
                ev.append({"type": "order_cancelled", "date": ds, **o, "why": why})
                continue
            cost = costs.buy_cost(px, shares)
            state["cash"] = round(state["cash"] - cost, 2)
            stop = round(px * config.STOP_FACTOR, 2)
            state["positions"][o["code"]] = {
                "trade_id": t["trade_id"], "shares": shares, "entry_price": px,
                "entry_date": ds, "stop": stop, "sessions_held": 0,
                "last_close": None, "trigger": None}
            t["entry"] = {"date": ds, "open": op, "price": px, "shares": shares,
                          "fee": costs.fee(px * shares), "cost": cost, "order_id": o["order_id"]}
            t["stop_history"] = [{"date": ds, "stop": stop,
                                  "why": f"公式：成交價 {px} × {config.STOP_FACTOR}"}]
            t["status"] = "open"
            ev.append({"type": "fill", "date": ds, "side": "buy", "code": o["code"],
                       "shares": shares, "open": op, "price": px, "cash_change": -cost,
                       "order_id": o["order_id"], "trade_id": t["trade_id"], "kind": o["kind"]})
    state["orders"] = keep

    # 2) 對照組 0050：初始化後第一個交易日開盤買進；期末開盤賣出
    b = state["benchmark"]
    bq = quotes.get(b["code"])
    if bq and bq.get("open") is not None:
        if b["entry_date"] is None and not b["closed"] and ds > state["init_date"]:
            px = costs.buy_fill_price(bq["open"], b["code"])
            n = costs.max_shares(b["cash"], px)
            b["cash"] = round(b["cash"] - costs.buy_cost(px, n), 2)
            b.update(shares=n, entry_date=ds, entry_price=px)
            ev.append({"type": "bench_fill", "date": ds, "side": "buy", "code": b["code"],
                       "shares": n, "price": px})
        elif b["pending_sell"] and b["shares"] > 0:
            px = costs.sell_fill_price(bq["open"], b["code"])
            b["cash"] = round(b["cash"] + costs.sell_proceeds(px, b["shares"], b["code"]), 2)
            ev.append({"type": "bench_fill", "date": ds, "side": "sell", "code": b["code"],
                       "shares": b["shares"], "price": px})
            b.update(shares=0, pending_sell=False, closed=True)

    # 3) 除權息（PLAN v1.1）：現金股利入帳；股票股利改股數，成交價與停損等比例換算；當天不判斷停損
    ex_today: set[str] = set()
    for code, pos in state["positions"].items():
        if code not in exrights or pos["entry_date"] == ds:
            continue  # 除權息日當天開盤才買進的部位沒有配息配股的權利
        ex_today.add(code)
        info = exrights[code]
        if "error" in info:
            state.setdefault("alerts", []).append(
                {"date": ds, "session_no": sn, "code": code, "prev_close": pos["last_close"], "close": None,
                 "msg": f"除權息明細抓取失敗（{info['error']}），股利與股數需人工處理"})
            ev.append({"type": "corporate_action_suspect", "date": ds, "code": code,
                       "why": "除權息明細抓取失敗"})
            continue
        t = trades[pos["trade_id"]]
        cash_amt = round(info["cash"] * pos["shares"], 2)
        if cash_amt:
            state["cash"] = round(state["cash"] + cash_amt, 2)
            t.setdefault("dividends", []).append({"date": ds, "per_share": info["cash"], "amount": cash_amt})
            ev.append({"type": "dividend", "date": ds, "code": code, "amount": cash_amt})
        if info["stock_per_1000"]:
            old_sh = pos["shares"]
            exact = old_sh * (1 + info["stock_per_1000"] / 1000)
            new_sh = int(exact + 1e-9)
            frac_cash = round((exact - new_sh) * 10, 2)  # 不足 1 股以面額 10 元折現
            f = old_sh / exact
            pos.update(shares=new_sh, entry_price=round(pos["entry_price"] * f, 4),
                       stop=round(pos["stop"] * f, 2),
                       last_close=round(pos["last_close"] * f, 4) if pos["last_close"] else None)
            if frac_cash:
                state["cash"] = round(state["cash"] + frac_cash, 2)
                t.setdefault("dividends", []).append({"date": ds, "per_share": 0, "amount": frac_cash,
                                                      "note": "配股不足 1 股折現"})
            t.setdefault("stop_history", []).append(
                {"date": ds, "stop": pos["stop"],
                 "why": f"股票股利每千股 {info['stock_per_1000']} 股：股數 {old_sh}→{new_sh}，停損等比例換算（×{f:.6f}）"})
            t.setdefault("adjustments", []).append(
                {"date": ds, "stock_per_1000": info["stock_per_1000"], "shares_before": old_sh,
                 "shares_after": new_sh, "factor": round(f, 8)})
            ev.append({"type": "stock_dividend", "date": ds, "code": code, "shares": new_sh - old_sh,
                       "why": f"每千股 {info['stock_per_1000']} 股"})
    bx = exrights.get(b["code"])
    if b["shares"] > 0 and bx and "error" not in bx:
        amt = round(bx["cash"] * b["shares"], 2)
        if bx["stock_per_1000"]:
            exact = b["shares"] * (1 + bx["stock_per_1000"] / 1000)
            amt += round((exact - int(exact + 1e-9)) * 10, 2)
            b["shares"] = int(exact + 1e-9)
        b["cash"] = round(b["cash"] + amt, 2)
        ev.append({"type": "bench_dividend", "date": ds, "amount": amt})

    # 4) 收盤：評價、持有天數、停損／到期觸發
    state["session_no"] = sn
    for code, pos in state["positions"].items():
        q = quotes.get(code)
        close = q.get("close") if q else None
        prev = pos["last_close"]
        suspect = (close is not None and prev and abs(close / prev - 1) > CA_JUMP
                   and pos["entry_date"] != ds and code not in ex_today)
        if close is not None:
            pos["last_close"] = close
        pos["sessions_held"] += 1
        if suspect:
            # 超過漲跌幅限制、又不在除權息表上的跳動＝分割、面額變更或減資。不判斷停損，要人工處理。
            state.setdefault("alerts", []).append(
                {"date": ds, "session_no": sn, "code": code, "prev_close": prev, "close": close,
                 "msg": "疑似分割／面額變更／減資，股數與停損需人工調整"})
            ev.append({"type": "corporate_action_suspect", "date": ds, "code": code,
                       "why": f"收盤 {prev} → {close}"})
            continue
        if pos["trigger"] is None:
            # 公司行動 alert 未處理期間，股數與停損都還沒換算，不判斷停損（到期照常）
            alerted = any(x["code"] == code for x in state.get("alerts", []))
            if close is not None and close <= pos["stop"] and code not in ex_today and not alerted:
                pos["trigger"] = {"kind": "stop", "date": ds, "session_no": state["session_no"],
                                  "close": close}
            elif pos["sessions_held"] >= config.MAX_HOLD_SESSIONS:
                pos["trigger"] = {"kind": "expiry", "date": ds, "session_no": state["session_no"]}
            if pos["trigger"]:
                ev.append({"type": "exit_triggered", "date": ds, "code": code, **pos["trigger"]})
    if bq and bq.get("close") is not None:
        b["last_close"] = bq["close"]
    state["last_session"] = ds

    eq = equity(state, quotes)
    ev.append({"type": "daily", "date": ds, "cash": state["cash"],
               "positions_value": round(eq - state["cash"], 2),
               "equity": eq,
               "n_positions": len(state["positions"]),
               "gross": round((eq - state["cash"]) / eq, 4) if eq > 0 else None,
               "bench_equity": bench_equity(state),
               "late_sessions": late_sessions})
    return ev


# ---------------------------------------------------------------- 強制出場委託

def overdue_alerts(state: dict) -> dict[str, dict]:
    """持股的公司行動 alert 已連續 CORP_ACTION_GRACE_SESSIONS 個交易日沒處理 → {code: 最早的 alert}。

    alert 在第 N 個交易日產生；第 N+3 個交易日結算後仍在 → 下一次開盤出清。
    人工處理＝調整 state.json 的股數／停損後，把該筆 alert 刪掉。
    """
    out: dict[str, dict] = {}
    for al in state.get("alerts", []):
        sn = al.get("session_no")
        if (al["code"] in state["positions"] and sn is not None
                and state["session_no"] - sn >= config.CORP_ACTION_GRACE_SESSIONS
                and al["code"] not in out):
            out[al["code"]] = al
    return out


def place_forced_exits(state: dict, run_date: dt.date, pcf_codes: set[str] | None) -> list[dict]:
    """停損／到期／剔除成分股／公司行動逾期未處理／期末 → T+1 開盤賣單。
    記 delay_sessions（觸發到下單隔了幾個交易日）。"""
    ev: list[dict] = []
    pend = pending_sell_codes(state)
    overdue = overdue_alerts(state)
    for code, pos in sorted(state["positions"].items()):
        if code in pend:
            continue
        kind, delay = None, 0
        if state["final"]:
            kind = "final"
        elif pos["trigger"]:
            kind = pos["trigger"]["kind"]
            delay = state["session_no"] - pos["trigger"]["session_no"]
        elif pcf_codes is not None and code not in pcf_codes:
            kind = "index_delete"
        elif code in overdue:
            kind = "corp_action_unhandled"
        if kind:
            add_order(state, ev, decision_date=run_date, side="sell", code=code,
                      shares=pos["shares"], kind=kind, trade_id=pos["trade_id"],
                      delay_sessions=delay)
    if state["final"] and state["benchmark"]["shares"] > 0:
        state["benchmark"]["pending_sell"] = True
    return ev


def snapshot(state: dict) -> dict:
    return copy.deepcopy(state)

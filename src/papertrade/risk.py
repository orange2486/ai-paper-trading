"""Python 風控（PLAN.md v1.2 第 2 節）：真帳 AI 只能提案買，這裡決定能不能變成委託。

真帳沒有 AI 賣出、沒有改停損：提案裡出現 sells／stop_updates 一律作廢。
不讀任何文字理由或新聞內容；文字欄位只檢查「有沒有寫」。
提案格式（ledger/proposals/YYYY-MM-DD.json）見 PROPOSAL_FORMAT.md。
"""

from __future__ import annotations

import datetime as dt

from . import config, costs, engine

BUY_TEXT_FIELDS = ("technical", "institutional", "material_info", "expected_path")


def _row(ds: str, kind: str, code: str, ok: bool, reason: str) -> dict:
    return {"date": ds, "item": kind, "code": code, "result": "通過" if ok else "作廢",
            "reason": reason}


def check(proposal: dict, state: dict, trades: dict, run_date: dt.date,
          quotes: dict, pcf_codes: set[str] | None, new_buys_allowed: bool,
          blocked_reasons: list[str], order_date: dt.date | None = None,
          ) -> tuple[list[dict], list[dict], list[dict]]:
    """回傳 (risk_log 列, 事件, 被接受的檢討)。會修改 state（新增委託）與 trades。

    order_date：委託日（成交在其後第一個交易日開盤）；漏跑時可能晚於 run_date。
    """
    ds = run_date.isoformat()
    od = order_date or run_date
    log: list[dict] = []
    ev: list[dict] = []
    reviews_ok: list[dict] = []

    if proposal.get("date") != ds:
        log.append(_row(ds, "proposal", "", False, f"提案日期 {proposal.get('date')} ≠ {ds}，整份作廢"))
        return log, ev, reviews_ok

    # ---- 檢討（只檢查格式）
    for r in proposal.get("reviews", []) or []:
        tid = r.get("trade_id", "")
        t = trades.get(tid)
        if t is None or t.get("status") != "closed":
            log.append(_row(ds, "review", tid, False, "交易不存在或尚未結束"))
        elif t.get("review"):
            log.append(_row(ds, "review", tid, False, "已檢討過，不能改"))
        elif r.get("category") not in config.REVIEW_CATEGORIES:
            log.append(_row(ds, "review", tid, False, f"類別必須是 {config.REVIEW_CATEGORIES} 之一"))
        elif not str(r.get("analysis", "")).strip():
            log.append(_row(ds, "review", tid, False, "analysis 不能空白"))
        else:
            t["review"] = {"date": ds, "category": r["category"], "analysis": r["analysis"].strip()}
            reviews_ok.append(t["review"] | {"trade_id": tid})
            log.append(_row(ds, "review", tid, True, r["category"]))

    # ---- v1.2：真帳 AI 不能賣、不能改停損（想早賣請寫進 shadow）
    for key, what in (("sells", "sell"), ("stop_updates", "stop_update")):
        for x in proposal.get(key, []) or []:
            log.append(_row(ds, what, str(x.get("code", "")), False,
                            "真帳 AI 不能賣出或改停損（PLAN v1.2 第 3 節）；想早賣請寫進 shadow"))

    pend = engine.pending_sell_codes(state)
    # ---- 新買
    buys = proposal.get("buys", []) or []
    for i, b in enumerate(buys):
        code = str(b.get("code", ""))
        reason = _buy_problem(i, b, code, state, quotes, pcf_codes, new_buys_allowed,
                              blocked_reasons, pend, ds, trades)
        if reason:
            log.append(_row(ds, "buy", code, False, reason))
            continue
        close = quotes[code]["close"]
        est_px = costs.buy_fill_price(close, code)
        shares = costs.max_shares(float(b["amount"]), est_px)
        tid = engine.new_trade_id(state)
        trades[tid] = {
            "trade_id": tid, "code": code, "name": quotes[code]["name"], "status": "ordered",
            "decision_date": ds,
            "proposal": {k: b.get(k) for k in
                         ("amount", "technical", "institutional", "material_info",
                          "expected_path", "target_price", "confidence", "data_as_of")},
            "stop_rule": f"成交價 × {config.STOP_FACTOR}（成交後自動設定，固定不改）",
            "dividends": [], "review": None,
        }
        engine.add_order(state, ev, decision_date=od, side="buy", code=code,
                         shares=shares, kind="ai", trade_id=tid)
        log.append(_row(ds, "buy", code, True, f"{shares} 股，估計 {costs.buy_cost(est_px, shares):,.0f} 元，交易 {tid}"))

    if not proposal.get("buys"):
        why = str(proposal.get("no_action_reason", "")).strip()
        log.append(_row(ds, "no_action", "", bool(why), why or "不動也要寫一行理由（no_action_reason）"))
    return log, ev, reviews_ok


def _buy_problem(i: int, b: dict, code: str, state: dict, quotes: dict,
                 pcf_codes: set[str] | None, new_buys_allowed: bool,
                 blocked_reasons: list[str], pend: set[str], ds: str,
                 trades: dict) -> str | None:
    if not new_buys_allowed:
        return "今天不開新倉：" + "；".join(blocked_reasons)
    if i >= config.MAX_NEW_BUYS_PER_DAY:
        return f"每天最多 {config.MAX_NEW_BUYS_PER_DAY} 筆新買"
    if pcf_codes is None or code not in pcf_codes:
        return "不在當日 0050 成分股（元大 PCF）"
    if code in state["positions"]:
        return "已持有，不加碼"
    if any(t.get("code") == code and (t.get("exit") or {}).get("date") == ds
           and t["exit"].get("kind") == "stop" for t in trades.values()):
        return "停損成交日當晚不得買回同一檔"
    if "stop" in b or "stop_loss" in b:
        return "停損由公式決定（成交價 × 0.92），提案不能自訂"
    for f in BUY_TEXT_FIELDS:
        if not str(b.get(f, "")).strip():
            return f"{f} 不能空白"
    if b.get("confidence") not in config.CONFIDENCE_LEVELS:
        return f"confidence 必須是 {config.CONFIDENCE_LEVELS} 之一"
    if b.get("data_as_of") != ds:
        return f"data_as_of 必須是 {ds}"
    q = quotes.get(code)
    if not q or q.get("close") is None:
        return "當日無收盤價"
    tp = b.get("target_price")
    if not isinstance(tp, (int, float)) or tp <= q["close"]:
        return "target_price 必須是大於今日收盤價的數字"
    amt = b.get("amount")
    if not isinstance(amt, (int, float)) or amt <= 0:
        return "amount 必須是正數（元）"

    total = engine.equity(state, quotes)
    if amt > config.MAX_POSITION_FRACTION * total + 1e-6:
        return f"單檔 {amt:,.0f} 超過總資產 20%（上限 {config.MAX_POSITION_FRACTION * total:,.0f}）"
    held_after = len([c for c in state["positions"] if c not in pend])
    if held_after + 1 > config.MAX_POSITIONS:
        return f"持股會超過 {config.MAX_POSITIONS} 檔"
    # 可用現金＝現金＋同日賣單的估計收入（以今日收盤估）
    cash = state["cash"]
    for c in pend:
        p = state["positions"][c]
        px = engine.mark_price(state, c, quotes) or p["entry_price"]
        cash += costs.sell_proceeds(costs.sell_fill_price(px, c), p["shares"], c)
    est_px = costs.buy_fill_price(q["close"], code)
    if costs.max_shares(float(amt), est_px) <= 0:
        return "金額不足以買 1 股"
    if costs.buy_cost(est_px, costs.max_shares(float(amt), est_px)) > cash + 1e-6:
        return f"現金不足（可用約 {cash:,.0f}），總曝險不能超過 100%"
    return None

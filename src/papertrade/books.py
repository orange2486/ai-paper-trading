"""影子帳與賣出盤（PLAN.md v1.2 第 7、8 節）。都不算正式權益，也不能改到真帳。

兩者是同一種結構：「早賣帳」掛在一本「基準帳」上。
- 影子帳：基準＝真帳。真帳成交一筆，影子帳就有一筆可早賣。
- 賣出盤：基準＝公式基準 `panel_formula_only`（前進期第 1 個成交日，當日 PCF 每檔虛擬 1 萬元，
  只走停損／60 日／剔除／公司行動／期末，用 engine 結算）。AI 的早賣記在 `panel_ai`。

早賣帳只能提早賣：寫「賣」→ 下一個交易日開盤（－1 檔）結算，成本同真帳；跌停或無開盤價則續掛。
沒早賣的部位在基準帳出場時同日同價平倉（`follow_real`）。賣掉的錢不能再買。
"""

from __future__ import annotations

import datetime as dt

from . import config, costs, engine


def new_early_book() -> dict:
    return {"positions": {}}  # trade_id -> 部位


def new_panel(init_date: dt.date) -> dict:
    st = engine.new_state(init_date, capital=0)
    st["benchmark"]["closed"] = True  # 公式基準不買 0050
    return {"status": "not_started", "state": st, "trades": {}, "ai": new_early_book()}


def open_positions(book: dict) -> dict[str, dict]:
    return {tid: p for tid, p in book["positions"].items() if p["status"] == "open"}


def required_codes(book: dict, base_state: dict) -> list[str]:
    """今晚必須寫「賣／不賣」的代號：未平倉、還沒寫過賣、基準帳也還沒有公式賣單（那些反正會跟著出場）。
    簡報與 decide 共用這份清單，兩邊才不會不一致。"""
    pend = engine.pending_sell_codes(base_state)
    return sorted(p["code"] for p in open_positions(book).values() if not p.get("order") and p["code"] not in pend)


def _ev(book_name: str, ds: str, typ: str, p: dict, **kw) -> dict:
    return {"date": ds, "book": book_name, "type": typ, "code": p["code"], "trade_id": p["trade_id"], **kw}


def _close(p: dict, t: dict, ds: str, open_: float | None, px: float, shares: int, kind: str) -> None:
    proceeds = costs.sell_proceeds(px, shares, p["code"])
    divs = sum(x["amount"] for x in t.get("dividends", []))
    cost = t["entry"]["cost"]
    p.update(status="closed", order=None,
             exit={"date": ds, "open": open_, "price": px, "shares": shares, "proceeds": proceeds, "kind": kind},
             pnl=round(proceeds + divs - cost, 2), **{"return": round((proceeds + divs - cost) / cost, 6)})


def pre_session(book: dict, base_state: dict, base_trades: dict, d: dt.date, quotes: dict,
                book_name: str) -> list[dict]:
    """開盤：成交之前寫「賣」的早賣單。必須在基準帳 run_session 之前呼叫（基準帳部位還在）。"""
    ds = d.isoformat()
    ev: list[dict] = []
    for tid, p in sorted(open_positions(book).items()):
        o = p.get("order")
        if not o or o["decision_date"] >= ds:
            continue
        pos = base_state["positions"].get(p["code"])
        if pos is None or pos["trade_id"] != tid:
            continue  # 基準帳已不在；收盤後 post_session 會以 follow_real 平倉
        q = quotes.get(p["code"])
        op = q.get("open") if q else None
        why = None
        if op is None:
            why = "當日無開盤價（停牌或無成交）"
        else:
            _, lim_dn = costs.limit_prices(engine._ref_price(q, pos["last_close"]) or op, p["code"])
            if op <= lim_dn:
                why = "開盤跌停，不成交"
        if why:
            ev.append(_ev(book_name, ds, "order_carried", p, why=why))
            continue
        px = costs.sell_fill_price(op, p["code"])
        _close(p, base_trades[tid], ds, op, px, pos["shares"], "early")
        ev.append(_ev(book_name, ds, "fill", p, side="sell", shares=pos["shares"], open=op, price=px,
                      kind="early", pnl=p["pnl"], reason=o["reason"]))
    return ev


def post_session(book: dict, base_trades: dict, d: dt.date, book_name: str) -> list[dict]:
    """收盤後：基準帳新成交的部位加進來；基準帳已出場、還沒早賣的部位同日同價平倉。"""
    ds = d.isoformat()
    ev: list[dict] = []
    for tid, t in sorted(base_trades.items()):
        if t.get("status") == "open" and tid not in book["positions"]:
            e = t["entry"]
            book["positions"][tid] = {"trade_id": tid, "code": t["code"], "name": t.get("name", t["code"]),
                                      "entry_date": e["date"], "entry_price": e["price"], "cost": e["cost"],
                                      "status": "open", "order": None, "decisions": []}
            ev.append(_ev(book_name, ds, "opened", book["positions"][tid], price=e["price"], shares=e["shares"]))
    for tid, p in sorted(open_positions(book).items()):
        t = base_trades[tid]
        if t.get("status") != "closed":
            continue
        x = t["exit"]
        _close(p, t, x["date"], x["open"], x["price"], x["shares"], "follow_real")
        p["exit"]["base_kind"] = x["kind"]
        ev.append(_ev(book_name, ds, "fill", p, side="sell", shares=x["shares"], open=x["open"],
                      price=x["price"], kind="follow_real", pnl=p["pnl"], why=f"基準帳出場：{x['kind']}"))
    return ev


def decide(book: dict, base_state: dict, decisions: list | None, order_date: dt.date, ds: str, book_name: str,
           allow_shadow_stop: bool = False) -> tuple[list[dict], list[dict]]:
    """Prompt B 的「賣／不賣」。回傳 (risk_log 列, 事件)。required_codes 裡的每一檔都要寫一次。"""
    req = set(required_codes(book, base_state))
    todo = {p["code"]: p for p in open_positions(book).values() if p["code"] in req}
    log: list[dict] = []
    ev: list[dict] = []
    seen: set[str] = set()

    def row(code: str, ok: bool, reason: str) -> dict:
        return {"date": ds, "item": book_name, "code": code, "result": "通過" if ok else "作廢", "reason": reason}

    for x in decisions or []:
        code = str(x.get("code", ""))
        p = todo.get(code)
        dec = x.get("decision")
        reason = str(x.get("reason", "")).strip()
        if p is None:
            log.append(row(code, False, "不在今晚必須寫的清單（已平倉、已寫過賣，或基準帳已有公式賣單）"))
        elif code in seen:
            log.append(row(code, False, "同一檔寫了兩次，只採第一次"))
        elif dec not in config.SELL_DECISIONS:
            log.append(row(code, False, f"decision 必須是 {config.SELL_DECISIONS} 之一（視為不賣）"))
        elif not reason:
            log.append(row(code, False, "reason 不能空白（視為不賣）"))
        else:
            seen.add(code)
            rec = {"date": ds, "decision": dec, "reason": reason}
            ss = x.get("shadow_stop")
            if allow_shadow_stop and isinstance(ss, (int, float)):
                rec["shadow_stop"] = ss  # 只記錄，不改任何停損
            p["decisions"].append(rec)
            if dec == "賣":
                p["order"] = {"decision_date": order_date.isoformat(), "date": ds, "reason": reason}
            log.append(row(code, True, dec + ("，下一個交易日開盤結算" if dec == "賣" else "")))
            ev.append(_ev(book_name, ds, "decision", p, decision=dec, reason=reason,
                          shadow_stop=rec.get("shadow_stop")))
    for code in sorted(set(todo) - seen):
        log.append(row(code, False, "未填，視為不賣"))
        ev.append(_ev(book_name, ds, "decision", todo[code], decision="未填"))
    return log, ev


# ---------------------------------------------------------------- 賣出盤：開始

def start_panel(panel: dict, constituents: list[dict], order_date: dt.date) -> list[dict]:
    """當日 PCF 每一檔下「買 1 萬元」的單，下一個交易日開盤成交（與對照組 0050 同一開盤）。"""
    st = panel["state"]
    st["capital"] = st["cash"] = float(config.PANEL_BUDGET * len(constituents))
    ev: list[dict] = []
    for c in constituents:
        tid = f"P-{c['code']}"
        panel["trades"][tid] = {"trade_id": tid, "code": c["code"], "name": c.get("name", c["code"]),
                                "status": "ordered", "decision_date": order_date.isoformat(), "dividends": []}
        engine.add_order(st, ev, decision_date=order_date, side="buy", code=c["code"], shares=0,
                         kind="panel", trade_id=tid, budget=config.PANEL_BUDGET)
    panel["status"] = "started"
    panel["order_date"] = order_date.isoformat()
    return [e | {"book": "panel_formula_only"} for e in ev]


# ---------------------------------------------------------------- 損益彙總（marked to 收盤）

def trade_pnl(t: dict, base_state: dict, quotes: dict) -> float | None:
    if t.get("status") == "closed":
        return t["pnl"]
    if t.get("status") != "open":
        return None
    pos = base_state["positions"].get(t["code"])
    if pos is None:
        return None
    px = engine.mark_price(base_state, t["code"], quotes) or pos["entry_price"]
    divs = sum(x["amount"] for x in t.get("dividends", []))
    return round(pos["shares"] * px + divs - t["entry"]["cost"], 2)


def summary(book: dict, base_state: dict, base_trades: dict, quotes: dict) -> dict:
    """早賣帳與基準帳（同一批部位）的累計損益。沒早賣的部位兩邊相同。"""
    early = base = 0.0
    n = n_early = 0
    for tid, p in book["positions"].items():
        b = trade_pnl(base_trades[tid], base_state, quotes)
        if b is None:
            continue
        n += 1
        base += b
        if p["status"] == "closed":
            early += p["pnl"]
            n_early += p["exit"]["kind"] == "early"
        else:
            early += b
    return {"n": n, "n_early": n_early, "early_pnl": round(early, 2), "base_pnl": round(base, 2),
            "diff": round(early - base, 2)}

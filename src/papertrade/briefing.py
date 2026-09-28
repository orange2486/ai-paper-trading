"""每日簡報（給 AI 提案用）：真帳、成分股價量與法人、重大訊息、先驗與 lessons、影子帳、賣出盤。"""

from __future__ import annotations

import datetime as dt

from . import books, config, costs, engine
from .store import Store


def _pct(a: float | None, b: float | None) -> str:
    if a is None or b in (None, 0):
        return ""
    return f"{(a / b - 1) * 100:+.1f}"


def stock_table(store: Store, d: dt.date, constituents: list[dict]) -> str:
    dates = [x for x in store.price_dates() if x <= d][-61:]
    hist = {x: store.load_quotes(x) or {} for x in dates}
    inst_dates = [x for x in dates if store.load_institutional(x) is not None][-5:]
    inst = {x: store.load_institutional(x) for x in inst_dates}
    today_inst = inst.get(d)

    lines = ["| 代號 | 名稱 | 權重% | 收盤 | 日% | 5日% | 20日% | 60日% | 距MA20% | 距MA60% | 量/20日均量 | 距60日高% | 外資(張) | 投信(張) | 法人5日(張) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    flagged: list[str] = []
    for c in constituents:
        code = c["code"]
        closes = [hist[x].get(code, {}).get("close") for x in dates]
        vols = [hist[x].get(code, {}).get("volume") for x in dates]
        q = hist.get(d, {}).get(code, {})
        close = q.get("close")

        def back(n: int):
            return closes[-1 - n] if len(closes) > n else None

        # 超過 11% 的跳動＝除權／分割（價格未還原）：只用跳動之後的資料
        jump = None
        for i in range(1, len(closes)):
            a, b = closes[i - 1], closes[i]
            if a and b and abs(b / a - 1) > 0.11:
                jump = i
        if jump is not None:
            closes = [None] * jump + closes[jump:]
            vols = [None] * jump + vols[jump:]
            flagged.append(f"{code}（{dates[jump]}）")
        valid = [x for x in closes if x is not None]
        ma20 = sum(valid[-20:]) / len(valid[-20:]) if len(valid) >= 20 else None
        ma60 = sum(valid[-60:]) / len(valid[-60:]) if len(valid) >= 60 else None
        vv = [v for v in vols[:-1][-20:] if v]
        vol_ratio = f"{q['volume'] / (sum(vv) / len(vv)):.2f}" if vv and q.get("volume") else ""
        hi60 = max(valid[-60:]) if valid else None
        ti = (today_inst or {}).get(code) or {}
        f_lots = f"{ti['foreign'] / 1000:,.0f}" if ti.get("foreign") is not None else ""
        t_lots = f"{ti['trust'] / 1000:,.0f}" if ti.get("trust") is not None else ""
        tot5 = [inst[x].get(code, {}).get("total") for x in inst_dates]
        tot5 = f"{sum(v for v in tot5 if v) / 1000:,.0f}" if any(tot5) else ""
        chg1 = _pct(close, q.get("ref")) if q.get("ref") else _pct(close, back(1))
        lines.append(f"| {code} | {c['name']} | {c.get('weight') or ''} | {close if close is not None else '--'} | "
                     f"{chg1} | {_pct(close, back(5))} | {_pct(close, back(20))} | {_pct(close, back(60))} | "
                     f"{_pct(close, ma20)} | {_pct(close, ma60)} | {vol_ratio} | {_pct(close, hi60)} | "
                     f"{f_lots} | {t_lots} | {tot5} |")
    if flagged:
        lines.append("\n† 這些股票近期有超過漲跌幅的跳動（除權／分割／面額變更，價格未還原），"
                     "只用跳動之後的資料計算，空白＝資料不足：" + "、".join(flagged))
    note = f"\n價量歷史：{dates[0] if dates else '無'} ~ {d}，共 {len(dates)} 個交易日。完整資料在 `data/prices/`、`data/institutional/`。"
    return "\n".join(lines) + note


def _early_table(book: dict, base_state: dict, base_trades: dict, quotes: dict, with_name: bool) -> list[str]:
    """影子帳／賣出盤未平倉部位。停損、到期都是基準帳的公式，只供參考。"""
    rows = sorted(books.open_positions(book).values(), key=lambda p: p["code"])
    if not rows:
        return ["（無）"]
    pend = engine.pending_sell_codes(base_state)
    need = set(books.required_codes(book, base_state))
    out = ["| 代號 | " + ("名稱 | " if with_name else "交易 | ") + "成交價 | 收盤 | 損益% | 公式停損 | 已持有 | 狀態 |",
           "|---|---|---|---|---|---|---|---|"]
    for p in rows:
        pos = base_state["positions"].get(p["code"])
        px = engine.mark_price(base_state, p["code"], quotes)
        if p.get("order"):
            status = f"已寫賣（{p['order']['date']}），待開盤結算"
        elif p["code"] in pend:
            status = "公式已下賣單"
        elif pos and pos["trigger"]:
            status = pos["trigger"]["kind"]
        elif p["code"] in need:
            status = "**要寫賣／不賣**"
        else:
            status = ""
        stop = pos["stop"] if pos else ""
        held = f"{pos['sessions_held']}/{config.MAX_HOLD_SESSIONS}" if pos else ""
        out.append(f"| {p['code']} | {p['name'] if with_name else p['trade_id']} | {p['entry_price']} | {px} | "
                   f"{_pct(px, p['entry_price'])} | {stop} | {held} | {status} |")
    out += ["", f"**今晚必須寫的代號（{len(need)} 檔）**：" + ("、".join(sorted(need)) if need else "（無）")]
    return out


def render(store: Store, state: dict, trades: dict, panel: dict, d: dt.date, constituents: list[dict],
           news: dict, day: dict, prior: str = "") -> str:
    quotes = store.load_quotes(d) or {}
    total = engine.equity(state, quotes)
    out = [f"# 每日簡報 {d}", ""]
    out += [f"- 執行時間：{day['run_at']}（台北）",
            f"- 準時：{'是' if day['on_time'] else '否'}；真帳今天可以新買：**{'可以' if day['new_buys_allowed'] else '不行'}**"]
    if day["blocked_reasons"]:
        out.append(f"- 不能新買的原因：{'；'.join(day['blocked_reasons'])}")
    if state["final"]:
        out.append("- **期末結算日：全部持股已下賣單，不准新買。**")
    out += ["", "## 真帳", "",
            f"- 總資產 {total:,.0f}；現金 {state['cash']:,.0f}；持股 {len(state['positions'])}/{config.MAX_POSITIONS} 檔；"
            f"曝險 {(total - state['cash']) / total * 100:.1f}%",
            f"- 單筆新買上限（總資產 20%）：{total * config.MAX_POSITION_FRACTION:,.0f} 元",
            f"- 對照組 0050：{engine.bench_equity(state):,.0f}",
            "- 真帳出場只有公式（停損＝成交價 × 0.92 固定、60 個交易日、剔除成分股、公司行動逾期、期末）。"
            "AI 不能賣、不能改停損。", ""]

    out += ["### 持股", ""]
    if state["positions"]:
        out += ["| 代號 | 交易 | 股數 | 成交價 | 收盤 | 損益% | 停損 | 距停損% | 已持有 | 狀態 |", "|---|---|---|---|---|---|---|---|---|---|"]
        pend = engine.pending_sell_codes(state)
        for code, p in sorted(state["positions"].items()):
            px = engine.mark_price(state, code, quotes)
            status = "已下賣單" if code in pend else (p["trigger"]["kind"] if p["trigger"] else "")
            out.append(f"| {code} | {p['trade_id']} | {p['shares']} | {p['entry_price']} | {px} | "
                       f"{_pct(px, p['entry_price'])} | {p['stop']} | {_pct(p['stop'], px)} | "
                       f"{p['sessions_held']}/{config.MAX_HOLD_SESSIONS} | {status} |")
    else:
        out.append("（無）")
    out.append("")

    out += ["### 已排定的委託（T+1 開盤）", ""]
    if state["orders"]:
        for o in state["orders"]:
            out.append(f"- {o['order_id']} {o['side']} {o['code']} {o['shares']} 股（{o['kind']}"
                       + (f"，延遲 {o['delay_sessions']} 個交易日" if o["delay_sessions"] else "") + "）")
    else:
        out.append("（無）")
    out.append("")

    todo = [t for t in trades.values() if t.get("status") == "closed" and not t.get("review")]
    out += ["### 待檢討的交易", ""]
    if todo:
        for t in todo:
            e, x = t["entry"], t["exit"]
            out.append(f"- **{t['trade_id']} {t['code']} {t['name']}**：{e['date']} 買 {e['price']} → "
                       f"{x['date']} 賣 {x['price']}（{x['kind']}），損益 {t['pnl']:,.0f}（{t['return'] * 100:+.2f}%），"
                       f"持有 {t['sessions_held']} 日。完整紀錄 `ledger/trades/{t['trade_id']}.json`")
    else:
        out.append("（無）")
    out.append("")

    out += ["## 0050 成分股（元大 PCF）", "", stock_table(store, d, constituents), ""]

    held = set(state["positions"])
    out += ["## 重大訊息（公開資訊觀測站；每檔最多 5 則，依公告時間由新到舊，非 AI 挑選）", ""]
    if news.get("items"):
        out.append(f"範圍：{news['after']} ~ {news['until']}。全宇宙只列標題；真帳持股附全文（PLAN 第 6 節）。")
        out.append("")
        for code, items in sorted(news["items"].items()):
            for it in items:
                out.append(f"- {code} {it['name']}｜{it['announced_at']}｜{it['subject']}")
        full = [(code, it) for code, items in sorted(news["items"].items()) if code in held for it in items]
        if full:
            out += ["", "### 真帳持股的重大訊息全文"]
            for code, it in full:
                out.append(f"\n#### {code} {it['name']}｜{it['announced_at']}\n\n**{it['subject']}**\n\n{it.get('body', '')}")
    else:
        out.append(f"（範圍 {news.get('after')} ~ {news.get('until')} 內無成分股重大訊息）")
    out.append("")

    out += ["## prompt_prior.md（鎖定先驗，前進期凍結）", "",
            prior.strip() or "（空）", ""]
    lessons = store.root / "lessons.md"
    out += ["## lessons.md（買進提案前自問）", "",
            lessons.read_text(encoding="utf-8").strip() if lessons.exists() else "（空）", ""]

    sh = books.summary(state["shadow"], state, trades, quotes)
    out += ["## Prompt B-1：影子帳（只記錄，不成交、不改真帳）", "",
            f"- 同一批 {sh['n']} 筆、已早賣 {sh['n_early']} 筆；影子帳累計損益 {sh['early_pnl']:,.0f}，"
            f"真帳同一批 {sh['base_pnl']:,.0f}，差 {sh['diff']:+,.0f}", ""]
    out += _early_table(state["shadow"], state, trades, quotes, with_name=False)
    out.append("")

    pst = panel["state"]
    pq = quotes
    pn = books.summary(panel["ai"], pst, panel["trades"], pq)
    out += ["## Prompt B-2：賣出盤（全成分虛擬各 1 萬；只記錄）", ""]
    if panel["status"] == "not_started":
        out.append("（尚未開始：拿到第一份當日 PCF 的那晚下單，下一個交易日開盤進場）")
    else:
        out += [f"- 進場委託日 {panel.get('order_date')}；{pn['n']} 檔、已早賣 {pn['n_early']} 檔；"
                f"賣出盤累計損益 {pn['early_pnl']:,.0f}，公式基準 {pn['base_pnl']:,.0f}，差 {pn['diff']:+,.0f}",
                "- 重大訊息只看上方標題（不附全文）。", ""]
        out += _early_table(panel["ai"], pst, panel["trades"], pq, with_name=True)
    out.append("")

    out += ["## 提案", "",
            f"寫到 `ledger/proposals/{d}.json`，格式見 `PROPOSAL_FORMAT.md`。",
            "- Prompt A（真帳）：最多 1 筆 `buys`，或 `no_action_reason`。停損由公式決定，不能自訂；不能寫 sells／stop_updates。",
            "- Prompt B（影子帳 `shadow`、賣出盤 `panel`）：狀態為「要寫賣／不賣」的每一檔都要寫 `decision` 與一行 `reason`；"
            "只能用該筆進場日之後的價量、法人、重大訊息，歷史走勢不構成理由。", ""]
    return "\n".join(out)

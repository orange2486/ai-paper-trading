"""匿名化簡報（試跑回測用，正式帳戶不用）：讓模型認不出是哪一檔、哪一天，避免知識截止前的記憶混進判斷。

遮掉的東西：代號與名稱（每個帳戶隨機對應 S01…S99）、日期（第 N 個交易日）、價格（每檔以歷史第一天收盤＝100 的指數）、
股數（改顯示金額，否則金額÷股數可推回股價）、法人張數（改成占 20 日均量的 %）、0050 權重、重大訊息。
AI 的提案用匿名代號與指數價格回來，由 translate() 換回真實代號與價格，再交給一般的風控。
"""

from __future__ import annotations

import datetime as dt
import random

from . import books, config, engine
from .briefing import _fmt, stock_metrics
from .store import Store

NOTE = """## 匿名回測說明（重要）

這是匿名化的回測：代號（S01…）、日期（第 N 個交易日）與價格（每檔以某一天收盤＝100 的指數）都已改寫，也沒有重大訊息。
請照常判斷，並依下列方式寫提案：
- `code` 寫簡報上的匿名代號（例如 `S17`）。
- `target_price`、`shadow_stop` 用簡報上的**指數價格**。
- `date`、`data_as_of` 寫簡報標題的標籤（例如 `第 12 個交易日`）；程式會自動換算。
- `material_info` 寫「本回測無重大訊息」。
- 其餘格式與規則完全相同。"""


def setup(state: dict, seed: int) -> None:
    pool = [f"S{i:02d}" for i in range(1, 100)]
    random.Random(seed).shuffle(pool)
    state["anon"] = {"seed": seed, "pool": pool, "alias": {}, "base": {}}


def alias(state: dict, code: str) -> str:
    a = state["anon"]
    if code not in a["alias"]:
        a["alias"][code] = a["pool"][len(a["alias"])]
    return a["alias"][code]


def base(state: dict, store: Store, code: str) -> float:
    """指數基準：這檔在帳戶價量歷史裡第一個有收盤價的那天。固定不變，前後各天可以比。"""
    a = state["anon"]
    if code not in a["base"]:
        for d in store.price_dates():
            c = (store.load_quotes(d) or {}).get(code, {}).get("close")
            if c:
                a["base"][code] = c
                break
    return a["base"].get(code) or 1.0


def idx(state: dict, store: Store, code: str, price: float | None) -> str:
    return "" if price is None else f"{price / base(state, store, code) * 100:.2f}"


def day_no(state: dict, store: Store, d: dt.date | str) -> str:
    d = dt.date.fromisoformat(d) if isinstance(d, str) else d
    init = dt.date.fromisoformat(state["init_date"])
    return f"第 {sum(1 for x in store.price_dates() if init <= x <= d)} 個交易日"


def _pos_rows(state, store, book, base_state, quotes, need, pend):
    rows = ["| 代號 | 成交指數 | 收盤指數 | 損益% | 公式停損（指數） | 已持有 | 狀態 |", "|---|---|---|---|---|---|---|"]
    for p in sorted(books.open_positions(book).values(), key=lambda p: alias(state, p["code"])):
        pos = base_state["positions"].get(p["code"])
        px = engine.mark_price(base_state, p["code"], quotes)
        if p.get("order"):
            status = "已寫賣，待開盤結算"
        elif p["code"] in pend:
            status = "公式已下賣單"
        elif p["code"] in need:
            status = "**要寫賣／不賣**"
        else:
            status = ""
        c = p["code"]
        rows.append(f"| {alias(state, c)} | {idx(state, store, c, pos['entry_price'] if pos else p['entry_price'])} | "
                    f"{idx(state, store, c, px)} | {_fmt((px / p['entry_price'] - 1) * 100 if px else None)} | "
                    f"{idx(state, store, c, pos['stop']) if pos else ''} | "
                    f"{pos['sessions_held'] if pos else ''}/{config.MAX_HOLD_SESSIONS} | {status} |")
    return rows


def render(store: Store, state: dict, trades: dict, panel: dict, d: dt.date, constituents: list[dict],
           day: dict, prior: str) -> str:
    quotes = store.load_quotes(d) or {}
    total = engine.equity(state, quotes)
    out = [f"# 每日簡報｜{day_no(state, store, d)}", "",
           f"- 真帳今天可以新買：**{'可以' if day['new_buys_allowed'] else '不行'}**", "", NOTE, "",
           "## 真帳", "",
           f"- 總資產 {total:,.0f}；現金 {state['cash']:,.0f}；持股 {len(state['positions'])}/{config.MAX_POSITIONS} 檔；"
           f"曝險 {(total - state['cash']) / total * 100:.1f}%",
           f"- 單筆新買上限（總資產 20%）：{total * config.MAX_POSITION_FRACTION:,.0f} 元",
           f"- 對照組 0050：{engine.bench_equity(state):,.0f}",
           "- 真帳出場只有公式（停損＝成交價 × 0.92 固定、60 個交易日、剔除成分股、期末）。AI 不能賣、不能改停損。", "",
           "### 持股", ""]
    if state["positions"]:
        out += ["| 代號 | 交易 | 市值（元） | 成交指數 | 收盤指數 | 損益% | 停損指數 | 已持有 |", "|---|---|---|---|---|---|---|---|"]
        for code, p in sorted(state["positions"].items(), key=lambda kv: alias(state, kv[0])):
            px = engine.mark_price(state, code, quotes)
            out.append(f"| {alias(state, code)} | {p['trade_id']} | {p['shares'] * px:,.0f} | {idx(state, store, code, p['entry_price'])} | "
                       f"{idx(state, store, code, px)} | {_fmt((px / p['entry_price'] - 1) * 100)} | "
                       f"{idx(state, store, code, p['stop'])} | {p['sessions_held']}/{config.MAX_HOLD_SESSIONS} |")
    else:
        out.append("（無）")
    out += ["", "### 已排定的委託（下一個交易日開盤）", ""]
    out += [f"- {o['side']} {alias(state, o['code'])}（{o['kind']}）" for o in state["orders"]] or ["（無）"]
    todo = [t for t in trades.values() if t.get("status") == "closed" and not t.get("review")]
    out += ["", "### 待檢討的交易", ""]
    for t in todo:
        e, x = t["entry"], t["exit"]
        out.append(f"- **{t['trade_id']} {alias(state, t['code'])}**：{day_no(state, store, e['date'])} 買（指數 {idx(state, store, t['code'], e['price'])}）→ "
                   f"{day_no(state, store, x['date'])} 賣（指數 {idx(state, store, t['code'], x['price'])}，{x['kind']}），"
                   f"損益 {t['pnl']:,.0f}（{t['return'] * 100:+.2f}%），持有 {t['sessions_held']} 日。"
                   f"買進時寫的預期：{t['proposal']['expected_path']}")
    if not todo:
        out.append("（無）")

    rows, _ = stock_metrics(store, d, constituents)
    out += ["", "## 0050 成分股（匿名；依代號排序）", "",
            "| 代號 | 收盤指數 | 日% | 5日% | 20日% | 60日% | 距MA20% | 距MA60% | 量/20日均量 | 距60日高% | 外資淨買（占均量%） | 投信淨買（%） | 法人5日（%） |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    pct = lambda v, avg: "" if v is None or not avg else f"{v / avg * 100:+.1f}"  # noqa: E731
    flagged = []
    for r in sorted(rows, key=lambda r: alias(state, r["code"])):
        a = alias(state, r["code"])
        if r["jump_date"]:
            flagged.append(a)
        vol = "" if r["vol_ratio"] is None else f"{r['vol_ratio']:.2f}"
        out.append(f"| {a} | {idx(state, store, r['code'], r['close'])} | {_fmt(r['chg1'])} | {_fmt(r['r5'])} | {_fmt(r['r20'])} | "
                   f"{_fmt(r['r60'])} | {_fmt(r['dma20'])} | {_fmt(r['dma60'])} | {vol} | {_fmt(r['dhi60'])} | "
                   f"{pct(r['foreign'], r['avgvol'])} | {pct(r['trust'], r['avgvol'])} | {pct(r['tot5'], r['avgvol'])} |")
    if flagged:
        out.append("\n† 近期有超過漲跌幅的跳動（除權／分割，價格未還原），只用跳動之後的資料：" + "、".join(flagged))

    out += ["", "## prompt_prior.md（鎖定先驗）", "", prior.strip() or "（空）", "",
            "## lessons.md（買進提案前自問）", "", (store.root / "lessons.md").read_text(encoding="utf-8").strip(), ""]

    need = set(books.required_codes(state["shadow"], state))
    out += ["## Prompt B-1：影子帳（只記錄，不成交、不改真帳）", ""]
    out += _pos_rows(state, store, state["shadow"], state, quotes, need, engine.pending_sell_codes(state)) \
        if books.open_positions(state["shadow"]) else ["（無）"]
    out += ["", f"**今晚必須寫的代號（{len(need)} 檔）**：" + ("、".join(sorted(alias(state, c) for c in need)) or "（無）"), ""]
    pst = panel["state"]
    need_p = set(books.required_codes(panel["ai"], pst))
    out += ["## Prompt B-2：賣出盤（全成分虛擬各 1 萬；只記錄）", ""]
    if panel["status"] == "not_started":
        out.append("（尚未開始）")
    else:
        out += _pos_rows(state, store, panel["ai"], pst, quotes, need_p, engine.pending_sell_codes(pst))
        out += ["", f"**今晚必須寫的代號（{len(need_p)} 檔）**：" + ("、".join(sorted(alias(state, c) for c in need_p)) or "（無）")]
    out += ["", "## 提案", "",
            "- Prompt A（真帳）：最多 1 筆 `buys`，或 `no_action_reason`。停損由公式決定，不能自訂。",
            "- Prompt B：清單裡每一檔都寫 `decision` 與一行 `reason`。", ""]
    return "\n".join(out)


def translate(proposal: dict, state: dict, store: Store, d: dt.date) -> dict:
    """匿名代號、指數價格、第 N 天 → 真實代號、價格、日期。對不上的代號原樣留下，讓風控作廢。"""
    back = {v: k for k, v in state["anon"]["alias"].items()}

    def code(x):
        return back.get(str(x).strip(), str(x))

    def price(c, v):
        return round(v / 100 * base(state, store, c), 4) if isinstance(v, (int, float)) else v

    out = dict(proposal)
    out["date"] = d.isoformat()
    buys = []
    for b in proposal.get("buys") or []:
        b = dict(b)
        b["code"] = code(b.get("code"))
        b["target_price"] = price(b["code"], b.get("target_price"))
        b["data_as_of"] = d.isoformat()
        buys.append(b)
    out["buys"] = buys
    for key in ("shadow", "panel"):
        rows = []
        for x in proposal.get(key) or []:
            x = dict(x)
            x["code"] = code(x.get("code"))
            if "shadow_stop" in x:
                x["shadow_stop"] = price(x["code"], x["shadow_stop"])
            rows.append(x)
        out[key] = rows
    return out

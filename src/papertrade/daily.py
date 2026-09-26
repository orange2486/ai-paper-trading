"""每日流程（PLAN.md 第 6 節）。

    prepare  : 抓資料 → 結算未結算的交易日（成交、除權息、出場觸發）→ 強制出場委託 → 抓重大訊息 → 寫簡報
    (AI 讀簡報，寫 ledger/proposals/YYYY-MM-DD.json)
    finalize : 風控檢查提案 → 委託、檢討入帳 → 寫紀錄
"""

from __future__ import annotations

import datetime as dt
import json

from . import briefing, config, engine, risk, sources
from .sources import Fetcher, SourceError
from .store import Store


LESSONS_HEADER = """# lessons.md：提案前自問的檢查問題

規則（PLAN.md 第 8 節）：只能寫「問題」，不能寫硬規則（例如「以後不准買 XX」）；
每條要引用至少一筆交易編號；同類錯誤出現 2 次以上才加；最多 20 條；不影響風控。

（目前沒有）
"""


def _holidays(fx: Fetcher, store: Store, year: int) -> set[dt.date]:
    p = store.raw_dir / "holidays" / f"{year}.json.gz"
    rec = sources.load_raw(p) if p.exists() else sources.fetch_holidays(fx, year)
    return sources.parse_holidays(rec["body"])


def is_weekday_open(d: dt.date, holidays: set[dt.date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def get_quotes(fx: Fetcher, store: Store, d: dt.date) -> dict | None:
    q = store.load_quotes(d)
    if q is not None:
        return q
    q = sources.parse_quotes(sources.fetch_quotes(fx, d)["body"])
    if q is not None:
        store.save_quotes(d, q)
    return q


def get_institutional(fx: Fetcher, store: Store, d: dt.date) -> dict | None:
    i = store.load_institutional(d)
    if i is not None:
        return i
    i = sources.parse_institutional(sources.fetch_institutional(fx, d)["body"])
    if i is not None:
        store.save_institutional(d, i)
    return i


def bootstrap(store: Store, fx: Fetcher, until: dt.date, sessions: int = 70,
              inst_sessions: int = 20) -> int:
    """抓 until（含）之前的歷史價量與法人，給簡報算 K 線指標用。"""
    got, d = 0, until
    hol: dict[int, set] = {}
    while got < sessions:
        if d.year not in hol:
            hol[d.year] = _holidays(fx, store, d.year)
        if is_weekday_open(d, hol[d.year]):
            if get_quotes(fx, store, d) is not None:
                if got < inst_sessions:
                    get_institutional(fx, store, d)
                got += 1
                print(f"  {d} ok ({got}/{sessions})", flush=True)
        d -= dt.timedelta(days=1)
    return got


def init(store: Store, init_date: dt.date, capital: float = config.CAPITAL) -> dict:
    if store.state_path.exists():
        raise SystemExit(f"{store.state_path} 已存在，不能重新初始化")
    state = engine.new_state(init_date, capital)
    # 初始化日前一天視為已結算，第一次 prepare 從 init_date 開始結算
    state["last_session"] = (init_date - dt.timedelta(days=1)).isoformat()
    store.save_state(state)
    (store.root / "lessons.md").write_text(LESSONS_HEADER, encoding="utf-8")
    return state


def prepare(store: Store, fx: Fetcher, now: dt.datetime, final: bool = False) -> dict:
    state = store.load_state()
    trades = store.load_trades()
    events: list[dict] = []
    notes: list[str] = []
    today = now.date()

    if state["day"] and state["day"].get("status") == "awaiting_proposal":
        notes.append(f"{state['day']['date']} 的提案沒有 finalize（視為未提案）")

    # 1) 結算所有未結算的交易日
    processed: list[dt.date] = []
    today_missing = False
    d = dt.date.fromisoformat(state["last_session"]) + dt.timedelta(days=1)
    hol: dict[int, set] = {}
    while d <= today:
        if d.year not in hol:
            hol[d.year] = _holidays(fx, store, d.year)
        if not is_weekday_open(d, hol[d.year]):
            d += dt.timedelta(days=1)
            continue
        if d == today and now.time() < config.ON_TIME_FROM:
            break  # 今天的收盤資料還不算數
        quotes = get_quotes(fx, store, d)
        if quotes is None:
            if d < today:
                notes.append(f"{d} 無行情（臨時休市），跳過")
                d += dt.timedelta(days=1)
                continue
            today_missing = True
            break
        exr = {}
        if state["positions"] or state["benchmark"]["shares"]:
            exr = sources.parse_exrights(sources.fetch_exrights(fx, d)["body"])
        late = 1 if d < today else 0
        events += engine.run_session(state, d, quotes, exr, trades, late_sessions=late)
        processed.append(d)
        d += dt.timedelta(days=1)

    if not processed:
        why = f"{today} 行情尚未取得，稍後重跑" if today_missing else "沒有新的交易日要結算（非交易日、未到 17:00 或今天已跑過）"
        notes.insert(0, why)
        store.append("runs", [{"run_at": now.isoformat(timespec="seconds"),
                               "session_date": state["last_session"], "on_time": False,
                               "new_buys_allowed": False, "blocked_reasons": "",
                               "sessions_processed": 0, "notes": "；".join(notes)}])
        return {"session": None, "briefing": None, "new_buys_allowed": False,
                "blocked": [], "notes": notes, "events": []}

    session = dt.date.fromisoformat(state["last_session"])
    for al in state.get("alerts", []):
        notes.append(f"【需人工處理】{al['date']} {al['code']}：{al['msg']}（{al['prev_close']} → {al['close']}）")
    blocked: list[str] = []
    on_time = bool(processed) and processed[-1] == today and now.time() >= config.ON_TIME_FROM
    if today_missing:
        blocked.append(f"{today} 行情尚未取得")
    elif not on_time:
        blocked.append("不是交易日當晚的準時執行（漏跑或非交易日）")
    if len(processed) > 1:
        notes.append(f"補結算 {len(processed)} 個交易日：{processed[0]} ~ {processed[-1]}")

    # 2) 當日 PCF 成分股
    pcf = None
    try:
        nxt = session + dt.timedelta(days=1)
        while True:
            if nxt.year not in hol:
                hol[nxt.year] = _holidays(fx, store, nxt.year)
            if is_weekday_open(nxt, hol[nxt.year]):
                break
            nxt += dt.timedelta(days=1)
        pcf = sources.parse_pcf(sources.fetch_pcf(fx, session, nxt)["body"])
    except SourceError as exc:
        notes.append(f"PCF 抓取失敗：{exc}")
    pcf_codes = None
    if pcf is None or pcf["trandate"] != session:
        blocked.append(f"元大 PCF 沒有 {session} 的名單")
    else:
        pcf_codes = {c["code"] for c in pcf["constituents"]}
        state["last_constituents"] = pcf["constituents"]
    constituents = pcf["constituents"] if pcf_codes else state.get("last_constituents", [])

    # 3) 三大法人（沒有不擋新買，只在簡報註明）
    if processed and processed[-1] == session:
        try:
            if get_institutional(fx, store, session) is None:
                notes.append(f"{session} 三大法人尚未公布")
        except SourceError as exc:
            notes.append(f"三大法人抓取失敗：{exc}")

    # 4) 重大訊息：上一次截止 ~ 現在
    after = (dt.datetime.fromisoformat(state["last_news_cutoff"])
             if state["last_news_cutoff"] else dt.datetime.combine(session, dt.time(0), config.TZ))
    news = {"after": after.isoformat(timespec="seconds"), "until": now.isoformat(timespec="seconds"),
            "items": {}}
    try:
        idx = []
        nd = after.date()
        while nd <= today:
            idx += sources.parse_news_index(sources.fetch_news_index(fx, today, nd)["body"])
            nd += dt.timedelta(days=1)
        codes = {c["code"] for c in constituents} | set(state["positions"])
        chosen = sources.select_news(idx, codes, after, now)
        for code, items in chosen.items():
            for it in items:
                it["body"] = sources.parse_news_detail(
                    sources.fetch_news_detail(fx, today, it["detail_params"])["body"])
                it["announced_at"] = it["announced_at"].isoformat()
        news["items"] = chosen
        news["fetched_at"] = sources.now_tw().isoformat(timespec="seconds")
        state["last_news_cutoff"] = now.isoformat(timespec="seconds")
    except SourceError as exc:
        notes.append(f"重大訊息抓取失敗：{exc}（簡報不含重大訊息）")
        news["error"] = str(exc)
    store.save_news(today, news)

    # 5) 強制出場委託。漏跑且已過開盤 → 委託日記為今天，下一個交易日開盤才成交（不回頭用舊價）
    order_date = session
    if today > session and now.time() >= dt.time(9, 0):
        order_date = today
    if final:
        state["final"] = True
        blocked.append("期末結算")
    events += engine.place_forced_exits(state, order_date, pcf_codes)

    new_buys_allowed = not blocked
    day = {"date": session.isoformat(), "order_date": order_date.isoformat(),
           "run_at": now.isoformat(timespec="seconds"), "status": "awaiting_proposal",
           "on_time": on_time, "new_buys_allowed": new_buys_allowed,
           "blocked_reasons": blocked, "pcf_codes": sorted(pcf_codes) if pcf_codes else None}
    state["day"] = day

    store.append_events(events)
    store.append("runs", [{"run_at": day["run_at"], "session_date": day["date"], "on_time": on_time,
                           "new_buys_allowed": new_buys_allowed, "blocked_reasons": "；".join(blocked),
                           "sessions_processed": len(processed), "notes": "；".join(notes)}])
    store.save_trades(trades)
    store.save_state(state)
    text = briefing.render(store, state, trades, session, constituents, news, day)
    if notes:
        text = text.replace("\n## 帳戶", "\n- 註記：" + "；".join(notes) + "\n\n## 帳戶", 1)
    p = store.briefing_path(session)
    p.parent.mkdir(parents=True, exist_ok=True)
    store.proposal_path(session).parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return {"session": session, "briefing": p, "new_buys_allowed": new_buys_allowed,
            "blocked": blocked, "notes": notes, "events": events}


def finalize(store: Store) -> dict:
    state = store.load_state()
    trades = store.load_trades()
    day = state.get("day")
    if not day or day.get("status") != "awaiting_proposal":
        raise SystemExit("沒有等待提案的交易日（先跑 prepare）")
    session = dt.date.fromisoformat(day["date"])
    order_date = dt.date.fromisoformat(day["order_date"])
    p = store.proposal_path(session)
    if p.exists():
        proposal = json.loads(p.read_text(encoding="utf-8"))
    else:
        proposal = {"date": session.isoformat(), "no_action_reason": ""}
    quotes = store.load_quotes(session) or {}
    pcf_codes = set(day["pcf_codes"]) if day["pcf_codes"] else None
    log, events, _ = risk.check(proposal, state, trades, session, quotes, pcf_codes,
                                day["new_buys_allowed"], day["blocked_reasons"],
                                order_date=order_date)
    if not p.exists():
        log.append({"date": session.isoformat(), "item": "proposal", "code": "", "result": "作廢",
                    "reason": "沒有提案檔"})
    day["status"] = "done"
    store.append("risk_log", log)
    store.append_events(events)
    store.save_trades(trades)
    store.save_state(state)
    return {"session": session, "log": log, "events": events}

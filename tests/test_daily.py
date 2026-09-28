import datetime as dt
import json

from papertrade import config, daily
from papertrade.store import Store
from helpers import FakeFetcher, q

TZ = config.TZ
D0, D1, D2 = dt.date(2026, 10, 5), dt.date(2026, 10, 6), dt.date(2026, 10, 7)
PCF = ["2330", "2317", "2454"]


def quotes(open_, close, ref=None):
    return [q("2330", 2475, 2475), q("2317", open_, close, ref=ref), q("2454", 1500, 1500),
            q("0050", 112, 112)]


def setup(tmp_path, qd):
    store = Store(tmp_path / "acct")
    daily.init(store, D0)
    return store, FakeFetcher(qd, PCF)


def write_proposal(store, d, **kw):
    p = store.proposal_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": d.isoformat(), **kw}, ensure_ascii=False), encoding="utf-8")


BUY = {"code": "2317", "amount": 200_000, "technical": "t", "institutional": "i", "material_info": "m",
       "expected_path": "p", "target_price": 300, "confidence": "中"}


def test_full_cycle_on_time(tmp_path):
    store, fx = setup(tmp_path, {D0: quotes(250, 250), D1: quotes(252, 255)})
    r = daily.prepare(store, fx, dt.datetime(2026, 10, 5, 20, 0, tzinfo=TZ))
    assert r["session"] == D0 and r["new_buys_allowed"]
    assert r["briefing"].exists() and "2317" in r["briefing"].read_text(encoding="utf-8")

    write_proposal(store, D0, buys=[BUY | {"data_as_of": D0.isoformat()}])
    daily.finalize(store)
    st = store.load_state()
    assert len(st["orders"]) == 1 and st["day"]["status"] == "done"

    daily.prepare(store, fx, dt.datetime(2026, 10, 6, 20, 0, tzinfo=TZ))
    st = store.load_state()
    assert st["positions"]["2317"]["entry_price"] == 252.5
    assert st["benchmark"]["entry_date"] == D1.isoformat()
    assert len(store.read_csv("daily")) == 2 and len(store.read_csv("fills")) == 2


def test_before_17_or_holiday_does_nothing(tmp_path):
    store, fx = setup(tmp_path, {D0: quotes(250, 250)})
    r = daily.prepare(store, fx, dt.datetime(2026, 10, 5, 15, 0, tzinfo=TZ))
    assert r["session"] is None
    r = daily.prepare(store, fx, dt.datetime(2026, 10, 5, 20, 0, tzinfo=TZ))
    assert r["session"] == D0
    r = daily.prepare(store, fx, dt.datetime(2026, 10, 5, 21, 0, tzinfo=TZ))  # 重跑
    assert r["session"] is None


def test_missed_run_blocks_new_buys_but_still_exits(tmp_path):
    store, fx = setup(tmp_path, {D0: quotes(250, 250), D1: quotes(250, 250),
                                 D2: quotes(240, 225, ref=250)})
    daily.prepare(store, fx, dt.datetime(2026, 10, 5, 20, 0, tzinfo=TZ))
    write_proposal(store, D0, buys=[BUY | {"data_as_of": D0.isoformat()}])
    daily.finalize(store)
    daily.prepare(store, fx, dt.datetime(2026, 10, 6, 20, 0, tzinfo=TZ))
    write_proposal(store, D1, no_action_reason="觀望")
    daily.finalize(store)
    # D2 收盤 225 < 停損 250.5×0.92=230.46；D2 晚上漏跑，D3 早上 10:00 才跑
    r = daily.prepare(store, fx, dt.datetime(2026, 10, 8, 10, 0, tzinfo=TZ))
    assert r["session"] == D2 and not r["new_buys_allowed"]
    st = store.load_state()
    sell = [o for o in st["orders"] if o["side"] == "sell"][0]
    assert sell["kind"] == "stop"
    assert sell["decision_date"] == "2026-10-08"  # 已過開盤 → 下一個交易日才成交，不回頭用舊價
    write_proposal(store, D2, buys=[BUY | {"code": "2454", "target_price": 2000, "data_as_of": D2.isoformat()}])
    r = daily.finalize(store)
    assert [x["result"] for x in r["log"] if x["item"] == "buy"] == ["作廢"]


def test_missing_pcf_blocks_buys(tmp_path):
    store, fx = setup(tmp_path, {D0: quotes(250, 250)})
    fx.pcf_codes = []
    r = daily.prepare(store, fx, dt.datetime(2026, 10, 5, 20, 0, tzinfo=TZ))
    assert not r["new_buys_allowed"] and any("PCF" in b for b in r["blocked"])


def test_news_window_and_cutoff(tmp_path):
    store, fx = setup(tmp_path, {D0: quotes(250, 250)})
    fx.news = [["115/10/05", "18:00:00", "2317", "鴻海", "董事會決議",
                {"parameters": {"companyId": "2317", "enterDate": "1151005", "serialNumber": 1}}],
               ["115/10/05", "21:00:00", "2317", "鴻海", "太晚", {"parameters": {}}]]
    daily.prepare(store, fx, dt.datetime(2026, 10, 5, 20, 0, tzinfo=TZ))
    news = store.load_news(D0)
    assert [x["subject"] for x in news["items"]["2317"]] == ["董事會決議"]
    assert news["items"]["2317"][0]["body"] == "全文"
    assert store.load_state()["last_news_cutoff"].startswith("2026-10-05T20:00")


def test_stock_dividend_through_daily_flow(tmp_path):
    store, fx = setup(tmp_path, {D0: quotes(250, 250), D1: quotes(250, 250),
                                 D2: quotes(210, 200, ref=208.33)})
    fx.exrights = {D2: {"2317": (0.0, 200.0)}}   # 每千股配 200 股
    daily.prepare(store, fx, dt.datetime(2026, 10, 5, 20, 0, tzinfo=TZ))
    write_proposal(store, D0, buys=[BUY | {"data_as_of": D0.isoformat()}])
    daily.finalize(store)
    daily.prepare(store, fx, dt.datetime(2026, 10, 6, 20, 0, tzinfo=TZ))
    write_proposal(store, D1, no_action_reason="觀望")
    daily.finalize(store)
    shares = store.load_state()["positions"]["2317"]["shares"]
    daily.prepare(store, fx, dt.datetime(2026, 10, 7, 20, 0, tzinfo=TZ))
    pos = store.load_state()["positions"]["2317"]
    assert pos["shares"] == int(shares * 1.2) and pos["trigger"] is None
    assert pos["stop"] == round(250.5 * 0.92 / 1.2, 2)


def _day(store, fx, d, **proposal):
    daily.prepare(store, fx, dt.datetime(d.year, d.month, d.day, 20, 0, tzinfo=TZ))
    write_proposal(store, d, **proposal)
    return daily.finalize(store)


def test_shadow_and_panel_early_sell(tmp_path):
    D3 = dt.date(2026, 10, 8)
    store, fx = setup(tmp_path, {D0: quotes(250, 250), D1: quotes(250, 250), D2: quotes(255, 255),
                                 D3: quotes(255, 255)})
    _day(store, fx, D0, buys=[BUY | {"data_as_of": D0.isoformat()}])
    panel = store.load_panel()
    assert panel["status"] == "started" and len(panel["state"]["orders"]) == len(PCF)

    r = _day(store, fx, D1, no_action_reason="觀望",
             shadow=[{"code": "2317", "decision": "賣", "reason": "量縮", "shadow_stop": 245}],
             panel=[{"code": "2330", "decision": "賣", "reason": "x"},
                    {"code": "2317", "decision": "不賣", "reason": "y"}])
    panel = store.load_panel()
    assert panel["state"]["positions"]["2330"]["shares"] == 4          # 10,000 / 2480
    assert store.load_state()["benchmark"]["entry_date"] == D1.isoformat()
    log = {(x["item"], x["code"]): x["result"] for x in r["log"]}
    assert log[("shadow", "2317")] == "通過" and log[("panel_ai", "2454")] == "作廢"   # 2454 未填
    assert "Prompt B-1" in store.briefing_path(D1).read_text(encoding="utf-8")

    daily.prepare(store, fx, dt.datetime(2026, 10, 7, 20, 0, tzinfo=TZ))
    st = store.load_state()
    sh = st["shadow"]["positions"]["T0001"]
    assert sh["status"] == "closed" and sh["exit"]["kind"] == "early" and sh["exit"]["price"] == 254.5
    assert "2317" in st["positions"]                                   # 真帳不受影響
    panel = store.load_panel()
    assert panel["ai"]["positions"]["P-2330"]["exit"]["kind"] == "early"
    assert "2330" in panel["state"]["positions"]                       # 公式基準仍持有
    row = store.read_csv("daily")[-1]
    assert float(row["shadow_pnl"]) != float(row["shadow_real_pnl"]) and row["shadow_n_early"] == "1"
    assert any(x["type"] == "fill" for x in store.read_csv("shadow"))
    assert any(x["kind"] == "early" for x in store.read_csv("sell_panel"))


def test_shadow_follows_real_formula_exit(tmp_path):
    D3 = dt.date(2026, 10, 8)
    store, fx = setup(tmp_path, {D0: quotes(250, 250), D1: quotes(250, 250), D2: quotes(240, 225, ref=250),
                                 D3: quotes(220, 222, ref=225)})
    _day(store, fx, D0, buys=[BUY | {"data_as_of": D0.isoformat()}])
    _day(store, fx, D1, no_action_reason="觀望", shadow=[{"code": "2317", "decision": "不賣", "reason": "x"}])
    _day(store, fx, D2, no_action_reason="觀望")                      # 停損觸發；影子帳沒寫
    daily.prepare(store, fx, dt.datetime(2026, 10, 8, 20, 0, tzinfo=TZ))
    st = store.load_state()
    t = store.load_trades()["T0001"]
    sh = st["shadow"]["positions"]["T0001"]
    assert t["exit"]["kind"] == "stop"
    assert sh["exit"]["kind"] == "follow_real" and sh["exit"]["price"] == t["exit"]["price"]
    assert sh["pnl"] == t["pnl"]
    # 賣出盤公式基準同樣觸發停損，panel_ai 跟著出場
    panel = store.load_panel()
    assert panel["trades"]["P-2317"]["exit"]["kind"] == "stop"
    assert panel["ai"]["positions"]["P-2317"]["exit"]["kind"] == "follow_real"
    # 停損成交日當晚不得買回
    write_proposal(store, D3, buys=[BUY | {"target_price": 300, "data_as_of": D3.isoformat()}])
    r = daily.finalize(store)
    assert "停損成交日" in [x for x in r["log"] if x["item"] == "buy"][0]["reason"]


def test_prompt_b_skips_positions_with_formula_sell(tmp_path):
    D3 = dt.date(2026, 10, 8)
    store, fx = setup(tmp_path, {D0: quotes(250, 250), D1: quotes(250, 250), D2: quotes(240, 225, ref=250),
                                 D3: quotes(220, 222, ref=225)})
    _day(store, fx, D0, buys=[BUY | {"data_as_of": D0.isoformat()}])
    _day(store, fx, D1, no_action_reason="x", shadow=[{"code": "2317", "decision": "不賣", "reason": "x"}],
         panel=[{"code": c, "decision": "不賣", "reason": "x"} for c in PCF])
    # D2：2317 觸發停損，真帳與公式基準都已下賣單 → 今晚不必寫 2317
    r = _day(store, fx, D2, no_action_reason="x", panel=[{"code": c, "decision": "不賣", "reason": "x"}
                                                           for c in PCF if c != "2317"])
    missing = [x for x in r["log"] if x["reason"].startswith("未填")]
    assert missing == []
    text = store.briefing_path(D2).read_text(encoding="utf-8")
    assert "今晚必須寫的代號（0 檔）" in text and "今晚必須寫的代號（2 檔）" in text

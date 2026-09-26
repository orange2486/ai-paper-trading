import datetime as dt

from papertrade import engine, risk
from helpers import q

D = dt.date(2026, 10, 5)
QUOTES = {"2330": q("2330", 2475, 2475, name="台積電"), "2317": q("2317", 250, 250, name="鴻海"),
          "2454": q("2454", 1500, 1500), "0050": q("0050", 112, 112)}
PCF = {"2330", "2317", "2454", "2881", "2882", "2303", "2412"}


def buy(code="2330", amount=200_000, **kw):
    b = {"code": code, "amount": amount, "technical": "t", "institutional": "i",
         "material_info": "m", "expected_path": "p", "target_price": 99999,
         "confidence": "中", "data_as_of": D.isoformat()}
    b.update(kw)
    return b


def run(proposal, state=None, allowed=True, trades=None):
    state = state or engine.new_state(D)
    trades = {} if trades is None else trades
    log, ev, _ = risk.check({"date": D.isoformat(), **proposal}, state, trades, D, QUOTES, PCF,
                            allowed, [] if allowed else ["漏跑"])
    return state, trades, log


def results(log, item="buy"):
    return [(r["result"], r["reason"]) for r in log if r["item"] == item]


def test_valid_buy_becomes_order():
    st, trades, log = run({"buys": [buy()]})
    assert results(log)[0][0] == "通過"
    assert len(st["orders"]) == 1 and st["orders"][0]["side"] == "buy"
    assert trades["T0001"]["status"] == "ordered"


def test_only_one_new_buy_per_day():
    st, _, log = run({"buys": [buy("2330"), buy("2317")]})
    assert [r[0] for r in results(log)] == ["通過", "作廢"]
    assert len(st["orders"]) == 1


def test_blocked_day_rejects_buy():
    st, _, log = run({"buys": [buy()]}, allowed=False)
    assert results(log)[0][0] == "作廢" and st["orders"] == []


def test_over_20_percent_rejected():
    _, _, log = run({"buys": [buy(amount=200_001)]})
    assert results(log)[0][0] == "作廢" and "20%" in results(log)[0][1]


def test_not_in_pcf_rejected():
    _, _, log = run({"buys": [buy("2603")]})
    assert "成分股" in results(log)[0][1]


def test_custom_stop_rejected():
    _, _, log = run({"buys": [buy(stop=2300)]})
    assert "停損" in results(log)[0][1]


def test_missing_text_and_bad_target_rejected():
    assert "不能空白" in results(run({"buys": [buy(technical="")]})[2])[0][1]
    assert "target_price" in results(run({"buys": [buy(target_price=2000)]})[2])[0][1]


def _full_state():
    st = engine.new_state(D)
    for i, c in enumerate(["2881", "2882", "2303", "2412", "2454"]):
        st["positions"][c] = {"trade_id": f"T{i}", "shares": 10, "entry_price": 100, "entry_date": "x",
                              "stop": 92, "sessions_held": 1, "last_close": 100, "trigger": None}
    st["cash"] = 900_000
    return st


def test_max_five_positions_unless_selling_same_day():
    _, _, log = run({"buys": [buy(amount=150_000)]}, state=_full_state())
    assert "5 檔" in results(log)[0][1]
    trades = {f"T{i}": {"trade_id": f"T{i}"} for i in range(5)}
    st, _, log = run({"sells": [{"code": "2881", "reason": "換股"}], "buys": [buy(amount=150_000)]},
                     state=_full_state(), trades=trades)
    assert results(log)[0][0] == "通過"


def test_cash_limit_keeps_gross_at_most_100():
    st = engine.new_state(D)
    st["cash"] = 50_000
    st["positions"]["2317"] = {"trade_id": "T9", "shares": 3800, "entry_price": 250, "entry_date": "x",
                               "stop": 230, "sessions_held": 1, "last_close": 250, "trigger": None}
    _, _, log = run({"buys": [buy(amount=150_000)]}, state=st)
    assert "現金不足" in results(log)[0][1]


def test_stop_only_tightens():
    st = engine.new_state(D)
    st["positions"]["2317"] = {"trade_id": "T1", "shares": 10, "entry_price": 250, "entry_date": "x",
                               "stop": 230, "sessions_held": 1, "last_close": 250, "trigger": None}
    trades = {"T1": {"trade_id": "T1", "stop_history": []}}
    _, _, log = run({"stop_updates": [{"code": "2317", "new_stop": 220, "why": "x"}]}, state=st, trades=trades)
    assert results(log, "stop_update")[0][0] == "作廢" and st["positions"]["2317"]["stop"] == 230
    _, _, log = run({"stop_updates": [{"code": "2317", "new_stop": 240, "why": "x"}]}, state=st, trades=trades)
    assert results(log, "stop_update")[0][0] == "通過" and st["positions"]["2317"]["stop"] == 240


def test_review_categories_and_once_only():
    trades = {"T1": {"trade_id": "T1", "status": "closed", "review": None}}
    _, _, log = run({"reviews": [{"trade_id": "T1", "category": "亂寫", "analysis": "x"}],
                     "no_action_reason": "x"}, trades=trades)
    assert results(log, "review")[0][0] == "作廢"
    _, _, log = run({"reviews": [{"trade_id": "T1", "category": "時機錯", "analysis": "x"}],
                     "no_action_reason": "x"}, trades=trades)
    assert results(log, "review")[0][0] == "通過"
    _, _, log = run({"reviews": [{"trade_id": "T1", "category": "運氣", "analysis": "y"}],
                     "no_action_reason": "x"}, trades=trades)
    assert results(log, "review")[0][0] == "作廢" and trades["T1"]["review"]["category"] == "時機錯"


def test_no_action_needs_reason():
    _, _, log = run({})
    assert results(log, "no_action")[0][0] == "作廢"
    _, _, log = run({"no_action_reason": "沒有好標的"})
    assert results(log, "no_action")[0][0] == "通過"


def test_wrong_date_voids_whole_proposal():
    st = engine.new_state(D)
    log, _, _ = risk.check({"date": "2026-10-04", "buys": [buy()]}, st, {}, D, QUOTES, PCF, True, [])
    assert log[0]["result"] == "作廢" and st["orders"] == []

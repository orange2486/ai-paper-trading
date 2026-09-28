import datetime as dt

from papertrade import costs, engine
from helpers import q

D0 = dt.date(2026, 10, 5)  # 初始化日（決策日）
D1, D2, D3 = dt.date(2026, 10, 6), dt.date(2026, 10, 7), dt.date(2026, 10, 8)


def _state_with_buy(code="2317", shares=100, open_=100.0):
    st = engine.new_state(D0)
    trades = {}
    tid = engine.new_trade_id(st)
    trades[tid] = {"trade_id": tid, "code": code, "name": code, "status": "ordered", "dividends": []}
    engine.add_order(st, [], decision_date=D0, side="buy", code=code, shares=shares, kind="ai", trade_id=tid)
    return st, trades, tid


def test_buy_fills_at_next_open_plus_tick_and_sets_formula_stop():
    st, trades, tid = _state_with_buy()
    ev = engine.run_session(st, D1, {"2317": q("2317", 100, 101), "0050": q("0050", 112, 112)}, {}, trades)
    pos = st["positions"]["2317"]
    assert pos["entry_price"] == 100.5          # 開盤 100 + 1 檔 0.5
    assert pos["stop"] == round(100.5 * 0.92, 2)
    assert st["cash"] == round(1_000_000 - costs.buy_cost(100.5, 100), 2)
    assert trades[tid]["status"] == "open"
    assert any(e["type"] == "fill" for e in ev)
    # 對照組同日開盤買進
    assert st["benchmark"]["entry_date"] == D1.isoformat()


def test_order_not_filled_on_decision_day_itself():
    st, trades, _ = _state_with_buy()
    engine.run_session(st, D0, {"2317": q("2317", 100, 101)}, {}, trades)
    assert "2317" not in st["positions"] and len(st["orders"]) == 1
    assert st["benchmark"]["entry_date"] is None  # 初始化日當天不買對照組


def test_limit_up_open_cancels_buy():
    st, trades, tid = _state_with_buy()
    engine.run_session(st, D1, {"2317": q("2317", 110, 110, ref=100)}, {}, trades)
    assert "2317" not in st["positions"] and st["orders"] == []
    assert trades[tid]["status"] == "cancelled"


def test_stop_trigger_then_forced_sell_at_next_open_even_on_gap():
    st, trades, tid = _state_with_buy()
    engine.run_session(st, D1, {"2317": q("2317", 100, 100)}, {}, trades)
    stop = st["positions"]["2317"]["stop"]  # 92.46
    engine.run_session(st, D2, {"2317": q("2317", 99, 92.0, ref=100)}, {}, trades)
    assert st["positions"]["2317"]["trigger"]["kind"] == "stop"
    engine.place_forced_exits(st, D2, {"2317"})
    assert st["orders"][0]["kind"] == "stop"
    # 跳空開低 85：用開盤價（減 1 檔）成交，不是停損價
    engine.run_session(st, D3, {"2317": q("2317", 85, 86, ref=92)}, {}, trades)
    t = trades[tid]
    assert t["status"] == "closed" and t["exit"]["price"] == 84.9 and t["exit"]["price"] < stop
    assert t["pnl"] < 0


def test_limit_down_keeps_forced_sell_for_next_session():
    st, trades, tid = _state_with_buy()
    engine.run_session(st, D1, {"2317": q("2317", 100, 100)}, {}, trades)
    engine.run_session(st, D2, {"2317": q("2317", 95, 90, ref=100)}, {}, trades)
    engine.place_forced_exits(st, D2, {"2317"})
    ev = engine.run_session(st, D3, {"2317": q("2317", 81, 81, ref=90)}, {}, trades)
    assert any(e["type"] == "order_carried" for e in ev)
    assert "2317" in st["positions"] and len(st["orders"]) == 1


def test_expiry_after_60_sessions():
    st, trades, _ = _state_with_buy()
    d = D1
    for _ in range(60):
        engine.run_session(st, d, {"2317": q("2317", 100, 100)}, {}, trades)
        d += dt.timedelta(days=1)
    assert st["positions"]["2317"]["trigger"]["kind"] == "expiry"


def test_index_delete_and_delay_sessions():
    st, trades, _ = _state_with_buy()
    engine.run_session(st, D1, {"2317": q("2317", 100, 100)}, {}, trades)
    engine.place_forced_exits(st, D1, {"2330"})
    assert st["orders"][0]["kind"] == "index_delete"

    st, trades, _ = _state_with_buy()
    engine.run_session(st, D1, {"2317": q("2317", 100, 100)}, {}, trades)
    engine.run_session(st, D2, {"2317": q("2317", 95, 90, ref=100)}, {}, trades)  # 觸發但漏跑
    engine.run_session(st, D3, {"2317": q("2317", 91, 95, ref=90)}, {}, trades)   # 反彈也照樣出場
    engine.place_forced_exits(st, D3, {"2317"})
    assert st["orders"][0]["kind"] == "stop" and st["orders"][0]["delay_sessions"] == 1


def test_dividend_credited_as_cash():
    st, trades, tid = _state_with_buy()
    engine.run_session(st, D1, {"2317": q("2317", 100, 100)}, {}, trades)
    cash = st["cash"]
    engine.run_session(st, D2, {"2317": q("2317", 95, 95, ref=95)}, {"2317": {"cash": 5.0, "stock_per_1000": 0}}, trades)
    assert st["cash"] == cash + 500
    assert trades[tid]["dividends"][0]["amount"] == 500


def test_insufficient_cash_reduces_shares():
    st, trades, tid = _state_with_buy(shares=20_000)
    engine.run_session(st, D1, {"2317": q("2317", 100, 100)}, {}, trades)
    assert st["cash"] >= 0
    assert st["positions"]["2317"]["shares"] < 20_000


def test_split_like_jump_does_not_trigger_stop_and_alerts():
    st, trades, _ = _state_with_buy(code="6669", shares=10, open_=7000)
    engine.run_session(st, D1, {"6669": q("6669", 7000, 7000)}, {}, trades)
    ev = engine.run_session(st, D2, {"6669": q("6669", 2400, 2350, ref=2333)}, {}, trades)
    assert st["positions"]["6669"]["trigger"] is None
    assert any(e["type"] == "corporate_action_suspect" for e in ev)
    assert st["alerts"][0]["code"] == "6669"


def test_ex_dividend_day_skips_stop_only_that_day():
    st, trades, _ = _state_with_buy()
    engine.run_session(st, D1, {"2317": q("2317", 100, 100)}, {}, trades)   # 停損 92.46
    engine.run_session(st, D2, {"2317": q("2317", 90, 91, ref=90)}, {"2317": {"cash": 10.0, "stock_per_1000": 0}}, trades)
    assert st["positions"]["2317"]["trigger"] is None                       # 除息日不判斷
    assert st["positions"]["2317"]["stop"] == 92.46                         # 現金股利不調整停損
    engine.run_session(st, D3, {"2317": q("2317", 91, 91, ref=91)}, {}, trades)
    assert st["positions"]["2317"]["trigger"]["kind"] == "stop"             # 隔天照常判斷


def test_stock_dividend_scales_shares_entry_and_stop():
    st, trades, tid = _state_with_buy(code="6669", shares=10)
    engine.run_session(st, D1, {"6669": q("6669", 7000, 7800)}, {}, trades)
    entry, stop, cash = st["positions"]["6669"]["entry_price"], st["positions"]["6669"]["stop"], st["cash"]
    ev = engine.run_session(st, D2, {"6669": q("6669", 2790, 2610, ref=2610)},
                            {"6669": {"cash": 0.0, "stock_per_1000": 1982.8}}, trades)
    pos = st["positions"]["6669"]
    exact = 10 * 2.9828
    assert pos["shares"] == 29                                   # 29.828 → 29 股
    assert st["cash"] == round(cash + round(0.828 * 10, 2), 2)   # 不足 1 股以面額折現
    f = 10 / exact
    assert pos["entry_price"] == round(entry * f, 4) and pos["stop"] == round(stop * f, 2)
    assert pos["trigger"] is None and not st["alerts"]          # 不是「疑似分割」
    assert any(e["type"] == "stock_dividend" for e in ev)
    assert trades[tid]["adjustments"][0]["shares_after"] == 29


def test_exright_detail_fetch_failure_alerts():
    st, trades, _ = _state_with_buy()
    engine.run_session(st, D1, {"2317": q("2317", 100, 100)}, {}, trades)
    engine.run_session(st, D2, {"2317": q("2317", 80, 80, ref=80)}, {"2317": {"error": "x"}}, trades)
    assert st["positions"]["2317"]["trigger"] is None and st["alerts"]


def test_unhandled_alert_liquidates_after_3_sessions():
    st, trades, tid = _state_with_buy(code="6669", shares=10, open_=7000)
    engine.run_session(st, D1, {"6669": q("6669", 7000, 7000)}, {}, trades)
    engine.run_session(st, D2, {"6669": q("6669", 2400, 2350, ref=2333)}, {}, trades)   # alert（第 2 個交易日）
    d = D3
    for _ in range(2):                                   # 第 3、4 個交易日：還在寬限期
        engine.run_session(st, d, {"6669": q("6669", 2350, 2350)}, {}, trades)
        assert engine.place_forced_exits(st, d, {"6669"}) == []
        d += dt.timedelta(days=1)
    engine.run_session(st, d, {"6669": q("6669", 2350, 2350)}, {}, trades)   # 第 5 個交易日仍未處理
    engine.place_forced_exits(st, d, {"6669"})
    assert st["orders"][0]["kind"] == "corp_action_unhandled"
    engine.run_session(st, d + dt.timedelta(days=1), {"6669": q("6669", 2350, 2350)}, {}, trades)
    assert trades[tid]["exit"]["kind"] == "corp_action_unhandled" and st["alerts"] == []


def test_handled_alert_is_not_liquidated():
    st, trades, _ = _state_with_buy(code="6669", shares=10, open_=7000)
    engine.run_session(st, D1, {"6669": q("6669", 7000, 7000)}, {}, trades)
    engine.run_session(st, D2, {"6669": q("6669", 2400, 2350, ref=2333)}, {}, trades)
    st["alerts"] = []                                    # 人工處理完：停損也等比例換算
    st["positions"]["6669"]["stop"] = round(st["positions"]["6669"]["stop"] / 3, 2)
    d = D3
    for _ in range(5):
        engine.run_session(st, d, {"6669": q("6669", 2350, 2350)}, {}, trades)
        d += dt.timedelta(days=1)
    assert engine.place_forced_exits(st, d, {"6669"}) == []


def test_budget_order_buys_one_share_when_price_above_budget():
    st = engine.new_state(D0, capital=0)
    trades = {"P-9999": {"trade_id": "P-9999", "code": "9999", "status": "ordered", "dividends": []}}
    engine.add_order(st, [], decision_date=D0, side="buy", code="9999", shares=0, kind="panel",
                     trade_id="P-9999", budget=10_000)
    engine.run_session(st, D1, {"9999": q("9999", 12_000, 12_000)}, {}, trades, max_positions=None)
    assert st["positions"]["9999"]["shares"] == 1


def test_buying_on_ex_dividend_day_gets_no_dividend():
    st, trades, tid = _state_with_buy()
    engine.run_session(st, D1, {"2317": q("2317", 95, 95, ref=95)}, {"2317": {"cash": 5.0, "stock_per_1000": 0}}, trades)
    assert trades[tid]["dividends"] == [] and st["positions"]["2317"]["shares"] == 100

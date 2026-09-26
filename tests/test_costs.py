from papertrade import costs


def test_tick_sizes():
    assert costs.tick_size(9.99, "2303") == 0.01
    assert costs.tick_size(49.9, "2303") == 0.05
    assert costs.tick_size(99, "2303") == 0.1
    assert costs.tick_size(250, "2317") == 0.5
    assert costs.tick_size(999, "2454") == 1.0
    assert costs.tick_size(2475, "2330") == 5.0
    assert costs.tick_size(112.4, "0050") == 0.05
    assert costs.tick_size(40, "00878") == 0.01


def test_slippage_one_tick():
    assert costs.buy_fill_price(2475, "2330") == 2480
    assert costs.sell_fill_price(2475, "2330") == 2470
    assert costs.buy_fill_price(250.5, "2317") == 251.0


def test_fee_has_no_minimum_and_tax_differs_for_etf():
    # 1 股台積電：手續費只收百分比，不收最低 20 元
    assert costs.fee(2480) == round(2480 * 0.001425, 2)
    assert costs.tax(100_000, "2330") == 300
    assert costs.tax(100_000, "0050") == 100


def test_buy_cost_and_proceeds():
    assert costs.buy_cost(100, 1000) == 100_000 + 142.5
    assert costs.sell_proceeds(100, 1000, "2317") == 100_000 - 142.5 - 300


def test_max_shares_respects_budget():
    n = costs.max_shares(200_000, 2480)
    assert costs.buy_cost(2480, n) <= 200_000 < costs.buy_cost(2480, n + 1)


def test_limit_prices():
    up, dn = costs.limit_prices(2475, "2330")
    assert up == 2720 and dn == 2230  # 2722.5 向下、2227.5 向上取到 5 元
    up, dn = costs.limit_prices(100, "2317")
    assert up == 110 and dn == 90

import datetime as dt

from papertrade import config, sources
from helpers import mi_body, q


def test_parse_quotes_sign_and_ref():
    body = mi_body([q("2330", 2480, 2475, ref=2500), q("2317", 253, 255, ref=250)])
    out = sources.parse_quotes(body)
    assert out["2330"]["close"] == 2475 and out["2330"]["ref"] == 2500
    assert out["2317"]["ref"] == 250 and out["2317"]["open"] == 253


def test_parse_quotes_no_data():
    assert sources.parse_quotes({"stat": "很抱歉，沒有符合條件的資料!"}) is None


def test_parse_quotes_real_row_format():
    body = {"stat": "OK", "tables": [{"fields": ['證券代號', '證券名稱', '成交股數', '成交筆數', '成交金額', '開盤價', '最高價',
                                                   '最低價', '收盤價', '漲跌(+/-)', '漲跌價差', 'a', 'b', 'c', 'd', 'e'],
                                        "data": [['2330', '台積電', '14,557,662', '75,382', '36,107,476,243', '2,480.00',
                                                  '2,490.00', '2,470.00', '2,475.00', '<p style= color:green>-</p>',
                                                  '25.00', '', '', '', '', ''],
                                                 ['9999', '停牌', '0', '0', '0', '--', '--', '--', '--', ' ', '0.00',
                                                  '', '', '', '', '']]}]}
    out = sources.parse_quotes(body)
    assert out["2330"]["ref"] == 2500 and out["2330"]["volume"] == 14557662
    assert out["9999"]["open"] is None and out["9999"]["close"] is None


def test_select_news_newest_first_max_five_window():
    tz = config.TZ
    items = [{"code": "2330", "name": "台積電", "subject": f"s{i}",
              "announced_at": dt.datetime(2026, 10, 5, 8 + i, 0, tzinfo=tz), "detail_params": {}}
             for i in range(8)]
    items.append({"code": "2603", "name": "x", "subject": "非成分股",
                  "announced_at": dt.datetime(2026, 10, 5, 9, tzinfo=tz), "detail_params": {}})
    after = dt.datetime(2026, 10, 5, 8, 30, tzinfo=tz)
    until = dt.datetime(2026, 10, 5, 14, 0, tzinfo=tz)
    out = sources.select_news(items, {"2330"}, after, until)
    subs = [x["subject"] for x in out["2330"]]
    assert subs == ["s6", "s5", "s4", "s3", "s2"]  # 14:00 以後的 s7 不算、08:00 的 s0 在範圍外
    assert "2603" not in out


def test_parse_news_index_and_pcf_and_holidays():
    body = {"code": 200, "result": {"data": [["115/09/24", "17:36:45", "2317", "鴻海", "主旨\r\n",
                                              {"apiName": "t05st02_detail", "parameters": {"companyId": "2317"}}]]}}
    it = sources.parse_news_index(body)[0]
    assert it["announced_at"] == dt.datetime(2026, 9, 24, 17, 36, 45, tzinfo=config.TZ)
    assert it["detail_params"] == {"companyId": "2317"}

    pcf = sources.parse_pcf({"PCF": {"trandate": "20260924", "anndate": "20260929"},
                             "FundWeights": {"StockWeights": [{"code": "2330", "name": "台積電", "weights": 56.3}]}})
    assert pcf["trandate"] == dt.date(2026, 9, 24) and pcf["constituents"][0]["code"] == "2330"

    hol = sources.parse_holidays({"data": [["2026-01-01", "開國紀念日", ""], ["2026-01-02", "國曆新年開始交易日", ""]]})
    assert hol == {dt.date(2026, 1, 1)}


def test_parse_exright_detail_real_format():
    body = {"stat": "ok", "fields": ["股票代號", "股票名稱", "(每股配發現金股利)除息", "(增資配股) 除權",
                                     "A. 按普通股股東持股比例每千股無償配股", "B. 員工紅利轉增資", "C. (有償) 現金增資",
                                     "每股認購金額", "a. 公開承銷", "b. 員工認購", " c. 原股東認購", "按股東持股比例每千股認購"],
            "data": [["6669  ", "緯穎", "0 元／股", "", "1,982.8 股", "0 股", "0 股", "0 元／股", "0 股", "0 股", "0 股",
                      "0.00000000 股"]]}
    assert sources.parse_exright_detail(body) == {"cash": 0.0, "stock_per_1000": 1982.8}
    body["data"][0][2], body["data"][0][4] = "5.5 元／股", ""
    assert sources.parse_exright_detail(body) == {"cash": 5.5, "stock_per_1000": 0.0}


def test_parse_exrights_codes():
    body = {"stat": "OK", "fields": ["資料日期", "股票代號", "權值+息值"], "data": [["", "2330 ", "5"]]}
    assert sources.parse_exrights(body) == {"2330"}


def test_fetcher_reuse_dir_reads_cached_raw(tmp_path):
    import gzip, json
    from papertrade.sources import Fetcher
    src = tmp_path / "old" / "2026-07-01"
    src.mkdir(parents=True)
    with gzip.open(src / "quotes.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"url": "u", "params": {}, "fetched_at": "t", "body": {"x": 1}}, f)
    fx = Fetcher(tmp_path / "new", reuse_dir=tmp_path / "old")
    assert fx.fetch("2026-07-01", "quotes", "http://invalid", {})["body"] == {"x": 1}
    assert (tmp_path / "new" / "2026-07-01" / "quotes.json.gz").exists()

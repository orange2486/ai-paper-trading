"""測試用：造假的證交所／元大／MOPS 回應。"""

from __future__ import annotations

import datetime as dt

MI_FIELDS = ['證券代號', '證券名稱', '成交股數', '成交筆數', '成交金額', '開盤價', '最高價', '最低價', '收盤價',
             '漲跌(+/-)', '漲跌價差', '最後揭示買價', '最後揭示買量', '最後揭示賣價', '最後揭示賣量', '本益比']


def q(code, open_, close, ref=None, name=None, volume=1_000_000):
    """一檔的行情；ref 預設＝open。"""
    ref = open_ if ref is None else ref
    return {"code": code, "name": name or code, "open": open_, "high": max(open_, close),
            "low": min(open_, close), "close": close, "volume": volume, "value": volume * close,
            "ref": ref}


def mi_body(rows: list[dict]) -> dict:
    data = []
    for r in rows:
        chg = r["close"] - r["ref"]
        sign = '<p style= color:red>+</p>' if chg > 0 else ('<p style= color:green>-</p>' if chg < 0 else ' ')
        f = lambda x: "--" if x is None else f"{x:,.2f}"
        data.append([r["code"], r["name"], f"{r['volume']:,.0f}", "1", f"{r['value']:,.0f}", f(r["open"]),
                     f(r["high"]), f(r["low"]), f(r["close"]), sign, f"{abs(chg):.2f}", "", "", "", "", ""])
    return {"stat": "OK", "tables": [{"title": "指數", "fields": ["指數"], "data": [["x"]]},
                                     {"title": "每日收盤行情", "fields": MI_FIELDS, "data": data}]}


def pcf_body(d: dt.date, codes: list[str]) -> dict:
    return {"PCF": {"trandate": f"{d:%Y%m%d}", "anndate": f"{d:%Y%m%d}"},
            "FundWeights": {"StockWeights": [{"code": c, "name": c, "weights": 2.0} for c in codes]}}


class FakeFetcher:
    """依日期回傳預先放好的資料；沒放的日期回傳「沒有資料」。"""

    def __init__(self, quotes_by_date: dict[dt.date, list[dict]], pcf_codes: list[str],
                 holidays: list[str] = (), news: list[list] = (), exrights: dict | None = None):
        self.quotes = quotes_by_date
        self.pcf_codes = pcf_codes
        self.holidays = list(holidays)
        self.news = list(news)
        self.exrights = exrights or {}
        self.calls: list[tuple] = []

    def fetch(self, folder, name, url, params, post_json=False):
        self.calls.append((url, dict(params)))
        body = self._body(url, params)
        return {"url": url, "params": params, "fetched_at": "x", "body": body}

    def _body(self, url, p):
        if url.endswith("MI_INDEX"):
            d = dt.datetime.strptime(p["date"], "%Y%m%d").date()
            rows = self.quotes.get(d)
            return mi_body(rows) if rows else {"stat": "很抱歉，沒有符合條件的資料!"}
        if url.endswith("T86"):
            return {"stat": "很抱歉，沒有符合條件的資料!"}
        if url.endswith("holidaySchedule"):
            return {"stat": "ok", "data": [[h, "假日", ""] for h in self.holidays]}
        if url.endswith("TWT49U"):
            d = dt.datetime.strptime(p["startDate"], "%Y%m%d").date()
            rows = self.exrights.get(d, {})
            return {"stat": "OK", "fields": ["資料日期", "股票代號", "權值+息值"],
                    "data": [["", c, "0"] for c in rows]} if rows else {"stat": "no"}
        if url.endswith("TWT49UDetail"):  # exrights: {date: {code: (每股現金, 每千股配股)}}
            d = dt.datetime.strptime(p["T1"], "%Y%m%d").date()
            cash, stock = self.exrights[d][p["STK_NO"]]
            return {"stat": "ok", "fields": ["股票代號", "(每股配發現金股利)除息", "A. 按普通股股東持股比例每千股無償配股"],
                    "data": [[p["STK_NO"], f"{cash} 元／股", f"{stock} 股"]]}
        if "yuantaetfs" in url:  # date＝公告日；trandate＝其前一個有行情的交易日
            ann = dt.datetime.strptime(p["date"], "%Y%m%d").date()
            trade = max(d for d in self.quotes if d < ann)
            return pcf_body(trade, self.pcf_codes)
        if url.endswith("t05st02"):
            return {"code": 200, "result": {"data": self.news}}
        if url.endswith("t05st02_detail"):
            return {"code": 200, "result": {"data": [[1, "", "", "", "", "", "", "", "", "全文"]]}}
        raise AssertionError(url)

"""公開資料來源（PLAN.md 第 5 節）：抓取、原樣存檔、解析。

- 價量：證交所 MI_INDEX（某日全部個股收盤行情）
- 三大法人：證交所 T86
- 成分股：元大投信 PCF（申購買回清單）
- 重大訊息：公開資訊觀測站 t05st02（依日期，可重播）＋ t05st02_detail（全文）
- 休市日：證交所 holidaySchedule
- 除權息：證交所 TWT49U

每個回應連同網址、參數、抓取時間原樣存成 gzip JSON。解析函式只吃原始 JSON，方便測試與重播。
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config

UA = "Mozilla/5.0 (research; ai-paper-trading)"
TWSE = "https://www.twse.com.tw/rwd/zh"
YUANTA_BRIDGE = "https://etfapi.yuantaetfs.com/ectranslation/api/bridge"
MOPS_API = "https://mops.twse.com.tw/mops/api"


class SourceError(RuntimeError):
    pass


def now_tw() -> dt.datetime:
    return dt.datetime.now(config.TZ)


def roc(d: dt.date) -> str:
    return f"{d.year - 1911}{d:%m%d}"


def parse_roc_date(s: str) -> dt.date:
    """'115/09/24' 或 '115年09月24日' → date"""
    s = s.replace("年", "/").replace("月", "/").replace("日", "").strip()
    y, m, d = (int(x) for x in s.split("/"))
    return dt.date(y + 1911, m, d)


def num(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace(",", "").strip()
    if s in ("", "--", "---", "X", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- HTTP

@dataclass
class Fetcher:
    raw_dir: Path
    min_interval: float = 3.0
    _last: float = 0.0

    def _wait(self) -> None:
        w = self.min_interval - (time.monotonic() - self._last)
        if w > 0:
            time.sleep(w)

    def request(self, url: str, params: dict, post_json: bool = False) -> dict:
        for attempt in range(4):
            self._wait()
            try:
                if post_json:
                    req = urllib.request.Request(
                        url, data=json.dumps(params).encode(), method="POST",
                        headers={"User-Agent": UA, "Content-Type": "application/json"})
                else:
                    req = urllib.request.Request(
                        url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8")
                self._last = time.monotonic()
                return json.loads(body)
            except Exception as exc:  # 網路錯誤、被暫時封鎖、回傳 HTML
                self._last = time.monotonic()
                if attempt == 3:
                    raise SourceError(f"{url} {params}: {exc}") from exc
                time.sleep(10 * (attempt + 1))
        raise AssertionError("unreachable")

    def fetch(self, folder: str, name: str, url: str, params: dict,
              post_json: bool = False) -> dict:
        """抓取並存成 raw_dir/folder/name.json.gz；回傳 {url, params, fetched_at, body}。"""
        fetched_at = now_tw().isoformat(timespec="seconds")
        body = self.request(url, params, post_json)
        rec = {"url": url, "params": params, "fetched_at": fetched_at, "body": body}
        out = self.raw_dir / folder
        out.mkdir(parents=True, exist_ok=True)
        with gzip.open(out / f"{name}.json.gz", "wt", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        return rec


def load_raw(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 價量

QUOTE_FIELDS = ("code", "name", "open", "high", "low", "close", "volume", "value", "ref")


def fetch_quotes(fx: Fetcher, d: dt.date) -> dict:
    return fx.fetch(f"{d}", "quotes", f"{TWSE}/afterTrading/MI_INDEX",
                    {"date": f"{d:%Y%m%d}", "type": "ALLBUT0999", "response": "json"})


def parse_quotes(body: dict) -> dict[str, dict] | None:
    """回傳 {code: {...}}；該日沒有資料（休市或尚未公布）回傳 None。"""
    if body.get("stat") != "OK":
        return None
    table = None
    for t in body.get("tables", []):
        f = t.get("fields") or []
        if f and f[0] == "證券代號" and "開盤價" in f:
            table = t
            break
    if table is None or not table.get("data"):
        return None
    f = table["fields"]
    ix = {name: f.index(name) for name in
          ("證券代號", "證券名稱", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價",
           "漲跌(+/-)", "漲跌價差")}
    out = {}
    for r in table["data"]:
        code = r[ix["證券代號"]].strip()
        close = num(r[ix["收盤價"]])
        sign_raw = r[ix["漲跌(+/-)"]]
        chg = num(r[ix["漲跌價差"]])
        if "-" in sign_raw:
            sign = -1
        elif "+" in sign_raw:
            sign = 1
        elif chg == 0:
            sign = 0
        else:
            sign = None  # X（不比價，例如除權息當日）或無法判斷
        ref = None
        if close is not None and chg is not None and sign is not None:
            ref = round(close - sign * chg, 4)
        out[code] = {
            "code": code,
            "name": r[ix["證券名稱"]].strip(),
            "open": num(r[ix["開盤價"]]),
            "high": num(r[ix["最高價"]]),
            "low": num(r[ix["最低價"]]),
            "close": close,
            "volume": num(r[ix["成交股數"]]),
            "value": num(r[ix["成交金額"]]),
            "ref": ref,
        }
    return out


# ---------------------------------------------------------------- 三大法人

def fetch_institutional(fx: Fetcher, d: dt.date) -> dict:
    return fx.fetch(f"{d}", "institutional", f"{TWSE}/fund/T86",
                    {"date": f"{d:%Y%m%d}", "selectType": "ALLBUT0999", "response": "json"})


def parse_institutional(body: dict) -> dict[str, dict] | None:
    """{code: {foreign, trust, dealer, total}}（單位：股）。"""
    if body.get("stat") != "OK" or not body.get("data"):
        return None
    f = body["fields"]

    def col(prefix: str) -> int:
        for i, name in enumerate(f):
            if name.startswith(prefix):
                return i
        raise SourceError(f"T86 找不到欄位 {prefix}: {f}")

    ic, ifo, itr, ide, ito = (col("證券代號"), col("外陸資買賣超股數"), col("投信買賣超股數"),
                              col("自營商買賣超股數"), col("三大法人買賣超股數"))
    return {r[ic].strip(): {"foreign": num(r[ifo]), "trust": num(r[itr]),
                            "dealer": num(r[ide]), "total": num(r[ito])}
            for r in body["data"]}


# ---------------------------------------------------------------- 成分股（元大 PCF）

def fetch_pcf(fx: Fetcher, d: dt.date, next_session: dt.date) -> dict:
    """決策日 d 收盤後公布的名單。元大的 date 參數是「公告日」＝下一個交易日；
    回傳的 trandate 應等於 d（呼叫端核對）。"""
    return fx.fetch(f"{d}", "pcf_0050", YUANTA_BRIDGE, {
        "APIType": "ETFAPI", "CompanyName": "YUANTAFUNDS", "DeviceId": "null",
        "FuncId": "PCF/Daily", "AppName": "ETF", "Device": "3", "Platform": "ETF",
        "ticker": config.BENCHMARK, "date": f"{next_session:%Y%m%d}"})


def parse_pcf(body: dict) -> dict | None:
    """{trandate, anndate, constituents: [{code, name, weight}]}"""
    try:
        pcf = body["PCF"]
        stocks = body["FundWeights"]["StockWeights"]
    except (KeyError, TypeError):
        return None
    if not stocks:
        return None
    return {
        "trandate": dt.datetime.strptime(pcf["trandate"], "%Y%m%d").date(),
        "anndate": dt.datetime.strptime(pcf["anndate"], "%Y%m%d").date() if pcf.get("anndate") else None,
        "constituents": [{"code": s["code"].strip(), "name": s["name"].strip(),
                          "weight": s.get("weights")} for s in stocks],
    }


# ---------------------------------------------------------------- 重大訊息（公開資訊觀測站）

def fetch_news_index(fx: Fetcher, run_date: dt.date, d: dt.date) -> dict:
    return fx.fetch(f"{run_date}", f"news_index_{d}", f"{MOPS_API}/t05st02",
                    {"year": str(d.year - 1911), "month": f"{d:%m}", "day": f"{d:%d}"},
                    post_json=True)


def parse_news_index(body: dict) -> list[dict]:
    """[{code, name, announced_at(datetime), subject, detail_params}]"""
    if body.get("code") != 200 or not body.get("result"):
        raise SourceError(f"MOPS t05st02 回應異常：{body.get('code')} {body.get('message')}")
    out = []
    for r in body["result"].get("data") or []:
        date_s, time_s, code, name, subject, link = r[0], r[1], r[2], r[3], r[4], r[5]
        d = parse_roc_date(date_s)
        hh, mm, ss = (int(x) for x in time_s.split(":"))
        out.append({
            "code": code.strip(), "name": name.strip(),
            "announced_at": dt.datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=config.TZ),
            "subject": subject.strip(),
            "detail_params": link.get("parameters", {}) if isinstance(link, dict) else {},
        })
    return out


def fetch_news_detail(fx: Fetcher, run_date: dt.date, params: dict) -> dict:
    name = f"news_{params.get('companyId')}_{params.get('enterDate')}_{params.get('serialNumber')}"
    return fx.fetch(f"{run_date}", name, f"{MOPS_API}/t05st02_detail", params, post_json=True)


def parse_news_detail(body: dict) -> str:
    """回傳說明全文。"""
    if body.get("code") != 200 or not body.get("result"):
        raise SourceError(f"MOPS 明細回應異常：{body.get('code')} {body.get('message')}")
    rows = body["result"].get("data") or []
    if not rows:
        return ""
    return str(rows[0][-1]).replace("\r\n", "\n").strip()


def select_news(items: list[dict], codes: set[str], after: dt.datetime | None,
                until: dt.datetime) -> dict[str, list[dict]]:
    """每檔成分股：公告時間在 (after, until] 內，依公告時間由新到舊取最多 5 則。不由 AI 挑。"""
    seen = set()
    by_code: dict[str, list[dict]] = {}
    for it in items:
        if it["code"] not in codes:
            continue
        if it["announced_at"] > until or (after is not None and it["announced_at"] <= after):
            continue
        key = (it["code"], it["announced_at"], it["subject"])
        if key in seen:
            continue
        seen.add(key)
        by_code.setdefault(it["code"], []).append(it)
    return {c: sorted(v, key=lambda x: x["announced_at"], reverse=True)[:config.MAX_NEWS_PER_STOCK]
            for c, v in by_code.items()}


# ---------------------------------------------------------------- 休市日、除權息

def fetch_holidays(fx: Fetcher, year: int) -> dict:
    return fx.fetch("holidays", f"{year}", f"{TWSE}/holidaySchedule/holidaySchedule",
                    {"date": f"{year}0101", "response": "json"})


def parse_holidays(body: dict) -> set[dt.date]:
    """休市日（不含「開始交易日」「最後交易日」這類其實有交易的列）。"""
    out = set()
    for r in body.get("data") or []:
        name = r[1]
        if "開始交易" in name or "最後交易" in name:
            continue
        out.add(dt.date.fromisoformat(r[0]))
    return out


def fetch_exrights(fx: Fetcher, d: dt.date) -> dict:
    return fx.fetch(f"{d}", "exrights", f"{TWSE}/exRight/TWT49U",
                    {"startDate": f"{d:%Y%m%d}", "endDate": f"{d:%Y%m%d}", "response": "json"})


def parse_exrights(body: dict) -> set[str]:
    """當日除權息（含除權、除息、權息）的代號。"""
    if body.get("stat") != "OK" or not body.get("data"):
        return set()
    ic = body["fields"].index("股票代號")
    return {r[ic].strip() for r in body["data"]}


def fetch_exright_detail(fx: Fetcher, d: dt.date, code: str) -> dict:
    return fx.fetch(f"{d}", f"exright_{code}", f"{TWSE}/exRight/TWT49UDetail",
                    {"STK_NO": code, "T1": f"{d:%Y%m%d}", "response": "json"})


def parse_exright_detail(body: dict) -> dict:
    """{"cash": 每股現金股利, "stock_per_1000": 每千股無償配股}。"""
    if str(body.get("stat", "")).lower() != "ok" or not body.get("data"):
        raise SourceError(f"TWT49UDetail 無資料：{body.get('stat')}")
    f, r = body["fields"], body["data"][0]

    def val(prefix: str) -> float:
        for i, name in enumerate(f):
            if name.strip().startswith(prefix) or prefix in name:
                return num(str(r[i]).replace("元／股", "").replace("股", "")) or 0.0
        raise SourceError(f"TWT49UDetail 找不到欄位 {prefix}: {f}")

    return {"cash": val("(每股配發現金股利)"), "stock_per_1000": val("每千股無償配股")}

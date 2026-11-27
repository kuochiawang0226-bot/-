"""所有外部資料來源。每個函式只負責「抓＋解析成乾淨的 dict」，不做判斷。

資料分級（沿用晨報慣例）：
  🔵A 官方：證交所 TWSE、櫃買 TPEx、期交所 TAIFEX
  🟡B 次級：Yahoo Finance（歷史K線、海外指數）、鉅亨網新聞
抓不到就丟 FetchError，由上層標記「⚪暫缺」，絕不臆造數字。
"""
from __future__ import annotations

import datetime as dt
import html as _html
import re
from urllib.parse import quote

from .net import FetchError, get, get_json

TWSE = "https://www.twse.com.tw"
OAPI = "https://openapi.twse.com.tw/v1"
TPEX = "https://www.tpex.org.tw/openapi/v1"


# ───────────────────────── 小工具 ─────────────────────────
def num(s) -> float | None:
    """'1,234.5' / '-' / '' / '<p>+</p>' → float 或 None"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = re.sub(r"<[^>]+>", "", str(s)).replace(",", "").strip()
    if s in ("", "-", "--", "---", "X", "除權息", "除息", "除權"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_to_date(s: str) -> dt.date:
    """'115/11/26' 或 '1151126' → date"""
    s = s.strip()
    if "/" in s:
        y, m, d = s.split("/")
    else:
        y, m, d = s[:-4], s[-4:-2], s[-2:]
    return dt.date(int(y) + 1911, int(m), int(d))


def ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def is_common_stock(code: str) -> bool:
    """4 碼普通股（排除 00 開頭 ETF、權證、特別股等）"""
    return len(code) == 4 and code.isdigit() and not code.startswith("0")


# ───────────────────────── 證交所 TWSE ─────────────────────────
def twse_fmtqik(month_first: dt.date) -> list[dict]:
    """月成交資訊：每日加權指數、成交金額。"""
    j = get_json(f"{TWSE}/rwd/zh/afterTrading/FMTQIK?response=json&date={ymd(month_first)}",
                 cache=month_first.month != dt.date.today().month)
    if j.get("stat") != "OK":
        return []
    out = []
    for r in j.get("data", []):
        out.append({"date": roc_to_date(r[0]), "shares": num(r[1]), "amount": num(r[2]),
                    "index": num(r[4]), "change": num(r[5])})
    return out


def twse_mi_index(d: dt.date) -> dict:
    """每日收盤行情（全部）＋類股指數＋漲跌家數。"""
    j = get_json(f"{TWSE}/rwd/zh/afterTrading/MI_INDEX?response=json&date={ymd(d)}&type=ALLBUT0999")
    if j.get("stat") not in (None, "OK") or not j.get("tables"):
        raise FetchError(f"MI_INDEX {d} 無資料")
    tables = j["tables"]
    quotes_t = next(t for t in tables if t.get("fields") and t["fields"][0] == "證券代號")
    stocks = {}
    for r in quotes_t["data"]:
        code = r[0].strip()
        sign = "-" if "-" in re.sub(r"<[^>]+>", "", r[9]) else "+"
        chg = num(r[10]) or 0.0
        close = num(r[8])
        stocks[code] = {
            "code": code, "name": r[1].strip(), "market": "上市",
            "volume": num(r[2]), "trades": num(r[3]), "amount": num(r[4]),
            "open": num(r[5]), "high": num(r[6]), "low": num(r[7]), "close": close,
            "change": -chg if sign == "-" else chg, "pe": num(r[15]),
        }
    sectors = []
    for t in tables[:1]:
        for r in t.get("data", []):
            sign = "-" if "-" in re.sub(r"<[^>]+>", "", r[2]) else "+"
            pct = num(r[4]) or 0.0
            sectors.append({"name": r[0], "close": num(r[1]),
                            "pct": -abs(pct) if sign == "-" else abs(pct)})
    breadth = {}
    bt = next((t for t in tables if t.get("fields") and t["fields"][0] == "類型"), None)
    if bt:
        for r in bt["data"]:
            m = re.match(r"([\d,]+)(?:\(([\d,]+)\))?", r[2])
            if not m:
                continue
            n, lim = num(m.group(1)), num(m.group(2)) if m.group(2) else 0
            if r[0].startswith("上漲"):
                breadth["up"], breadth["limit_up"] = n, lim
            elif r[0].startswith("下跌"):
                breadth["down"], breadth["limit_down"] = n, lim
            elif r[0].startswith("持平"):
                breadth["flat"] = n
    return {"date": d, "stocks": stocks, "sectors": sectors, "breadth": breadth}


def twse_t86(d: dt.date) -> dict:
    """三大法人買賣超（股）。"""
    try:
        j = get_json(f"{TWSE}/rwd/zh/fund/T86?response=json&date={ymd(d)}&selectType=ALLBUT0999")
    except FetchError:
        j = get_json(f"{TWSE}/fund/T86?response=json&date={ymd(d)}&selectType=ALLBUT0999")
    if j.get("stat") != "OK":
        raise FetchError(f"T86 {d} 無資料")
    out = {}
    for r in j["data"]:
        out[r[0].strip()] = {"foreign": num(r[4]) or 0, "trust": num(r[10]) or 0,
                             "dealer": num(r[11]) or 0, "total": num(r[18]) or 0}
    return out


def twse_daytrade(d: dt.date) -> dict:
    """當沖成交股數。"""
    j = get_json(f"{TWSE}/exchangeReport/TWTB4U?response=json&date={ymd(d)}&selectType=All")
    if j.get("stat") != "OK":
        raise FetchError(f"TWTB4U {d} 無資料")
    t = next((t for t in j["tables"] if t.get("fields") and t["fields"][0] == "證券代號"
              and len(t["fields"]) >= 6), None)
    if not t or not t.get("data"):
        raise FetchError(f"TWTB4U {d} 尚未公布當沖統計")
    summary = j["tables"][0]["data"][0] if j["tables"][0].get("data") else None
    return {"by_code": {r[0].strip(): {"shares": num(r[3]) or 0, "suspended": bool(r[2].strip())}
                        for r in t["data"]},
            "market_pct": num(summary[1]) if summary else None}


def twse_sbl(d: dt.date) -> dict:
    """借券賣出（當日）。"""
    j = get_json(f"{TWSE}/rwd/zh/marginTrading/TWT93U?date={ymd(d)}&response=json")
    if j.get("stat") != "OK":
        raise FetchError(f"TWT93U {d} 無資料")
    return {r[0].strip(): {"sbl_sell": num(r[9]) or 0, "sbl_balance": num(r[12]) or 0}
            for r in j["data"]}


def twse_margin(d: dt.date) -> dict:
    """融資融券：全市場統計＋個股。"""
    j = get_json(f"{TWSE}/rwd/zh/marginTrading/MI_MARGN?date={ymd(d)}&selectType=ALL&response=json")
    if j.get("stat") != "OK":
        raise FetchError(f"MI_MARGN {d} 無資料")
    summ = {r[0]: {"prev": num(r[4]), "today": num(r[5])} for r in j["tables"][0]["data"]}
    per = {}
    if len(j["tables"]) > 1:
        for r in j["tables"][1]["data"]:
            per[r[0].strip()] = {"margin_prev": num(r[5]), "margin_today": num(r[6]),
                                 "short_prev": num(r[11]), "short_today": num(r[12])}
    return {"summary": summ, "by_code": per}


def twse_valuation(d: dt.date) -> dict:
    """本益比、殖利率、股價淨值比。"""
    j = get_json(f"{TWSE}/rwd/zh/afterTrading/BWIBBU_d?response=json&date={ymd(d)}&selectType=ALL")
    if j.get("stat") != "OK":
        raise FetchError(f"BWIBBU_d {d} 無資料")
    return {r[0].strip(): {"yield": num(r[3]), "pe": num(r[5]), "pb": num(r[6])} for r in j["data"]}


def twse_stock_day(code: str, month_first: dt.date) -> list[dict]:
    """個股日成交（官方 K 線，用於持股與回測記分）。"""
    cur = month_first.year == dt.date.today().year and month_first.month == dt.date.today().month
    j = get_json(f"{TWSE}/rwd/zh/afterTrading/STOCK_DAY?response=json&date={ymd(month_first)}&stockNo={code}",
                 cache=not cur)
    if j.get("stat") != "OK":
        return []
    return [{"date": roc_to_date(r[0]), "volume": num(r[1]), "amount": num(r[2]), "open": num(r[3]),
             "high": num(r[4]), "low": num(r[5]), "close": num(r[6])} for r in j["data"]]


def twse_disposal() -> dict:
    """處置股（OpenAPI，官方）。回傳 {代號: {名稱, 期間, 原因}}，僅保留今天仍在處置期內者。"""
    j = get_json(f"{OAPI}/announcement/punish", cache=False)
    today = dt.date.today()
    out = {}
    for r in j:
        code = r.get("Code", "").strip()
        period = r.get("DispositionPeriod", "")
        m = re.findall(r"(\d+)/(\d+)/(\d+)", period)
        if len(m) >= 2:
            s = dt.date(int(m[0][0]) + 1911, int(m[0][1]), int(m[0][2]))
            e = dt.date(int(m[1][0]) + 1911, int(m[1][1]), int(m[1][2]))
            if e < today:
                continue
        out[code] = {"name": r.get("Name", "").strip(), "period": period,
                     "reason": r.get("ReasonsOfDisposition", "")}
    return out


def twse_companies() -> dict:
    """上市公司基本資料：產業別代碼。"""
    j = get_json(f"{OAPI}/opendata/t187ap03_L", cache=False)
    return {r["公司代號"].strip(): {"industry_code": r.get("產業別", "").strip(),
                                 "short": r.get("公司簡稱", "").strip()} for r in j}


def monthly_revenue() -> dict:
    """上市＋上櫃最新月營收（官方 OpenAPI）。"""
    out = {}
    for url in (f"{OAPI}/opendata/t187ap05_L", f"{TPEX}/mopsfin_t187ap05_O"):
        try:
            j = get_json(url, cache=False)
        except FetchError:
            continue
        for r in j:
            code = str(r.get("公司代號", "")).strip()
            out[code] = {
                "ym": r.get("資料年月"), "industry": r.get("產業別", ""),
                "rev": num(r.get("營業收入-當月營收")),
                "mom": num(r.get("營業收入-上月比較增減(%)")),
                "yoy": num(r.get("營業收入-去年同月增減(%)")),
                "ytd_yoy": num(r.get("累計營業收入-前期比較增減(%)")),
            }
    return out


# ───────────────────────── 櫃買 TPEx（上櫃、興櫃）─────────────────────────
def tpex_quotes() -> dict:
    """上櫃最新收盤行情（官方 OpenAPI，只提供最新一日）。"""
    j = get_json(f"{TPEX}/tpex_mainboard_daily_close_quotes", cache=False)
    out = {}
    for r in j:
        code = r["SecuritiesCompanyCode"].strip()
        out[code] = {
            "code": code, "name": r["CompanyName"].strip(), "market": "上櫃",
            "date": roc_to_date(r["Date"]),
            "volume": num(r.get("TradingShares")), "amount": num(r.get("TransactionAmount")),
            "open": num(r.get("Open")), "high": num(r.get("High")), "low": num(r.get("Low")),
            "close": num(r.get("Close")), "change": num(r.get("Change")),
        }
    return out


def tpex_insti() -> dict:
    j = get_json(f"{TPEX}/tpex_3insti_daily_trading", cache=False)
    out = {}
    for r in j:
        code = r["SecuritiesCompanyCode"].strip()
        k = {key.replace(" ", ""): v for key, v in r.items()}  # 官方欄位名稱空白不一致
        foreign = num(k.get("ForeignInvestorsIncludeMainlandAreaInvestors-Difference")) or 0
        trust = num(k.get("SecuritiesInvestmentTrustCompanies-Difference")) or 0
        dealer = num(k.get("Dealers-Difference")) or 0
        total = num(k.get("TotalDifference"))
        out[code] = {"foreign": foreign, "trust": trust, "dealer": dealer,
                     "total": total if total is not None else foreign + trust + dealer,
                     "date": roc_to_date(r["Date"])}
    return out


def tpex_insti_dated(d: dt.date) -> dict:
    """上櫃三大法人（指定日期）。欄位：外資2-4、外資自營5-7、外資合計8-10、投信11-13、自營合計20-22、三大合計23。"""
    j = get_json(f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW"
                 f"&date={quote(d.strftime('%Y/%m/%d'))}&response=json")
    tables = j.get("tables") or []
    if not tables or not tables[0].get("data"):
        raise FetchError(f"上櫃法人 {d} 無資料")
    return {r[0].strip(): {"foreign": num(r[10]) or 0, "trust": num(r[13]) or 0,
                           "dealer": num(r[22]) or 0, "total": num(r[23]) or 0}
            for r in tables[0]["data"] if len(r) >= 24}


def tpex_valuation() -> dict:
    j = get_json(f"{TPEX}/tpex_mainboard_peratio_analysis", cache=False)
    return {r["SecuritiesCompanyCode"].strip(): {"pe": num(r.get("PriceEarningRatio")),
                                                "yield": num(r.get("YieldRatio")),
                                                "pb": num(r.get("PriceBookRatio"))} for r in j}


def tpex_disposal() -> dict:
    j = get_json(f"{TPEX}/tpex_disposal_information", cache=False)
    today = dt.date.today()
    out = {}
    for r in j:
        period = r.get("DispositionPeriod", "")
        m = re.findall(r"(\d{7})", period)
        if len(m) >= 2 and roc_to_date(m[1]) < today:
            continue
        out[r["SecuritiesCompanyCode"].strip()] = {"name": r.get("CompanyName", ""), "period": period,
                                                  "reason": r.get("DispositionReasons", "")}
    return out


def tpex_companies() -> dict:
    j = get_json(f"{TPEX}/mopsfin_t187ap03_O", cache=False)
    return {r["SecuritiesCompanyCode"].strip(): {"industry_code": r.get("SecuritiesIndustryCode", "").strip(),
                                                "short": r.get("CompanyAbbreviation", "").strip()} for r in j}


def emerging_quotes() -> dict:
    """興櫃最新行情（官方）。"""
    j = get_json(f"{TPEX}/tpex_esb_latest_statistics", cache=False)
    out = {}
    for r in j:
        code = r["SecuritiesCompanyCode"].strip()
        out[code] = {"code": code, "name": r["CompanyName"].strip(), "market": "興櫃",
                     "date": roc_to_date(r["Date"]), "close": num(r.get("LatestPrice")),
                     "avg": num(r.get("Average")), "high": num(r.get("Highest")),
                     "low": num(r.get("Lowest")), "prev_avg": num(r.get("PreviousAveragePrice")),
                     "volume": num(r.get("TransactionVolume"))}
    return out


# ───────────────────────── 期交所 TAIFEX ─────────────────────────
def _html_rows(s: str) -> list[list[str]]:
    s = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", "", s)
    rows = []
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", s):
        cells = re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", tr)
        cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", c))).strip() for c in cells]
        if cells:
            rows.append(cells)
    return rows


def taifex_foreign_oi(d: dt.date) -> dict:
    """臺股期貨三大法人未平倉（口）。"""
    s = get(f"https://www.taifex.com.tw/cht/3/futContractsDate?queryDate={quote(d.strftime('%Y/%m/%d'))}")
    rows = _html_rows(s)
    out, in_tx = {}, False
    for r in rows:
        if any(c == "臺股期貨" for c in r):
            in_tx = True
        elif in_tx and len(r) >= 2 and re.match(r"^\d+$", r[0] or "") and r[1] != "臺股期貨":
            break
        if not in_tx:
            continue
        for who in ("自營商", "投信", "外資"):
            if who in r:
                i = r.index(who)
                vals = [num(x) for x in r[i + 1:]]
                if len(vals) >= 12:
                    out[who] = {"trade_net": vals[4], "oi_long": vals[6], "oi_short": vals[8], "oi_net": vals[10]}
        if "外資" in out:
            break
    if "外資" not in out:
        raise FetchError("期交所外資未平倉解析失敗")
    return {"date": d, **out}


def taifex_night(d: dt.date) -> dict | None:
    """台指期夜盤（queryDate = 夜盤結束的那一天，例：11/26 15:00~11/27 05:00 → 11/27）。"""
    s = get(f"https://www.taifex.com.tw/cht/3/futDailyMarketReport?queryType=2&marketCode=1"
            f"&commodity_id=TX&queryDate={quote(d.strftime('%Y/%m/%d'))}")
    for r in _html_rows(s):
        if len(r) >= 8 and r[0] == "TX" and re.match(r"^\d{6}$", r[1].strip()):
            chg = num(re.sub(r"[▲▼]", "", r[6]))
            pct = num(re.sub(r"[▲▼%]", "", r[7]))
            if "▼" in r[6]:
                chg, pct = -abs(chg or 0), -abs(pct or 0)
            return {"session_end": d, "month": r[1].strip(), "open": num(r[2]), "high": num(r[3]),
                    "low": num(r[4]), "close": num(r[5]), "change": chg, "pct": pct, "volume": num(r[8])}
    return None


# ───────────────────────── Yahoo Finance（🟡B）─────────────────────────
def yahoo_chart(symbol: str, rng: str = "1y", interval: str = "1d", cache: bool = True) -> dict:
    j = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?range={rng}&interval={interval}",
                 cache=cache)
    res = (j.get("chart") or {}).get("result")
    if not res:
        raise FetchError(f"Yahoo {symbol} 無資料")
    r = res[0]
    q = r["indicators"]["quote"][0]
    tz = dt.timezone(dt.timedelta(seconds=r["meta"].get("gmtoffset", 0)))
    bars = []
    for i, ts in enumerate(r.get("timestamp") or []):
        c = q["close"][i]
        if c is None:
            continue
        bars.append({"date": dt.datetime.fromtimestamp(ts, tz).date(), "open": q["open"][i],
                     "high": q["high"][i], "low": q["low"][i], "close": c, "volume": q["volume"][i]})
    m = r["meta"]
    return {"symbol": symbol, "bars": bars, "price": m.get("regularMarketPrice"),
            "prev_close": m.get("chartPreviousClose") or m.get("previousClose"),
            "time": dt.datetime.fromtimestamp(m.get("regularMarketTime", 0), tz)}


GLOBAL_SYMBOLS = {
    "^DJI": "道瓊", "^GSPC": "S&P 500", "^IXIC": "那斯達克", "^SOX": "費城半導體",
    "^VIX": "VIX 恐慌指數", "TSM": "台積電 ADR", "NVDA": "輝達", "TWD=X": "美元/台幣",
    "^N225": "日經 225", "^KS11": "韓國綜合", "CL=F": "原油", "^TNX": "美 10 年債殖利率",
}


def global_markets() -> dict:
    out = {}
    for sym, name in GLOBAL_SYMBOLS.items():
        try:
            c = yahoo_chart(sym, "1mo", cache=False)
            bars = c["bars"]
            if len(bars) < 2:
                continue
            last, prev = bars[-1], bars[-2]
            closes = [b["close"] for b in bars]
            down3 = len(closes) >= 4 and all(closes[-i] < closes[-i - 1] for i in (1, 2, 3))
            out[sym] = {"name": name, "date": last["date"].isoformat(), "close": round(last["close"], 2),
                        "pct": round((last["close"] / prev["close"] - 1) * 100, 2),
                        "spark": [round(x, 2) for x in closes[-20:]], "down3": down3}
        except FetchError:
            continue
    return out


# ───────────────────────── 新聞（鉅亨網 🟡B）─────────────────────────
TWZ = dt.timezone(dt.timedelta(hours=8))
NEWS_CATS = {"tw_stock": "台股", "wd_stock": "國際股", "headline": "頭條", "tw_macro": "台灣總經"}
RSS_FEEDS = [
    ("Google 新聞", "台股", "https://news.google.com/rss/search?q=%E5%8F%B0%E8%82%A1&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ("Google 新聞", "國際股", "https://news.google.com/rss/search?q=%E7%BE%8E%E8%82%A1+%E8%B2%BB%E5%8D%8A&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ("Yahoo 股市", "台股", "https://tw.stock.yahoo.com/rss?category=tw-market"),
    ("經濟日報", "台股", "https://money.udn.com/rssfeed/news/1001/5590?ch=money"),
]


def _rss_items(xml: str) -> list[dict]:
    out = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def tag(t):
            m = re.search(rf"<{t}[^>]*>(.*?)</{t}>", it, re.S)
            if not m:
                return ""
            v = re.sub(r"^<!\[CDATA\[|\]\]>$", "", m.group(1).strip())
            return _html.unescape(re.sub(r"<[^>]+>", "", v)).strip()
        out.append({"title": tag("title"), "link": tag("link"), "date": tag("pubDate"), "source": tag("source"),
                    "desc": tag("description")})
    return out


def _news_time(s: str) -> str:
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s).astimezone(TWZ).isoformat(timespec="minutes")
    except Exception:  # noqa: BLE001
        return ""


def _tag_stocks(text: str, name_map: dict | None) -> list[dict]:
    """從標題找出提到的個股（名稱或 4 碼代號）。"""
    if not name_map:
        return []
    found = []
    for code in re.findall(r"(?<!\d)(\d{4})(?!\d)", text):
        if code in name_map.values() and code not in [f["code"] for f in found]:
            found.append({"code": code, "name": next(n for n, c in name_map.items() if c == code)})
    for name, code in name_map.items():
        if len(found) >= 6:
            break
        if name in text and code not in [f["code"] for f in found]:
            found.append({"code": code, "name": name})
    return found[:6]


def news(limit: int = 15, name_map: dict | None = None) -> list[dict]:
    """多來源財經新聞：鉅亨網、Google 新聞（彙整工商、經濟、自由、中央社等）、Yahoo 股市、經濟日報、公司重大訊息。"""
    items, seen = [], set()

    def add(it):
        key = re.sub(r"[\s\W]", "", it["title"])[:24]
        if not it["title"] or key in seen:
            return
        seen.add(key)
        items.append(it)

    for cat, label in NEWS_CATS.items():
        try:
            j = get_json(f"https://api.cnyes.com/media/api/v1/newslist/category/{cat}?limit={limit}", cache=False)
        except FetchError:
            continue
        for it in j.get("items", {}).get("data", []):
            stocks = [{"code": str(m["code"]), "name": m.get("name", "")} for m in (it.get("market") or [])
                      if isinstance(m, dict) and m.get("code")]
            add({"id": it.get("newsId"), "cat": label, "src": "鉅亨網", "title": _html.unescape(it.get("title", "")),
                 "summary": _html.unescape(re.sub(r"<[^>]+>", "", it.get("summary") or ""))[:160],
                 "time": dt.datetime.fromtimestamp(int(it.get("publishAt", 0)), TWZ).isoformat(timespec="minutes"),
                 "url": f"https://news.cnyes.com/news/id/{it.get('newsId')}", "stocks": stocks[:6]})

    for src_name, label, url in RSS_FEEDS:
        try:
            xml = get(url, cache=False, retries=2)
        except FetchError:
            continue
        for r in _rss_items(xml)[:limit]:
            title, source = r["title"], r["source"] or src_name
            if src_name == "Google 新聞" and " - " in title:
                title, source = title.rsplit(" - ", 1)[0], r["source"] or title.rsplit(" - ", 1)[1]
            if not r["link"].startswith("https://") or any(x in source for x in ("Facebook", "YouTube", "Instagram", "Threads", "PTT")):
                continue
            add({"id": r["link"], "cat": label, "src": source, "title": title, "summary": r["desc"][:160] if src_name != "Google 新聞" else "",
                 "time": _news_time(r["date"]), "url": r["link"], "stocks": _tag_stocks(title, name_map)})

    # 公司重大訊息（官方）
    for url, code_k, name_k in ((f"{OAPI}/opendata/t187ap04_L", "公司代號", "公司名稱"),
                                (f"{TPEX}/mopsfin_t187ap04_O", "SecuritiesCompanyCode", "CompanyName")):
        try:
            j = get_json(url, cache=False, retries=2, timeout=60)
        except FetchError:
            continue
        for r in j[:60]:
            k = {kk.strip(): v for kk, v in r.items()}
            code, name = str(k.get(code_k, "")).strip(), str(k.get(name_k, "")).strip()
            subj = re.sub(r"\s+", "", str(k.get("主旨", "")))[:90]
            d, t = str(k.get("發言日期", "")), str(k.get("發言時間", "")).zfill(6)
            try:
                ts = dt.datetime.combine(roc_to_date(d), dt.time(int(t[:2]), int(t[2:4]))).replace(tzinfo=TWZ).isoformat(timespec="minutes")
            except Exception:  # noqa: BLE001
                ts = ""
            add({"id": f"ann-{code}-{d}-{t}-{subj[:10]}", "cat": "重大訊息", "src": "公開資訊觀測站", "title": f"{name}：{subj}",
                 "summary": re.sub(r"\s+", " ", str(k.get("說明", "")))[:160], "time": ts,
                 "url": f"https://mops.twse.com.tw/mops/#/web/t05sr01_1", "stocks": [{"code": code, "name": name}] if code else []})

    items.sort(key=lambda x: x["time"], reverse=True)
    return items


# ───────────────────────── 盤中即時 ─────────────────────────
def realtime_quotes(codes: list[tuple[str, str]]) -> dict:
    """codes = [(代號, 市場)]。先試證交所 mis（最即時），被擋就改 Yahoo。"""
    out = {}
    if codes:
        ex = "|".join((f"otc_{c}.tw" if m in ("上櫃", "興櫃") else f"tse_{c}.tw") for c, m in codes)
        try:
            j = get_json(f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={quote(ex, safe='|_.')}&json=1&delay=0",
                         cache=False, retries=1, headers={"Referer": "https://mis.twse.com.tw/stock/index.jsp"})
            for m in j.get("msgArray", []):
                price = num(m.get("z")) or num((m.get("b") or "").split("_")[0])
                out[m["c"]] = {"price": price, "prev": num(m.get("y")), "open": num(m.get("o")),
                               "high": num(m.get("h")), "low": num(m.get("l")), "volume": num(m.get("v")),
                               "time": m.get("t"), "src": "證交所即時"}
        except FetchError:
            pass
    for c, m in codes:
        if c in out and out[c]["price"]:
            continue
        sym = f"{c}.TWO" if m in ("上櫃", "興櫃") else f"{c}.TW"
        try:
            y = yahoo_chart(sym, "1d", "5m", cache=False)
            bars = y["bars"]
            out[c] = {"price": y["price"], "prev": y["prev_close"],
                      "open": bars[0]["open"] if bars else None,
                      "high": max((b["high"] for b in bars if b["high"]), default=None),
                      "low": min((b["low"] for b in bars if b["low"]), default=None),
                      "volume": sum((b["volume"] or 0) for b in bars), "time": y["time"].strftime("%H:%M"),
                      "src": "Yahoo", "intraday": [round(b["close"], 2) for b in bars]}
        except FetchError:
            continue
    return out

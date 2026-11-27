"""每日晨報主程式。

用法：
  python -m engine.run_daily            # 產生網站資料（site/data/）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import traceback
from pathlib import Path

from . import details, scorecard, sources as src, strategy as stg
from .crypto import decrypt, encrypt
from .indicators import analyze
from .net import FetchError, clear_cache_older_than

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site" / "data"
TW = dt.timezone(dt.timedelta(hours=8))
FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16",
             "2026-10-28", "2026-12-09"]
FOMC_2027 = ["2027-01-27", "2027-03-17", "2027-04-28", "2027-06-16", "2027-07-28", "2027-09-22",
             "2027-11-03", "2027-12-15"]

quality: dict[str, str] = {}   # 資料品質紀錄：名稱 → 🔵A/🟡B/⚪暫缺＋說明


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_tdcc() -> dict:
    """集保千張大戶：抓本週資料，存一份精簡快照到 data/tdcc/，和上週、四週前比較。"""
    snap_dir = ROOT / "data" / "tdcc"
    snap_dir.mkdir(parents=True, exist_ok=True)
    got = try_("集保股權分散（千張大戶）", src.tdcc_holders, fallback=None)
    if got:
        date, cur = got
        f = snap_dir / f"{date}.json"
        if not f.exists():
            f.write_text(json.dumps({c: [v["b"], v["b4"], v["r"], v["n"]] for c, v in cur.items()
                                     if len(c) in (4, 5, 6)}, separators=(",", ":")), encoding="utf-8")
    files = sorted(snap_dir.glob("*.json"))
    for old in files[:-10]:
        old.unlink()
    files = files[-10:]
    if not files:
        return {}
    load = lambda p: json.loads(p.read_text(encoding="utf-8"))
    cur, date = load(files[-1]), files[-1].stem
    prev = load(files[-2]) if len(files) >= 2 else {}
    m4 = load(files[-5]) if len(files) >= 5 else {}
    out = {}
    for c, (b, b4, r, n) in cur.items():
        o = {"date": date, "b": b, "b4": b4, "r": r, "n": n}
        if c in prev:
            pb, _, pr, pn = prev[c]
            o.update(b_chg=round(b - pb, 2), r_chg=round(r - pr, 2), n_chg=round((n / pn - 1) * 100, 2) if pn else None,
                     prev=files[-2].stem)
        if c in m4:
            o["b_chg4"] = round(b - m4[c][0], 2)
        out[c] = o
    return out


def try_(name, fn, grade="🔵A", fallback=None):
    try:
        v = fn()
        quality[name] = grade
        return v
    except Exception as e:  # noqa: BLE001
        quality[name] = f"⚪暫缺（{str(e)[:60]}）"
        log(f"[暫缺] {name}: {e}")
        return fallback


def first_ok(name, fn, dates, grade="🔵A"):
    """依序嘗試多個日期（今天沒公布就用前一日），回傳 (資料, 使用日期)。"""
    for i, d in enumerate(dates):
        try:
            v = fn(d)
            quality[name] = grade + ("" if i == 0 else f"（沿用 {d.isoformat()}）")
            return v, d
        except FetchError as e:
            log(f"[{name}] {d} {e}")
    quality[name] = "⚪暫缺"
    return None, None


def months_back(d: dt.date, n: int) -> list[dt.date]:
    out, y, m = [], d.year, d.month
    for _ in range(n):
        out.append(dt.date(y, m, 1))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def third_wednesday(y: int, m: int) -> dt.date:
    d = dt.date(y, m, 15)
    while d.weekday() != 2:
        d += dt.timedelta(days=1)
    return d


def load_holdings() -> dict:
    pw = os.environ.get("STOCK_PASSWORD")
    enc = ROOT / "data" / "holdings.enc.json"
    if pw and enc.exists():
        return decrypt(json.loads(enc.read_text(encoding="utf-8")), pw)
    plain = ROOT / "private" / "holdings.json"
    if plain.exists():
        return json.loads(plain.read_text(encoding="utf-8-sig"))
    return {"holdings": [], "history": []}


def build(now: dt.datetime) -> dict:
    today = now.date()
    # ── 1. 交易日與大盤（官方 FMTQIK）
    rows = []
    for mf in months_back(today, 5):
        rows += try_(f"FMTQIK {mf:%Y-%m}", lambda mf=mf: src.twse_fmtqik(mf), fallback=[]) or []
    rows = sorted({r["date"]: r for r in rows if r["index"]}.values(), key=lambda r: r["date"])
    if len(rows) < 25:
        raise SystemExit("大盤資料不足，終止（不臆造）")
    days = [r["date"] for r in rows]
    basis = days[-1]
    prev = days[-2]
    idx_closes = [r["index"] for r in rows]
    basis_pct = (idx_closes[-1] / idx_closes[-2] - 1) * 100
    report_date = today if today > basis else basis + dt.timedelta(days=1)
    while report_date.weekday() >= 5:
        report_date += dt.timedelta(days=1)
    log(f"資料基準 {basis}，報告日 {report_date}")
    D3 = days[-3:]

    # ── 2. 上市資料
    mi = src.twse_mi_index(basis)
    quality["MI_INDEX 收盤行情"] = "🔵A"
    t86 = {d: try_(f"T86 法人 {d}", lambda d=d: src.twse_t86(d), fallback={}) for d in D3}
    dtr, dtr_d = first_ok("TWTB4U 當沖", src.twse_daytrade, [basis, prev])
    def _sbl(d):
        x = src.twse_sbl(d)
        if not x:
            raise FetchError("尚未公布")
        return x
    sbl, _ = first_ok("TWT93U 借券", _sbl, [basis, prev])
    mg, mg_d = first_ok("MI_MARGN 融資", src.twse_margin, [basis, prev])
    val, _ = first_ok("BWIBBU 本益比", src.twse_valuation, [basis, prev])
    disp = try_("處置股（上市）", src.twse_disposal, fallback={}) or {}
    comp = try_("上市產業別", src.twse_companies, fallback={}) or {}
    rev = try_("月營收", src.monthly_revenue, fallback={}) or {}
    fin = try_("季財報 EPS／毛利率", src.financials, fallback={}) or {}
    tdcc = load_tdcc()

    # ── 3. 上櫃／興櫃
    tq = try_("上櫃行情", src.tpex_quotes, fallback={}) or {}
    tq = {k: v for k, v in tq.items() if v.get("date") == basis}
    if not tq:
        quality["上櫃行情"] = "⚪暫缺（日期與基準日不符）"
    tins = {d: try_(f"上櫃法人 {d}", lambda d=d: src.tpex_insti_dated(d), fallback={}) for d in D3}
    tval = try_("上櫃本益比", src.tpex_valuation, fallback={}) or {}
    tdisp = try_("處置股（上櫃）", src.tpex_disposal, fallback={}) or {}
    tcomp = try_("上櫃產業別", src.tpex_companies, fallback={}) or {}
    emg = try_("興櫃行情", src.emerging_quotes, fallback={}) or {}
    disposal = {**disp, **tdisp}

    # ── 4. 期貨、海外、新聞
    oi = try_("期交所外資未平倉", lambda: src.taifex_foreign_oi(basis))
    night_d = today if now.hour < 15 and today > basis else basis + dt.timedelta(days=1)
    night = try_("台指夜盤", lambda: src.taifex_night(night_d))
    if night is None and night_d != basis:
        night = try_("台指夜盤", lambda: src.taifex_night(basis))
    glob = try_("海外市場", src.global_markets, grade="🟡B", fallback={}) or {}
    twii = try_("加權 200 日線", lambda: src.yahoo_chart("^TWII", "2y", cache=False), grade="🟡B")

    # ── 5. 組合個股宇宙（上市＋上櫃普通股）
    stocks: dict[str, dict] = {}
    for code, q in list(mi["stocks"].items()) + list(tq.items()):
        is_etf = code.startswith("00") and 4 <= len(code) <= 6
        if not (src.is_common_stock(code) or is_etf) or not q.get("close") or not q.get("volume"):
            continue
        s = dict(q)
        s["etf"] = is_etf
        prevc = s["close"] - (s.get("change") or 0)
        s["pct"] = round((s["close"] / prevc - 1) * 100, 2) if prevc else None
        if s["market"] == "上市":
            hist = [((t86.get(d) or {}).get(code) or {}).get("total") for d in D3]
            today_ins = (t86.get(basis) or {}).get(code) or {}
            s["industry"] = (comp.get(code) or {}).get("industry_code")
            if dtr:
                x = dtr["by_code"].get(code)
                if x:
                    s["dt_ratio"] = round(x["shares"] / s["volume"] * 100, 1)
                    s["dt_suspended"] = x["suspended"]
            if sbl and code in sbl:
                s["sbl_ratio"] = round(sbl[code]["sbl_sell"] / s["volume"] * 100, 1)
            if val and code in val:
                s.update({k: val[code][k] for k in ("pe", "pb", "yield")})
            if mg and code in mg["by_code"]:
                x = mg["by_code"][code]
                if x["margin_today"] is not None and x["margin_prev"] is not None:
                    s["margin_chg"] = x["margin_today"] - x["margin_prev"]
        else:
            hist = [((tins.get(d) or {}).get(code) or {}).get("total") for d in D3]
            today_ins = (tins.get(basis) or {}).get(code) or {}
            s["industry"] = (tcomp.get(code) or {}).get("industry_code")
            if code in tval:
                s.update({k: tval[code][k] for k in ("pe", "pb", "yield")})
        s["insti_hist"] = hist
        src_ins = t86 if s["market"] == "上市" else tins
        s["insti_days"] = [{"d": d.isoformat(), **{k: round(v / 1000) for k, v in ((src_ins.get(d) or {}).get(code) or {}).items()
                                                  if k in ("foreign", "trust", "dealer", "total")}} for d in D3]
        s["insti"] = today_ins.get("total")
        s["foreign"], s["trust"] = today_ins.get("foreign"), today_ins.get("trust")
        r = rev.get(code)
        if r:
            s["rev_yoy"], s["rev_ytd"], s["rev_mom"], s["rev_ym"] = r["yoy"], r["ytd_yoy"], r["mom"], r["ym"]
        stocks[code] = s
    # 興櫃：只有基本報價（不做技術分析），讓「查個股」找得到
    for code, e in emg.items():
        if code not in stocks and src.is_common_stock(code) and e.get("close"):
            stocks[code] = {**e, "pct": round((e["close"] / e["prev_avg"] - 1) * 100, 2) if e.get("prev_avg") else None,
                            "gates": [], "insti_hist": [], "industry": None}
    log(f"宇宙 {len(stocks)} 檔（上市 {sum(1 for s in stocks.values() if s['market']=='上市')}，"
        f"ETF {sum(1 for s in stocks.values() if s.get('etf'))}，興櫃 {sum(1 for s in stocks.values() if s['market']=='興櫃')}）")
    name_map = {s["name"].replace("*", "").replace("-KY", ""): c for c, s in stocks.items()
                if len(s["name"]) >= 2 and s["market"] != "興櫃" and not s.get("etf")}
    news = try_("財經新聞", lambda: src.news(name_map=name_map), grade="🟡B", fallback=[]) or []

    for code, s in stocks.items():
        if fin.get(code):
            s["fin"] = fin[code]
        if tdcc.get(code):
            s["tdcc"] = tdcc[code]

    gate_stats = stg.apply_gates({c: s for c, s in stocks.items() if not s.get("etf") and s["market"] != "興櫃"}, disposal)
    stg.apply_gates({c: s for c, s in stocks.items() if s.get("etf")}, disposal)   # ETF 也標門檻，但不算進統計

    # ── 6. 持股
    hold = load_holdings()
    hlist = hold.get("holdings", [])
    for h in hlist:
        c = h.get("stock_id")
        if c not in stocks and c in emg:
            e = emg[c]
            stocks[c] = {**e, "pct": round((e["close"] / e["prev_avg"] - 1) * 100, 2) if e.get("prev_avg") and e.get("close") else None,
                         "gates": [], "insti_hist": [], "_held_only": True}
        elif c not in stocks and c in mi["stocks"]:   # ETF 等
            q = dict(mi["stocks"][c])
            prevc = q["close"] - (q.get("change") or 0)
            q["pct"] = round((q["close"] / prevc - 1) * 100, 2) if prevc else None
            stocks[c] = {**q, "gates": [], "insti_hist": [], "_held_only": True}

    # ── 7. 技術面（Yahoo 1 年日K，🟡B；最後收盤與官方比對）
    need = {c for c, s in stocks.items() if s["market"] != "興櫃"} | {h.get("stock_id") for h in hlist}
    log(f"抓技術面 {len(need)} 檔")
    mism = 0
    for c in sorted(need):
        s = stocks.get(c)
        if not s:
            continue
        sym = f"{c}.TWO" if s["market"] in ("上櫃", "興櫃") else f"{c}.TW"
        try:
            bars = src.yahoo_chart(sym, "1y")["bars"]
        except FetchError:
            continue
        bars = [b for b in bars if b["date"] <= basis and b["high"] and b["low"]]
        if bars and bars[-1]["date"] == basis and s.get("close"):
            if abs(bars[-1]["close"] / s["close"] - 1) > 0.006:
                mism += 1
            bars[-1] = {**bars[-1], "close": s["close"], "high": s.get("high") or bars[-1]["high"],
                        "low": s.get("low") or bars[-1]["low"], "open": s.get("open") or bars[-1]["open"]}
        elif bars and s.get("close") and s.get("high"):
            bars.append({"date": basis, "open": s["open"], "high": s["high"], "low": s["low"],
                         "close": s["close"], "volume": s.get("volume")})
        s["tech"] = analyze(bars)
        s["_bars"] = bars[-130:]
    quality["個股技術面"] = f"🟡B Yahoo（收盤已以官方覆寫；差異>0.6% 共 {mism} 檔）"

    # ── 8. 大盤判讀
    sox = glob.get("^SOX") or {}
    ma200 = None
    if twii and len(twii["bars"]) >= 200:
        cl = [b["close"] for b in twii["bars"] if b["date"] <= basis]
        ma200 = sum(cl[-200:]) / 200
    regime = stg.market_regime(idx_closes, basis_pct, mi["breadth"], (oi or {}).get("外資", {}).get("oi_net"),
                               sox.get("down3"), event_day=False, ma200=ma200)
    scorecard.PRELOADED.update({c: s["_bars"] for c, s in stocks.items() if s.get("_bars")})
    sel, dtrec = scorecard.load()
    n1 = try_("中長期補分", lambda: scorecard.score_selection(sel, today), fallback=0)
    n2 = try_("當沖補分", lambda: scorecard.score_daytrade(dtrec, today), fallback=0)
    st_mid = scorecard.stats(sel, "中長期")
    st_cmp = scorecard.stats(sel, "綜合評分")
    st_dt = scorecard.stats(dtrec, "當沖")
    brk = scorecard.breaker(st_mid)

    # ── 9. 選股
    stg.composite_scores(stocks)
    for c in stocks.values():
        c["_disposal"] = disposal
    mid = stg.midterm_picks(stocks, regime, brk["on"])
    cmp_ = stg.composite_picks(stocks, regime, brk["on"])
    dtp = stg.daytrade_picks(stocks, regime)
    vp = stg.value_picks(stocks)
    for c in stocks.values():
        c.pop("_disposal", None)

    # ── 10. 記錄今日推薦（之後真實記分）
    if today >= basis:
        scorecard.append(sel, report_date, basis, "中長期", mid, regime["longterm"]["label"])
        scorecard.append(sel, report_date, basis, "綜合評分", cmp_[:5], regime["longterm"]["label"])
        for p in dtp["picks"]:
            p["market"] = stocks[p["code"]]["market"]
        scorecard.append(dtrec, report_date, basis, "當沖", dtp["picks"], dtp["verdict"]["label"])
        scorecard.save(sel, dtrec)

    # ── 10b. 個股詳細頁資料（K線、籌碼、營收、交易計畫）
    held = {h.get("stock_id") for h in hlist}
    featured = {p["code"] for p in mid + cmp_ + vp + dtp["picks"] + dtp.get("watch", [])}
    pub_details, priv_details = {}, {}
    for c, s in stocks.items():
        if not s.get("_bars"):
            continue
        d = details.build(s, regime, basis)
        if not s.get("_held_only") and (s["market"] != "興櫃" or c in featured):
            pub_details[c] = d
        elif c in held:          # 只因為持有才有的標的 → 放進加密檔，避免外洩持股
            priv_details[c] = d
    details.write_public(pub_details)

    # ── 11. 持股健診
    pf = stg.portfolio_review(hlist, stocks, regime, hold.get("cash")) if hlist else None

    # ── 12. 事件雷達
    events = []
    for f in FOMC_2026 + FOMC_2027:
        fd = dt.date.fromisoformat(f)
        if 0 <= (fd - report_date).days <= 14:
            events.append(f"{fd.month}/{fd.day} 美國 FOMC 利率決議（台灣時間隔日凌晨）")
    sw = third_wednesday(report_date.year, report_date.month)
    if sw < report_date:
        nm = report_date.replace(day=1) + dt.timedelta(days=32)
        sw = third_wednesday(nm.year, nm.month)
    if (sw - report_date).days <= 7:
        events.append(f"{sw.month}/{sw.day} 台指期結算{'（今天）' if sw == report_date else ''}")
    if report_date.day <= 10:
        events.append("每月 10 日前公布上月營收")

    # 市場彙總
    t_b = t86.get(basis) or {}
    ins_tot = {"foreign": sum(v["foreign"] for k, v in t_b.items() if src.is_common_stock(k)),
               "trust": sum(v["trust"] for k, v in t_b.items() if src.is_common_stock(k)),
               "total": sum(v["total"] for k, v in t_b.items() if src.is_common_stock(k))} if t_b else None
    top_buy = sorted((s for s in stocks.values() if s.get("insti") and not s.get("etf")), key=lambda s: -s["insti"])[:8]
    top_sell = sorted((s for s in stocks.values() if s.get("insti") and not s.get("etf")), key=lambda s: s["insti"])[:8]
    margin_chg_e = None
    if mg:
        x = mg["summary"].get("融資金額(仟元)")
        if x and x["today"] and x["prev"]:
            margin_chg_e = (x["today"] - x["prev"]) / 1e5
    amount_e = rows[-1]["amount"] / 1e8 if rows[-1]["amount"] else None
    index_hist = [{"d": r["date"].isoformat(), "c": r["index"], "a": round((r["amount"] or 0) / 1e8)} for r in rows[-80:]]

    # 個股總表（網站搜尋用）
    table = []
    for s in stocks.values():
        if s.get("_held_only"):      # 只因持有才加入的標的（興櫃、ETF）不放進公開總表
            continue
        t = s.get("tech") or {}
        table.append({"c": s["code"], "n": s["name"], "m": {"上市": "市", "上櫃": "櫃", "興櫃": "興"}.get(s["market"], "市"),
                      "e": 1 if s.get("etf") else 0, "i": "ETF" if s.get("etf") else stg.INDUSTRY.get(s.get("industry"), ""),
                      "p": s["close"], "chg": s.get("pct"), "amt": round((s.get("amount") or 0) / 1e8, 2),
                      "ins": round((s.get("insti") or 0) / 1000), "sc": s.get("score"), "rs": s.get("rs"),
                      "pe": s.get("pe"), "y": s.get("yield"), "yoy": round(s["rev_yoy"], 1) if s.get("rev_yoy") is not None else None,
                      "g": s.get("gates", []), "ma20": round(t["ma20"], 2) if t.get("ma20") else None,
                      "rsi": round(t["rsi"]) if t.get("rsi") else None,
                      "eps": (s.get("fin") or {}).get("eps"), "gm": (s.get("fin") or {}).get("gm"),
                      "bh": (s.get("tdcc") or {}).get("b"), "bc": (s.get("tdcc") or {}).get("b_chg")})
    table.sort(key=lambda r: -(r["amt"] or 0))

    rep = {
        "generated_at": now.isoformat(timespec="minutes"), "basis": basis.isoformat(),
        "report_date": report_date.isoformat(), "version": "SKILL v4.5.1 + 多因子 v1",
        "market": {"regime": regime, "amount_e": amount_e, "index_hist": index_hist,
                   "sectors": stg.sector_heat(mi["sectors"]), "insti_total": ins_tot,
                   "top_buy": [{"code": s["code"], "name": s["name"], "lots": round(s["insti"] / 1000)} for s in top_buy],
                   "top_sell": [{"code": s["code"], "name": s["name"], "lots": round(s["insti"] / 1000)} for s in top_sell],
                   "margin_chg_e": margin_chg_e, "daytrade_market_pct": (dtr or {}).get("market_pct"),
                   "night": night, "taifex": oi, "gates": gate_stats,
                   "disposal": [{"code": k, **v} for k, v in disposal.items() if src.is_common_stock(k)]},
        "global": glob, "news": news[:120], "events": events,
        "breaker": brk, "midterm": mid, "composite": cmp_, "daytrade": dtp, "value": vp,
        "stats": {"中長期": st_mid, "綜合評分": st_cmp, "當沖": st_dt,
                  "monthly": scorecard.monthly(sel, "中長期"), "scored_today": {"selection": n1, "daytrade": n2}},
        "quality": quality, "table": table,
    }
    hold["_details"] = priv_details
    return rep, pf, hold


def write(rep: dict, pf: dict | None, hold: dict) -> None:
    SITE.mkdir(parents=True, exist_ok=True)
    pw = os.environ.get("STOCK_PASSWORD")
    priv_details = hold.pop("_details", {})
    private = {"portfolio": pf, "history": hold.get("history", []), "holdings_raw": hold, "details": priv_details}
    if pw:
        (SITE / "private.enc.json").write_text(json.dumps(encrypt(private, pw)), encoding="utf-8")
        (SITE / "private.json").unlink(missing_ok=True)
    else:
        # 沒設密碼＝本機/私人主機模式；此檔已列入 .gitignore，不會上傳
        (SITE / "private.json").write_text(json.dumps(private, ensure_ascii=False, default=str), encoding="utf-8")
    table = rep.pop("table", [])
    (SITE / "stocks.json").write_text("[\n" + ",\n".join(json.dumps(r, ensure_ascii=False) for r in table) + "\n]",
                                      encoding="utf-8")
    (SITE / "report.json").write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    rep["table"] = table
    daily = ROOT / "data" / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    slim = {k: rep[k] for k in ("basis", "report_date", "breaker", "midterm", "composite", "daytrade", "value")}
    slim["regime"] = rep["market"]["regime"]
    (daily / f"{rep['basis']}.json").write_text(json.dumps(slim, ensure_ascii=False, default=str), encoding="utf-8")


def main():
    argparse.ArgumentParser(description="產生每日網站資料").parse_args()
    now = dt.datetime.now(TW)
    clear_cache_older_than(10)
    rep, pf, hold = build(now)
    write(rep, pf, hold)
    log("完成")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)

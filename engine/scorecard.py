"""回測記分：每天的推薦都留紀錄，之後用真實 K 線打分數。這是判斷「方法有沒有效」的唯一依據。

中長期／綜合評分：進場價＝資料基準日收盤；之後 5 個交易日內先碰 +5% 記 win、先碰 -7% 記 loss；
               同日都碰以末日收盤判；都沒碰則末日收盤 > 進場記「小勝」，否則「小負」。
當沖：當日真實；進場＝當日開盤；高≥+3% 記 win、低≤-2% 記 loss；都碰標 ambiguous；都沒碰以收盤判。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from statistics import mean

from . import sources as src
from .net import FetchError

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "data" / "scorecard"
NORM = {"small_win": "小勝", "small_loss": "小負", "flat": "小負"}
WIN_SET = {"win", "小勝"}


def _load(name: str, legacy: str) -> list[dict]:
    p = DIR / name
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    lp = DIR / legacy
    if not lp.exists():
        return []
    recs = json.loads(lp.read_text(encoding="utf-8-sig"))
    out = []
    for r in recs:
        if r.get("outcome") in NORM:
            r["outcome"] = NORM[r["outcome"]]
        r.setdefault("type", "當沖" if "dayHigh" in r else r.get("type", "中長期"))
        r["legacy"] = True
        out.append(r)
    return out


def load():
    return _load("selection.json", "selection-legacy.json"), _load("daytrade.json", "daytrade-legacy.json")


def save(sel: list[dict], day: list[dict]) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / "selection.json").write_text(json.dumps(sel, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (DIR / "daytrade.json").write_text(json.dumps(day, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


# ───────── K 線來源：上市用證交所 STOCK_DAY（官方），其餘用 Yahoo ─────────
_bar_cache: dict = {}
PRELOADED: dict = {}   # run_daily 已經抓好的日 K（含官方收盤覆寫），有就直接用，不再逐檔打證交所


def bars_after(code: str, market: str, start: dt.date) -> list[dict]:
    key = (code, market, start)
    if key in _bar_cache:
        return _bar_cache[key]
    bars = list(PRELOADED.get(code) or [])
    if bars and bars[0]["date"] > start:
        bars = []
    if not bars and market in (None, "", "上市"):
        m = dt.date(start.year, start.month, 1)
        today = dt.date.today()
        while m <= today:
            bars += src.twse_stock_day(code, m)
            m = dt.date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    if not bars:
        try:
            sym = f"{code}.TWO" if market in ("上櫃", "興櫃") else f"{code}.TW"
            bars = src.yahoo_chart(sym, "3mo", cache=False)["bars"]
        except FetchError:
            bars = []
    bars = [b for b in bars if b["date"] >= start and b.get("high") and b.get("low")]
    _bar_cache[key] = bars
    return bars


def _ret(r: dict) -> float | None:
    """單筆實現報酬（%）。"""
    o = r.get("outcome")
    if o == "win":
        return 5.0
    if o == "loss":
        return -7.0
    e, c = r.get("entry") or r.get("entryClose"), r.get("win5Close")
    if e and c:
        return (c / e - 1) * 100
    if r.get("d5ret") is not None:
        return r["d5ret"] * 100
    return None


def score_selection(recs: list[dict], today: dt.date) -> int:
    n = 0
    for r in recs:
        if r.get("scored") or not r.get("entry") or "target" not in r:
            continue
        basis = dt.date.fromisoformat(r["basis"])
        start = dt.date.fromisoformat(r["entry_date"]) + dt.timedelta(days=1) if r.get("entry_date") else basis
        try:
            bars = bars_after(r["code"], r.get("market"), start)[:5]
        except FetchError:
            continue
        if len(bars) < 5 or bars[-1]["date"] >= today and today.weekday() < 5 and dt.datetime.now().hour < 14:
            continue
        e, tgt, stp = r["entry"], r["target"], r["stopPrice"]
        outcome = None
        for b in bars:
            ht, hs = b["high"] >= tgt, b["low"] <= stp
            if ht and hs:
                outcome = "ambiguous"
                break
            if ht:
                outcome = "win"
                break
            if hs:
                outcome = "loss"
                break
        last = bars[-1]["close"]
        if outcome == "ambiguous":
            outcome = "小勝" if last > e else "小負"
        if outcome is None:
            outcome = "小勝" if last > e else "小負"
        r.update({"win5High": max(b["high"] for b in bars), "win5Low": min(b["low"] for b in bars),
                  "win5Close": last, "hitTarget": max(b["high"] for b in bars) >= tgt,
                  "hitStop": min(b["low"] for b in bars) <= stp, "outcome": outcome,
                  "maxRet": round(max(b["high"] for b in bars) / e - 1, 4), "scored": True,
                  "scoredOn": today.isoformat(), "d5ret": round(last / e - 1, 4)})
        n += 1
    return n


def score_daytrade(recs: list[dict], today: dt.date) -> int:
    n = 0
    for r in recs:
        if r.get("scored"):
            continue
        basis = dt.date.fromisoformat(r["basis"])
        if basis > today or (basis == today and dt.datetime.now().hour < 14):
            continue
        try:
            bars = [b for b in bars_after(r["code"], r.get("market"), basis) if b["date"] == basis]
        except FetchError:
            continue
        if not bars:
            if (today - basis).days > 5:
                r.update({"scored": True, "outcome": "no_data"})
            continue
        b = bars[0]
        e = r.get("entry_actual") or b["open"]
        h3, s2 = b["high"] >= e * 1.03, b["low"] <= e * 0.98
        if h3 and s2:
            o = "ambiguous"
        elif h3:
            o = "win"
        elif s2:
            o = "loss"
        else:
            o = "小勝" if b["close"] > e else "小負"
        r.update({"entry": e, "dayHigh": b["high"], "dayLow": b["low"], "dayClose": b["close"],
                  "hit3": h3, "stop2": s2, "outcome": o, "dayRet": round(b["close"] / e - 1, 4), "scored": True})
        n += 1
    return n


def stats(recs: list[dict], typ: str | None = None, last: int = 20) -> dict:
    xs = [r for r in recs if r.get("scored") and r.get("outcome") in ("win", "loss", "小勝", "小負", "ambiguous")
          and (typ is None or r.get("type") == typ)]
    xs.sort(key=lambda r: (r.get("basis", ""), r.get("code", "")))
    xs = xs[-last:]
    if not xs:
        return {"n": 0}
    if typ == "當沖":
        rets = [(r.get("dayRet") or 0) * 100 for r in xs]
        wins = [r for r in xs if r["outcome"] in WIN_SET or (r["outcome"] == "ambiguous" and (r.get("dayRet") or 0) > 0)]
    else:
        rets = [x for x in (_ret(r) for r in xs) if x is not None]
        wins = [r for r in xs if r["outcome"] in WIN_SET]
    pos, neg = [x for x in rets if x > 0], [x for x in rets if x <= 0]
    wr = len(wins) / len(xs)
    aw, al = (mean(pos) if pos else 0.0), (mean(neg) if neg else 0.0)
    exp = wr * aw - (1 - wr) * abs(al)
    streak = 0
    for r in reversed(xs):
        if r["outcome"] in ("loss", "小負"):
            streak += 1
        else:
            break
    return {"n": len(xs), "win_rate": round(wr * 100, 1), "avg_win": round(aw, 2), "avg_loss": round(al, 2),
            "payoff": round(aw / abs(al), 2) if al else None, "expectancy": round(exp, 2),
            "avg_ret": round(mean(rets), 2) if rets else None, "loss_streak": streak,
            "last_outcome": xs[-1]["outcome"],
            "series": [{"basis": r["basis"], "code": r["code"], "name": r.get("name"), "outcome": r["outcome"],
                        "ret": round(_ret(r) if typ != "當沖" else (r.get("dayRet") or 0) * 100, 2)} for r in xs]}


def breaker(st: dict) -> dict:
    """期望值斷路器（v4.4.1 B）。"""
    if not st.get("n"):
        return {"on": False, "why": "尚無記分資料"}
    trig = st["expectancy"] < 0 or st["loss_streak"] >= 4
    release = st["last_outcome"] == "win"
    on = trig and not release
    why = []
    if st["expectancy"] < 0:
        why.append(f"近{st['n']}筆期望值 {st['expectancy']:+.2f}%")
    if st["loss_streak"] >= 4:
        why.append(f"連續 {st['loss_streak']} 筆 loss/小負")
    return {"on": on, "why": "、".join(why) if on else ("最新一筆為 win，已解除" if trig else "期望值為正"),
            "text": ("⛔ 斷路器啟動：暫停中長期新單，只觀察與降風險" if on else "✅ 斷路器未啟動")}


def monthly(recs: list[dict], typ: str) -> list[dict]:
    by = {}
    for r in recs:
        if r.get("type") != typ or not r.get("scored") or r.get("outcome") not in ("win", "loss", "小勝", "小負"):
            continue
        m = r["basis"][:7]
        by.setdefault(m, []).append(r)
    out = []
    for m in sorted(by):
        xs = by[m]
        w = sum(1 for r in xs if r["outcome"] in WIN_SET)
        rets = [x for x in (_ret(r) for r in xs) if x is not None]
        out.append({"month": m, "n": len(xs), "win_rate": round(w / len(xs) * 100, 1),
                    "avg_ret": round(mean(rets), 2) if rets else None})
    return out


def append(recs: list[dict], basis: dt.date, entry_date: dt.date, typ: str, picks: list[dict], note: str = "") -> int:
    have = {(r.get("basis"), r.get("type"), r.get("code")) for r in recs}
    n = 0
    for p in picks:
        k = (basis.isoformat(), typ, p["code"])
        if k in have:
            continue
        e = p["close"]
        rec = {"basis": basis.isoformat(), "entry_date": entry_date.isoformat(), "type": typ, "code": p["code"],
               "name": p["name"], "market": p.get("market", "上市"), "entry": e, "scored": False,
               "status": p.get("status") or p.get("action") or p.get("verdict", ""), "note": note}
        if typ != "當沖":
            rec.update({"target": round(e * 1.05, 3), "stopPrice": round(e * 0.93, 3)})
        recs.append(rec)
        n += 1
    return n

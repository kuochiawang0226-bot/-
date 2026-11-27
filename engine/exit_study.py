"""出場規則回測：用過去所有中長期推薦紀錄，比較不同停利／停損／持有天數的真實成績。

python -m engine.exit_study   → 結果寫到 data/exit_study.json（網站「勝率回測」頁會顯示）
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from statistics import mean

from . import sources as src
from .indicators import atr, sma, rsi
from .net import FetchError

ROOT = Path(__file__).resolve().parent.parent
COST = 0.4   # 來回手續費＋證交稅（%）

RULES = [
    {"id": "A", "name": "停利 +5%／停損 −7%（現行）", "tp": 5, "sl": 7, "days": 5},
    {"id": "B", "name": "停利 +10%／停損 −5%", "tp": 10, "sl": 5, "days": 10},
    {"id": "C", "name": "停利 +8%／停損 −4%", "tp": 8, "sl": 4, "days": 10},
    {"id": "D", "name": "停利 +15%／停損 −5%", "tp": 15, "sl": 5, "days": 15},
    {"id": "E", "name": "ATR：停損 1.5 倍、停利 3 倍", "atr_sl": 1.5, "atr_tp": 3, "days": 10},
    {"id": "F", "name": "移動停利：停損 −5%，從高點回落 2 倍 ATR 出場", "sl": 5, "trail_atr": 2, "days": 20},
    {"id": "G", "name": "對照組：不停利停損，抱 5 天", "days": 5, "base": True},
    {"id": "H", "name": "對照組：不停利停損，抱 20 天", "days": 20, "base": True},
]


def bars_for(code: str) -> list[dict]:
    for sym in (f"{code}.TW", f"{code}.TWO"):
        try:
            b = src.yahoo_chart(sym, "1y")["bars"]
            if b:
                return [x for x in b if x["high"] and x["low"]]
        except FetchError:
            continue
    return []


def simulate(rule: dict, entry: float, before: list[dict], after: list[dict]) -> float | None:
    if not after:
        return None
    a = atr(before[-30:]) if len(before) > 15 else None
    if rule.get("base"):
        w = after[:rule["days"]]
        return (w[-1]["close"] / entry - 1) * 100 - COST
    if rule.get("atr_sl"):
        if not a:
            return None
        sl, tp = entry - rule["atr_sl"] * a, entry + rule["atr_tp"] * a
    else:
        sl = entry * (1 - rule["sl"] / 100)
        tp = entry * (1 + rule["tp"] / 100) if rule.get("tp") else None
    peak = entry
    window = after[:rule["days"]]
    for b in window:
        if b["low"] <= sl:                       # 同一天都碰到，保守當作先停損
            return (sl / entry - 1) * 100 - COST
        if tp and b["high"] >= tp:
            return (tp / entry - 1) * 100 - COST
        if rule.get("trail_atr") and a:
            peak = max(peak, b["close"])
            sl = max(sl, peak - rule["trail_atr"] * a)
    return (window[-1]["close"] / entry - 1) * 100 - COST


FILTERS = [
    {"id": "all", "name": "全部推薦（不過濾）"},
    {"id": "mkt", "name": "只在大盤站上月線時進場"},
    {"id": "trend", "name": "個股在月線之上且月線上揚"},
    {"id": "notext", "name": "不追高：股價離月線 < 8%"},
    {"id": "rsi", "name": "RSI 介於 50~70（強但不過熱）"},
    {"id": "combo", "name": "大盤月線之上＋個股趨勢＋不追高"},
]


def passes(fid: str, before: list[dict], mkt_ok: bool) -> bool:
    c = [b["close"] for b in before]
    if len(c) < 25:
        return fid == "all"
    m20, m20p = sma(c, 20), sma(c[:-5], 20)
    trend = c[-1] > m20 and m20 > m20p
    notext = c[-1] / m20 < 1.08
    if fid == "mkt":
        return mkt_ok
    if fid == "trend":
        return trend
    if fid == "notext":
        return notext
    if fid == "rsi":
        r = rsi(c)
        return r is not None and 50 <= r <= 70
    if fid == "combo":
        return mkt_ok and trend and notext
    return True


def summarize(xs: list[float]) -> dict:
    w, l = [x for x in xs if x > 0], [x for x in xs if x <= 0]
    return {"n": len(xs), "win_rate": round(len(w) / len(xs) * 100, 1) if xs else 0,
            "avg_win": round(mean(w), 2) if w else 0, "avg_loss": round(mean(l), 2) if l else 0,
            "payoff": round(mean(w) / abs(mean(l)), 2) if w and l else None,
            "expectancy": round(mean(xs), 2) if xs else 0, "total": round(sum(xs), 1)}


def main():
    recs = json.loads((ROOT / "data/scorecard/selection.json").read_text(encoding="utf-8-sig"))
    seen, trades = set(), []
    for r in recs:
        e = r.get("entry") or r.get("entryClose")
        key = (r.get("basis"), r.get("code"))
        if not e or not r.get("basis") or key in seen:
            continue
        seen.add(key)
        trades.append((dt.date.fromisoformat(r["basis"]), r["code"], float(e)))
    cache, results = {}, {x["id"]: [] for x in RULES}
    twii = [b for b in src.yahoo_chart("^TWII", "1y")["bars"] if b["close"]]
    fres = {(f["id"], r["id"]): [] for f in FILTERS for r in RULES}
    for basis, code, e in trades:
        if code not in cache:
            cache[code] = bars_for(code)
        bars = cache[code]
        before = [b for b in bars if b["date"] < basis]
        after = [b for b in bars if b["date"] >= basis]
        if len(after) < 3:
            continue
        tc = [b["close"] for b in twii if b["date"] < basis]
        mkt_ok = len(tc) > 20 and tc[-1] > sma(tc, 20)
        for rule in RULES:
            ret = simulate(rule, e, before, after)
            if ret is not None:
                results[rule["id"]].append(ret)
                for f in FILTERS:
                    if passes(f["id"], before, mkt_ok):
                        fres[(f["id"], rule["id"])].append(ret)
    out = []
    for rule in RULES:
        xs = results[rule["id"]]
        if not xs:
            continue
        w, l = [x for x in xs if x > 0], [x for x in xs if x <= 0]
        out.append({**{k: rule[k] for k in ("id", "name", "days")}, "n": len(xs),
                    "win_rate": round(len(w) / len(xs) * 100, 1), "avg_win": round(mean(w), 2) if w else 0,
                    "avg_loss": round(mean(l), 2) if l else 0,
                    "payoff": round(mean(w) / abs(mean(l)), 2) if w and l else None,
                    "expectancy": round(mean(xs), 2), "total": round(sum(xs), 1)})
    for r in out:
        r["base"] = bool(next(x for x in RULES if x["id"] == r["id"]).get("base"))
    best = max((r for r in out if not r["base"]), key=lambda r: r["expectancy"])
    res = {"generated": dt.date.today().isoformat(), "trades": len(trades), "cost_pct": COST,
           "period": [min(t[0] for t in trades).isoformat(), max(t[0] for t in trades).isoformat()],
           "rules": out, "best": best["id"],
           "filters": [{**f, "by_rule": {r["id"]: summarize(fres[(f["id"], r["id"])]) for r in RULES}} for f in FILTERS]}
    for p in (ROOT / "data/exit_study.json", ROOT / "site/data/exit_study.json"):
        p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in out:
        print(f"{r['id']} {r['name']:<28} 筆數 {r['n']:>3}  勝率 {r['win_rate']:>5}%  賺賠比 {r['payoff']}  期望值 {r['expectancy']:+.2f}%  累計 {r['total']:+.1f}%")
    print("最佳：", best["id"], best["name"])
    for f in res["filters"]:
        a = f["by_rule"]["A"]; g = f["by_rule"]["H"]
        print(f"{f['name']:<24} 筆數 {a['n']:>3}  規則A 勝率 {a['win_rate']:>5}% 期望 {a['expectancy']:+.2f}%  | 抱20天 期望 {g['expectancy']:+.2f}%  | " + " ".join(f"{k}{v['expectancy']:+.2f}" for k, v in f['by_rule'].items()))


if __name__ == "__main__":
    main()

"""當沖盤中即時判斷（沿用 SKILL 當沖 v3 與「盤中確認」排版規則）。

每檔候選用當日 1 分 K 算出：VWAP、開盤區間（前 15 分鐘高低）、量能進度、是否衝高回落，
再給出 ✅可進 / 👀觀察 / ❌不適合，以及進場區、停損、停利與觸發價。只顯示在網站，不推 LINE。
"""
from __future__ import annotations

import datetime as dt

from . import sources as src
from .net import FetchError

OPEN = dt.time(9, 0)
CLOSE = dt.time(13, 30)
SESSION_MIN = 270


def phase(now: dt.datetime) -> dict:
    t = now.time()
    if now.weekday() >= 5:
        return {"key": "closed", "label": "休市", "note": "週末休市，下方為下個交易日的候選與關鍵價位"}
    if t < OPEN:
        return {"key": "pre", "label": "盤前", "note": "09:00 開盤；開盤 15 分鐘內先看方向，不搶進"}
    if t < dt.time(9, 15):
        return {"key": "opening", "label": "開盤 15 分鐘", "note": "先等方向：開盤區間成形、站穩 VWAP 再說"}
    if t < dt.time(13, 0):
        return {"key": "trading", "label": "盤中", "note": "可依訊號分批；破 VWAP 或 -2~3% 立刻出場"}
    if t < CLOSE:
        return {"key": "late", "label": "尾盤", "note": "13:00 後不新開倉，手上部位準備平倉"}
    return {"key": "after", "label": "盤後", "note": "今日已收盤；下方為今日盤中最後狀態，明早更新候選"}


def _yahoo_minutes(code: str, market: str) -> list[dict]:
    """含時間戳的 1 分 K（yahoo_chart 只回日期，這裡另取時間）。"""
    sym = f"{code}.TWO" if market in ("上櫃", "興櫃") else f"{code}.TW"
    j = src.get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1d&interval=1m", cache=False)
    r = (j.get("chart") or {}).get("result") or []
    if not r:
        raise FetchError(f"{sym} 無盤中資料")
    r = r[0]
    q = r["indicators"]["quote"][0]
    tz = dt.timezone(dt.timedelta(seconds=r["meta"].get("gmtoffset", 28800)))
    out = []
    for i, ts in enumerate(r.get("timestamp") or []):
        c = q["close"][i]
        if c is None or q["high"][i] is None:
            continue
        out.append({"t": dt.datetime.fromtimestamp(ts, tz), "o": q["open"][i], "h": q["high"][i], "l": q["low"][i],
                    "c": c, "v": q["volume"][i] or 0})
    return out, r["meta"].get("chartPreviousClose") or r["meta"].get("previousClose")


def evaluate(cand: dict, bars: list[dict], prev_close: float | None, avg_vol: float | None,
             now: dt.datetime, verdict: dict, budget: float = 500_000) -> dict:
    """單檔判斷。bars 為當日 1 分 K（已截到 now）。"""
    out = {"code": cand["code"], "name": cand["name"], "tier": cand.get("tier", ""), "industry": cand.get("industry", ""),
           "prev_close": prev_close or cand.get("close"), "prev_high": cand.get("prev_high"), "prev_low": cand.get("prev_low"),
           "tag": cand.get("tag", "")}
    pc = out["prev_close"]
    if not bars:
        lv = pc or cand.get("close")
        out.update({"status": "wait", "label": "⏳ 尚未開盤／無成交",
                    "levels": {"watch_above": round(lv * 1.005, 2) if lv else None,
                               "avoid_below": round(lv * 0.98, 2) if lv else None},
                    "checks": [], "reasons": ["開盤後站穩 VWAP 且量增才考慮"]})
        return out
    o = bars[0]["o"] or bars[0]["c"]
    last = bars[-1]["c"]
    hi = max(b["h"] for b in bars)
    lo = min(b["l"] for b in bars)
    vol = sum(b["v"] for b in bars)
    pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars)
    vwap = pv / vol if vol else last
    vw_series, cpv, cv = [], 0.0, 0
    for b in bars:
        cpv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
        cv += b["v"]
        vw_series.append(cpv / cv if cv else b["c"])
    streak = 0
    for b, v in zip(reversed(bars), reversed(vw_series)):
        if b["c"] > v:
            streak += 1
        else:
            break
    or_bars = [b for b in bars if b["t"].time() < dt.time(9, 15)]
    or_hi = max((b["h"] for b in or_bars), default=None)
    or_lo = min((b["l"] for b in or_bars), default=None)
    elapsed = max(1, min(SESSION_MIN, (now - now.replace(hour=9, minute=0, second=0, microsecond=0)).seconds // 60))
    pace = (vol / (avg_vol * elapsed / SESSION_MIN)) if avg_vol else None
    chg = (last / pc - 1) * 100 if pc else None
    from_open = (last / o - 1) * 100
    fade = hi > o * 1.02 and (hi - last) / max(hi - o, 1e-9) > 0.5
    limit_locked = chg is not None and chg >= 9.4 and last >= hi
    dist_vwap = (last / vwap - 1) * 100
    t = now.time()

    checks = [
        {"k": "站穩 VWAP（連 5 分鐘以上）", "ok": streak >= 5, "v": f"VWAP {vwap:.2f}｜連 {streak} 分"},
        {"k": "紅 K（現價在開盤價之上）", "ok": last > o, "v": f"開 {o:.2f}"},
        {"k": "量增（量能進度 ≥ 1.2 倍）", "ok": pace is not None and pace >= 1.2, "v": f"{pace:.2f} 倍" if pace else "暫缺"},
        {"k": "未衝高回落", "ok": not fade, "v": f"高 {hi:.2f}"},
        {"k": "未追高（漲幅 < 7%、離 VWAP ≤ 1.5%）", "ok": (chg is None or chg < 7) and dist_vwap <= 1.5,
         "v": f"{chg:+.2f}%｜離 VWAP {dist_vwap:+.2f}%" if chg is not None else f"離 VWAP {dist_vwap:+.2f}%"},
    ]
    if or_hi and t >= dt.time(9, 15):
        checks.append({"k": "突破開盤區間高點", "ok": last > or_hi, "v": f"區間 {or_lo:.2f}–{or_hi:.2f}", "bonus": True})

    core_ok = all(c["ok"] for c in checks if not c.get("bonus"))
    bad = []
    if last < vwap:
        bad.append("跌破 VWAP")
    if from_open <= -2:
        bad.append("跌破開盤 -2%")
    if fade:
        bad.append("開高後回落過半（假突破風險）")
    if limit_locked:
        bad.append("漲停鎖住，買不到也不該追")
    if chg is not None and chg >= 7 and not limit_locked:
        bad.append(f"已漲 {chg:.1f}%，追高風險大")

    if bad:
        status, label = "no", "❌ 不適合"
    elif core_ok:
        status, label = "go", "✅ 可進"
    else:
        status, label = "watch", "👀 觀察"
    if t < dt.time(9, 15) and status == "go":
        status, label = "watch", "👀 觀察（開盤 15 分鐘內不搶）"
    if t >= dt.time(13, 0) and status == "go":
        status, label = "watch", "👀 13:00 後不新開倉"
    blocked = verdict.get("key") == "red"
    if blocked and status == "go":
        label = "✅ 技術面可進｜但今日🔴停做"

    entry_lo, entry_hi = round(vwap, 2), round(vwap * 1.005, 2)
    ref = max(last, vwap) if status == "go" else entry_hi
    stop = round(max(ref * 0.975, vwap * 0.997), 2)
    levels = {"vwap": round(vwap, 2), "entry": [entry_lo, entry_hi], "stop": stop,
              "tp1": round(ref * 1.03, 2), "tp2": round(ref * 1.05, 2),
              "watch_above": round(max(vwap, or_hi or vwap), 2), "avoid_below": round(min(vwap, o * 0.98), 2),
              "or_hi": round(or_hi, 2) if or_hi else None, "or_lo": round(or_lo, 2) if or_lo else None}
    if status == "go":
        reasons = [f"可在 {entry_lo:g}～{entry_hi:g}（VWAP 附近）分批，不追高",
                   f"停損 {stop:g}（破 VWAP 或 -2.5% 全出）", f"停利：半數 {levels['tp1']:g}、其餘移動停利"]
    elif status == "watch":
        miss = [c["k"].split("（")[0] for c in checks if not c["ok"] and not c.get("bonus")]
        reasons = [f"還差：{'、'.join(miss) or '時間條件'}",
                   f"站穩 {levels['watch_above']:g} 以上且量增再看"]
    else:
        reasons = bad + [f"要重新站回 {levels['vwap']:g}（VWAP）並帶量才再評估"]
    # 盤中走勢（每 5 分鐘取樣）
    spark = [round(b["c"], 2) for b in bars[::5]] + [round(last, 2)]
    vspark = [round(v, 2) for v in vw_series[::5]] + [round(vwap, 2)]
    lot_cost = last * 1000
    out.update({"status": status, "label": label, "blocked": blocked, "price": round(last, 2), "open": round(o, 2),
                "high": round(hi, 2), "low": round(lo, 2), "chg": round(chg, 2) if chg is not None else None,
                "vwap": round(vwap, 2), "dist_vwap": round(dist_vwap, 2), "pace": round(pace, 2) if pace else None,
                "streak": streak, "time": bars[-1]["t"].strftime("%H:%M"), "checks": checks, "reasons": reasons,
                "levels": levels, "spark": spark, "vspark": vspark, "lot_cost_w": round(lot_cost / 1e4, 1),
                "lots": int(budget // lot_cost) if lot_cost else 0})
    return out


def monitor(daytrade: dict, now: dt.datetime, avg_vols: dict, asof: dt.time | None = None) -> dict:
    """對觀察名單逐檔判斷。asof 用於回放（例如收盤後模擬 09:45 的狀態）。"""
    verdict = daytrade.get("verdict", {})
    cands = daytrade.get("watch") or daytrade.get("picks") or []
    eval_now = now.replace(hour=asof.hour, minute=asof.minute) if asof else now
    items = []
    for c in cands:
        try:
            bars, prev = _yahoo_minutes(c["code"], c.get("market", "上市"))
        except FetchError:
            items.append({**c, "status": "nodata", "label": "⚪ 暫缺", "reasons": ["盤中資料暫時抓不到"], "checks": []})
            continue
        bars = [b for b in bars if b["t"].date() == now.date() and b["t"] <= eval_now.replace(second=59)]
        items.append(evaluate(c, bars, prev or c.get("close"), avg_vols.get(c["code"]), eval_now, verdict))
    order = {"go": 0, "watch": 1, "wait": 2, "no": 3, "nodata": 4}
    items.sort(key=lambda x: (order.get(x["status"], 9), x.get("tier") != "主要候選"))
    ph = phase(eval_now)
    return {"updated": eval_now.isoformat(timespec="minutes"), "phase": ph, "verdict": verdict,
            "counts": {k: sum(1 for i in items if i["status"] == k) for k in ("go", "watch", "no")},
            "items": items, "rules": daytrade.get("rules", []), "replay": bool(asof)}

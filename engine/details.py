"""個股詳細頁資料：K 線、技術摘要、籌碼、營收估值、訊號與交易計畫。每檔一個小 JSON，網站點開時才載入。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .strategy import INDUSTRY, entry_exit_signals

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "site" / "data" / "stock"


def _r(x, d=2):
    return None if x is None else round(x, d)


def plan(s: dict, regime: dict) -> dict:
    """交易計畫：進場區、停損（ATR 與 −7% 取較近者）、目標（2R、3R 與 52 週高）。"""
    t = s.get("tech") or {}
    c = s["close"]
    atr = t.get("atr") or c * 0.03
    above20 = bool(t.get("ma20") and c > t["ma20"])
    if above20:
        lo = max(t.get("ma10") or t.get("ma20"), t.get("ma20"))
        hi = t.get("ma5") or c
        zone = sorted([lo, hi])
        how = "多頭格局：拉回 5~10 日線附近分批，不追高"
    else:
        zone = [t.get("ma20") or c, (t.get("ma20") or c) * 1.02]
        how = "尚在月線下：等收盤站回月線再試單"
    ref = (zone[0] + zone[1]) / 2
    stop = max(ref - 2 * atr, ref * 0.93)
    risk = ref - stop
    t1, t2 = ref + 2 * risk, ref + 3 * risk
    return {"zone": [_r(zone[0]), _r(zone[1])], "how": how, "stop": _r(stop), "stop_pct": _r((stop / ref - 1) * 100, 1),
            "t1": _r(t1), "t2": _r(t2), "hi52": _r(t.get("hi52")), "rr": 2.0,
            "single_max": regime["level"]["single_max"], "allowed": regime["longterm"]["key"] != "watch"}


def build(s: dict, regime: dict, basis) -> dict:
    t = s.get("tech") or {}
    bars = s.get("_bars") or []
    m = t.get("macd") or {}
    k = t.get("kd") or {}
    sig = entry_exit_signals(s)
    checks = [
        ["收盤站上月線", bool(t.get("ma20") and s["close"] > t["ma20"])],
        ["月線在季線之上", bool(t.get("ma20") and t.get("ma60") and t["ma20"] > t["ma60"])],
        ["季線上彎", bool(t.get("ma60_up"))],
        ["站上年線(200MA)", bool(t.get("ma200") and s["close"] > t["ma200"])],
        ["RSI 在 40~70 健康區", bool(t.get("rsi") and 40 <= t["rsi"] <= 70)],
        ["近 3 日法人至少 2 日買超", sum(1 for x in (s.get("insti_hist") or []) if x and x > 0) >= 2],
    ]
    return {
        "c": s["code"], "n": s["name"], "m": s["market"], "i": INDUSTRY.get(s.get("industry"), ""), "basis": str(basis),
        "p": s["close"], "chg": s.get("pct"), "o": s.get("open"), "h": s.get("high"), "l": s.get("low"),
        "v": s.get("volume"), "amt": _r((s.get("amount") or 0) / 1e8),
        "bars": [[str(b["date"]), _r(b["open"]), _r(b["high"]), _r(b["low"]), _r(b["close"]), int(b["volume"] or 0)]
                 for b in bars],
        "t": {"ma5": _r(t.get("ma5")), "ma10": _r(t.get("ma10")), "ma20": _r(t.get("ma20")), "ma60": _r(t.get("ma60")),
              "ma120": _r(t.get("ma120")), "ma200": _r(t.get("ma200")), "rsi": _r(t.get("rsi"), 1),
              "k": _r(k.get("k"), 1), "d": _r(k.get("d"), 1), "macd": _r(m.get("hist"), 3),
              "atr": _r(t.get("atr")), "atr_pct": _r((t.get("atr_pct") or 0) * 100, 2),
              "ret5": _r((t.get("ret5") or 0) * 100, 1), "ret20": _r((t.get("ret20") or 0) * 100, 1),
              "ret60": _r((t.get("ret60") or 0) * 100, 1), "ret120": _r((t.get("ret120") or 0) * 100, 1),
              "hi52": _r(t.get("hi52")), "lo52": _r(t.get("lo52")), "vol_ratio": _r(t.get("vol_ratio")),
              "bias20": _r((t.get("bias20") or 0) * 100, 1), "amp8": _r((t.get("amp8") or 0) * 100, 1)},
        "score": s.get("score"), "parts": s.get("score_parts"), "rs": s.get("rs"),
        "insti": s.get("insti_days") or [],
        "rev": {"ym": s.get("rev_ym"), "yoy": _r(s.get("rev_yoy"), 1), "mom": _r(s.get("rev_mom"), 1),
                "ytd": _r(s.get("rev_ytd"), 1)},
        "val": {"pe": s.get("pe"), "pb": s.get("pb"), "y": s.get("yield")},
        "chips": {"margin_chg": s.get("margin_chg"), "dt_ratio": s.get("dt_ratio"), "sbl_ratio": s.get("sbl_ratio")},
        "gates": s.get("gates", []), "sig": sig, "checks": checks, "plan": plan(s, regime),
    }


def write_public(details: dict) -> None:
    shutil.rmtree(DIR, ignore_errors=True)
    DIR.mkdir(parents=True, exist_ok=True)
    for c, d in details.items():
        (DIR / f"{c}.json").write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

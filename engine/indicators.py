"""技術指標（純 Python，無外部套件）。輸入 bars = [{date, open, high, low, close, volume}] 由舊到新。"""
from __future__ import annotations

from statistics import mean


def sma(xs: list[float], n: int) -> float | None:
    return mean(xs[-n:]) if len(xs) >= n else None


def sma_series(xs: list[float], n: int) -> list[float | None]:
    out, s = [], 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def ema_series(xs: list[float], n: int) -> list[float]:
    k, out = 2 / (n + 1), []
    for i, x in enumerate(xs):
        out.append(x if i == 0 else x * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def macd(closes: list[float]) -> dict | None:
    if len(closes) < 35:
        return None
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema_series(dif, 9)
    hist = [a - b for a, b in zip(dif, dea)]
    return {"dif": dif[-1], "dea": dea[-1], "hist": hist[-1], "hist_prev": hist[-2],
            "golden": hist[-2] <= 0 < hist[-1], "dead": hist[-2] >= 0 > hist[-1]}


def atr(bars: list[dict], n: int = 14) -> float | None:
    if len(bars) <= n:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = mean(trs[:n])
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def kd(bars: list[dict], n: int = 9) -> dict | None:
    if len(bars) < n + 2:
        return None
    k, d = 50.0, 50.0
    ks = []
    for i in range(n - 1, len(bars)):
        win = bars[i - n + 1:i + 1]
        hi, lo = max(b["high"] for b in win), min(b["low"] for b in win)
        rsv = 50.0 if hi == lo else (bars[i]["close"] - lo) / (hi - lo) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        ks.append((k, d))
    (k0, d0), (k1, d1) = ks[-2], ks[-1]
    return {"k": k1, "d": d1, "golden": k0 <= d0 and k1 > d1, "dead": k0 >= d0 and k1 < d1}


def pct_return(closes: list[float], n: int) -> float | None:
    if len(closes) <= n or closes[-n - 1] == 0:
        return None
    return closes[-1] / closes[-n - 1] - 1


def avg_amplitude(bars: list[dict], n: int = 8) -> float | None:
    """近 n 日平均振幅（排除一字鎖死日）。"""
    xs = []
    for i in range(max(1, len(bars) - n), len(bars)):
        b, pc = bars[i], bars[i - 1]["close"]
        if b["high"] == b["low"] or not pc:
            continue
        xs.append((b["high"] - b["low"]) / pc)
    return mean(xs) if xs else None


def analyze(bars: list[dict]) -> dict:
    """單一個股完整技術摘要。"""
    if len(bars) < 25:
        return {"ok": False}
    c = [b["close"] for b in bars]
    v = [b["volume"] or 0 for b in bars]
    ma5, ma10, ma20, ma60 = sma(c, 5), sma(c, 10), sma(c, 20), sma(c, 60)
    ma120, ma200 = sma(c, 120), sma(c, 200)
    ma20s = sma_series(c, 20)
    ma60s = sma_series(c, 60)
    a = atr(bars)
    hi20 = max(b["high"] for b in bars[-21:-1]) if len(bars) > 21 else None
    hi52 = max(b["high"] for b in bars[-250:])
    lo52 = min(b["low"] for b in bars[-250:])
    vol5, vol20 = sma(v, 5), sma(v, 20)
    last = bars[-1]
    rng = last["high"] - last["low"]
    out = {
        "ok": True, "close": c[-1], "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "ma120": ma120, "ma200": ma200,
        "ma20_up": ma20s[-1] is not None and ma20s[-6] is not None and ma20s[-1] > ma20s[-6],
        "ma60_up": len(ma60s) > 10 and ma60s[-1] is not None and ma60s[-11] is not None and ma60s[-1] > ma60s[-11],
        "rsi": rsi(c), "macd": macd(c), "kd": kd(bars), "atr": a,
        "atr_pct": a / c[-1] if a else None,
        "ret5": pct_return(c, 5), "ret20": pct_return(c, 20), "ret60": pct_return(c, 60),
        "ret120": pct_return(c, 120),
        "hi20": hi20, "hi52": hi52, "lo52": lo52,
        "from_hi52": c[-1] / hi52 - 1 if hi52 else None,
        "from_lo52": c[-1] / lo52 - 1 if lo52 else None,
        "vol_ratio": (v[-1] / vol20) if vol20 else None,
        "vol5_ratio": (vol5 / vol20) if vol20 and vol5 else None,
        "bias5": (c[-1] - ma5) / ma5 if ma5 else None,
        "bias20": (c[-1] - ma20) / ma20 if ma20 else None,
        "amp8": avg_amplitude(bars, 8),
        "upper_shadow_fade": rng > 0 and (last["high"] - last["close"]) / rng > 0.5 and last["high"] > last["open"],
        "breakout20": hi20 is not None and c[-1] > hi20,
        "ma5_cross_ma20": False,
        "spark": [round(x, 2) for x in c[-60:]],
    }
    ma5s = sma_series(c, 5)
    if ma5s[-2] and ma20s[-2] and ma5 and ma20:
        out["ma5_cross_ma20"] = ma5s[-2] <= ma20s[-2] and ma5 > ma20
        out["ma5_dead_ma20"] = ma5s[-2] >= ma20s[-2] and ma5 < ma20
    return out

"""選股與風控規則。

第一部分完整實作晨報 SKILL v4.5.1（大盤濾網、五道門檻、中長期 Top5、當沖候選、低估值、持股健診、斷路器、部位級距）。
第二部分是新增的「多因子綜合評分」與「進出場訊號」，用來擴大資料面、提高篩選品質；
兩套方法都會進回測記分，用真實結果比較誰的期望值高，而不是憑感覺。
"""
from __future__ import annotations

from statistics import median

INDUSTRY = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織", "05": "電機機械", "06": "電器電纜",
    "08": "玻璃陶瓷", "09": "造紙", "10": "鋼鐵", "11": "橡膠", "12": "汽車", "14": "建材營造",
    "15": "航運", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨", "19": "綜合", "20": "其他",
    "21": "化學", "22": "生技醫療", "23": "油電燃氣", "24": "半導體", "25": "電腦週邊", "26": "光電",
    "27": "通信網路", "28": "電子零組件", "29": "電子通路", "30": "資訊服務", "31": "其他電子",
    "32": "文化創意", "33": "農業科技", "34": "電子商務", "35": "綠能環保", "36": "數位雲端",
    "37": "運動休閒", "38": "居家生活", "80": "管理股票", "91": "存託憑證",
}
# 當沖不做：金融＋傳產
DAYTRADE_EXCLUDE = {"01", "02", "03", "04", "05", "06", "08", "09", "10", "11", "12", "14", "15",
                    "16", "17", "18", "19", "21", "23"}

LOT = 1000  # 一張 = 1000 股


def is_stock(code: str) -> bool:
    return len(code) == 4 and code.isdigit() and not code.startswith("0")


def pct(a, b):
    return None if a is None or not b else (a / b - 1) * 100


# ═════════════════════════ 大盤研判 ═════════════════════════
def market_regime(idx_closes: list[float], basis_pct: float, breadth: dict, foreign_oi: float | None,
                  sox_down3: bool | None, event_day: bool = False, ma200: float | None = None) -> dict:
    c = idx_closes
    close = c[-1]
    ma5 = sum(c[-5:]) / 5
    ma20 = sum(c[-20:]) / 20
    ma50 = sum(c[-50:]) / 50 if len(c) >= 50 else None
    up, down = breadth.get("up") or 0, breadth.get("down") or 0
    down_up = down / up if up else 99.0
    panic_next = basis_pct is not None and basis_pct < -3

    # Weinstein 階段（以 50MA 近似 30 週線，搭配斜率）
    ma50_prev = sum(c[-60:-10]) / 50 if len(c) >= 60 else None
    ma50_up = ma50 is not None and ma50_prev is not None and ma50 > ma50_prev
    if ma50 is None:
        stage = "資料不足"
    elif close > ma50 and ma50_up:
        stage = "Stage 2 上升"
    elif close > ma50 and not ma50_up:
        stage = "Stage 1 / 3 盤整"
    elif close < ma50 and not ma50_up:
        stage = "Stage 4 下降"
    else:
        stage = "Stage 3 轉弱"

    # 部位級距
    if ma50 and close > ma50 and close > ma20:
        level = {"key": "green", "label": "🟢 Stage2", "single_max": 15, "cash_min": 15}
    elif ma50 and close > ma50:
        level = {"key": "yellow", "label": "🟡 偏弱", "single_max": 10, "cash_min": 25}
    else:
        level = {"key": "white", "label": "⚪ 空頭", "single_max": 5, "cash_min": 40}
    notes = []
    if panic_next:
        level = {**level, "single_max": level["single_max"] / 2}
        notes.append("恐慌日次日：部位全降一級減半")
    if event_day:
        level = {**level, "single_max": level["single_max"] / 2}
        notes.append("重大事件日：再降一級")

    # 中長期環境濾網（v4.4 E）
    if close < ma5 and down > up:
        lt = {"key": "watch", "label": "只觀察", "why": "加權收在 5MA 下且跌家多於漲家"}
    elif close < ma20 or panic_next:
        lt = {"key": "cautious", "label": "保守", "why": "加權在月線下" if close < ma20 else "恐慌日次日"}
    else:
        lt = {"key": "normal", "label": "正常", "why": "加權站上 5MA"}

    # 當沖硬性否決
    veto = []
    if ma50 and close < ma50:
        veto.append("加權 < 50MA")
    if close < ma20 and down_up >= 2:
        veto.append(f"加權 < 20MA 且跌÷漲 {down_up:.1f}")
    if foreign_oi is not None and foreign_oi <= -60000:
        veto.append(f"外資期貨淨空 {foreign_oi:,.0f} 口")
    if panic_next:
        veto.append("恐慌日次日")
    if sox_down3:
        veto.append("費半連 3 日跌")
    if veto:
        dt = {"key": "red", "label": "🔴 停做", "why": veto}
    elif down > up or event_day:
        dt = {"key": "yellow", "label": "🟡 減量", "why": ["廣度偏空" if down > up else "事件日"]}
    else:
        dt = {"key": "green", "label": "🟢 可做", "why": []}

    alarms = [
        {"name": "跌破月線", "on": close < ma20},
        {"name": "跌破季線(50MA)", "on": bool(ma50 and close < ma50)},
        {"name": "廣度跌÷漲≥2", "on": down_up >= 2},
        {"name": "外資期貨淨空≥6萬口", "on": foreign_oi is not None and foreign_oi <= -60000},
    ]
    return {
        "close": close, "pct": basis_pct, "ma5": ma5, "ma20": ma20, "ma50": ma50, "ma200": ma200,
        "vs_ma5": pct(close, ma5), "vs_ma20": pct(close, ma20), "vs_ma50": pct(close, ma50),
        "breadth": breadth, "down_up": down_up, "foreign_oi": foreign_oi, "panic_next": panic_next,
        "stage": stage, "level": level, "longterm": lt, "daytrade": dt, "alarms": alarms,
        "alarm_count": sum(a["on"] for a in alarms), "notes": notes,
    }


# ═════════════════════════ 五道硬門檻 ═════════════════════════
def apply_gates(stocks: dict, disposal: dict) -> dict:
    """在每檔 stock 上標記 gates（被哪幾道擋）。回傳統計。"""
    stats = {k: 0 for k in ("漲幅≥9%", "當沖比≥40%", "借券賣出≥15%", "法人連3賣", "處置股")}
    for s in stocks.values():
        g = []
        if s.get("pct") is not None and s["pct"] >= 9:
            g.append("漲幅≥9%")
        if s.get("dt_ratio") is not None and s["dt_ratio"] >= 40:
            g.append("當沖比≥40%")
        if s.get("sbl_ratio") is not None and s["sbl_ratio"] >= 15:
            g.append("借券賣出≥15%")
        ins = s.get("insti_hist") or []
        if len(ins) >= 3 and all(x is not None and x < 0 for x in ins[-3:]):
            g.append("法人連3賣")
        if s["code"] in disposal:
            g.append("處置股")
        s["gates"] = g
        for x in g:
            stats[x] += 1
    n = len(stocks)
    passed = sum(1 for s in stocks.values() if not s["gates"])
    return {"total": n, "passed": passed,
            "by_gate": [{"name": k, "count": v, "pct": round(v / n * 100, 1) if n else 0} for k, v in stats.items()]}


# ═════════════════════════ 中長期 Top5（SKILL 相對吸籌法）═════════════════════════
def midterm_picks(stocks: dict, regime: dict, breaker_on: bool, top_n: int = 5) -> list[dict]:
    pool = []
    for s in stocks.values():
        if s["gates"] or not s.get("insti") or not s.get("volume") or not is_stock(s["code"]):
            continue
        if (s.get("amount") or 0) < 3e8 or s["insti"] < 300 * LOT:
            continue
        ins = s.get("insti_hist") or []
        if len(ins) < 2 or not ((ins[-1] or 0) > 0 and (ins[-2] or 0) > 0):
            continue
        s["accum"] = s["insti"] / s["volume"] * 100
        pool.append(s)
    pool.sort(key=lambda x: x["accum"], reverse=True)
    picks, used_ind = [], set()
    lt = regime["longterm"]["key"]
    below50 = regime["ma50"] is not None and regime["close"] < regime["ma50"]
    limit = 2 if lt == "cautious" else top_n
    for s in pool:
        ind = s.get("industry") or "?"
        if ind in used_ind:
            continue
        t = s.get("tech") or {}
        conds = {
            "漲幅≤4%": s["pct"] is not None and s["pct"] <= 4,
            "乖離MA5≤5%": t.get("bias5") is not None and t["bias5"] <= 0.05,
            "站上MA5與MA20": bool(t.get("ma5") and t.get("ma20") and s["close"] > t["ma5"] and s["close"] > t["ma20"]),
        }
        ok = all(conds.values())
        if breaker_on or lt == "watch" or below50:
            status = "👀僅觀察"
        elif ok:
            status = "🟢可分批"
        else:
            status = "⏳等回檔"
        entry_lo = t.get("ma5") or s["close"]
        atr_stop = s["close"] - 2 * t["atr"] if t.get("atr") else None
        hard_stop = s["close"] * 0.93
        picks.append({
            "code": s["code"], "name": s["name"], "market": s["market"], "industry": INDUSTRY.get(ind, ind),
            "close": s["close"], "pct": s["pct"], "accum": round(s["accum"], 1),
            "insti_2d": [round(x / LOT) for x in (s.get("insti_hist") or [])[-2:]],
            "foreign": round((s.get("foreign") or 0) / LOT), "trust": round((s.get("trust") or 0) / LOT),
            "ma5": t.get("ma5"), "ma20": t.get("ma20"), "bias5": t.get("bias5"),
            "conds": conds, "status": status,
            "entry_zone": [round(entry_lo * 0.99, 2), round(entry_lo * 1.01, 2)],
            "stop": round(max(hard_stop, atr_stop) if atr_stop else hard_stop, 2),
            "stop_hard": round(hard_stop, 2), "target": round(s["close"] * 1.05, 2),
            "score": s.get("score"),
        })
        used_ind.add(ind)
        if len(picks) >= limit:
            break
    return picks


# ═════════════════════════ 當沖候選 v3 ═════════════════════════
def daytrade_picks(stocks: dict, regime: dict, budget: float = 500_000, n: int = 2, n_watch: int = 8) -> dict:
    """主要候選（嚴格 v3 條件，最多 2 檔）＋盤中觀察名單（條件略寬，最多 8 檔）。"""
    strict, loose = [], []
    for s in stocks.values():
        t = s.get("tech") or {}
        if s["market"] == "興櫃" or not is_stock(s["code"]) or s["code"] in (s.get("_disposal") or {}):
            continue
        if "處置股" in s.get("gates", []) or s.get("dt_suspended"):
            continue
        if not s.get("close") or s["close"] > 450 or s.get("industry") in DAYTRADE_EXCLUDE:
            continue
        amp, r, amt = t.get("amp8"), s.get("dt_ratio"), s.get("amount") or 0
        if amp is None or t.get("upper_shadow_fade"):
            continue
        if s["pct"] is not None and s["pct"] >= 5:
            continue
        tag = "昨重挫：需站回VWAP＋帶量" if s["pct"] is not None and s["pct"] <= -9 else ""
        rank = -amp * min(amt / 30e8, 1.5)
        if amt >= 10e8 and amp >= 0.03 and (r is None or 15 <= r <= 35):
            strict.append((tag != "", rank, s, amp, tag))
        elif amt >= 5e8 and amp >= 0.025 and (r is None or 10 <= r <= 45):
            loose.append((tag != "", rank, s, amp, tag))
    strict.sort(key=lambda x: x[:2])
    loose.sort(key=lambda x: x[:2])

    def card(s, amp, tag, tier):
        lot_cost = s["close"] * LOT
        return {"code": s["code"], "name": s["name"], "market": s["market"], "close": s["close"], "pct": s["pct"],
                "prev_high": s.get("high"), "prev_low": s.get("low"),
                "amount_e": round((s.get("amount") or 0) / 1e8, 1), "dt_ratio": s.get("dt_ratio"),
                "amp8": round(amp * 100, 1), "lot_cost_w": round(lot_cost / 1e4, 1),
                "lots": int(budget // lot_cost), "tag": tag, "tier": tier,
                "industry": INDUSTRY.get(s.get("industry"), ""),
                "trigger": "盤中連續站穩 VWAP、非開盤衝高回落",
                "stop": round(s["close"] * 0.975, 2), "tp1": round(s["close"] * 1.03, 2)}

    picks, watch, used = [], [], {}
    for _, _, s, amp, tag in strict:
        if used.get(s.get("industry")):
            continue
        picks.append(card(s, amp, tag, "主要候選"))
        used[s.get("industry")] = 1
        if len(picks) >= n:
            break
    watch = list(picks)
    for _, _, s, amp, tag in strict + loose:
        if len(watch) >= n_watch:
            break
        if any(w["code"] == s["code"] for w in watch) or used.get(s.get("industry"), 0) >= 2:
            continue
        watch.append(card(s, amp, tag, "觀察名單"))
        used[s.get("industry")] = used.get(s.get("industry"), 0) + 1
    return {"verdict": regime["daytrade"], "picks": picks, "watch": watch,
            "rules": ["停損 -2~3% 或破 VWAP 全出", "15~20 分鐘未表態先退",
                      "第一批(半數) +3~5% 或前高落袋，剩餘移動停利", "收盤前一定平倉"]}


# ═════════════════════════ 低估值 ═════════════════════════
def value_picks(stocks: dict, n: int = 5) -> list[dict]:
    pool = []
    for s in stocks.values():
        if s["gates"] or s["market"] == "興櫃" or not is_stock(s["code"]):
            continue
        y, pe, pb = s.get("yield"), s.get("pe"), s.get("pb")
        if not (y and pe and pb) or y < 3 or not (0 < pe <= 12) or pb > 1.5:
            continue
        if (s.get("insti") or 0) <= 0 or (s.get("amount") or 0) < 5e8:
            continue
        # 綜合：殖利率高、本益比低、淨值比低
        rank = y / 3 + 12 / pe + 1.5 / max(pb, 0.3)
        pool.append((rank, s))
    pool.sort(key=lambda x: x[0], reverse=True)
    return [{"code": s["code"], "name": s["name"], "close": s["close"], "pct": s["pct"],
             "yield": s["yield"], "pe": s["pe"], "pb": s["pb"], "insti": round(s["insti"] / LOT),
             "rev_yoy": s.get("rev_yoy"), "stop": round(s["close"] * 0.93, 2),
             "industry": INDUSTRY.get(s.get("industry"), "")} for _, s in pool[:n]]


# ═════════════════════════ 多因子綜合評分（新增）═════════════════════════
def composite_scores(stocks: dict) -> None:
    """0~100 分：趨勢 30＋相對強弱 20＋籌碼 25＋基本面 15＋風險 10。結果寫回 s['score']、s['score_parts']。

    依據：Weinstein/Minervini 趨勢樣板、O'Neil 相對強度 RS、法人持續買超、月營收成長、波動與乖離控管。
    """
    elig = [s for s in stocks.values() if (s.get("tech") or {}).get("ok") and is_stock(s["code"])]
    if not elig:
        return
    r60 = sorted(s["tech"]["ret60"] for s in elig if s["tech"].get("ret60") is not None)
    acc = sorted(s["insti"] / s["volume"] for s in elig if s.get("insti") is not None and s.get("volume"))

    def prank(xs, v):
        if v is None or not xs:
            return 0.5
        lo, hi = 0, len(xs)
        while lo < hi:
            mid = (lo + hi) // 2
            if xs[mid] < v:
                lo = mid + 1
            else:
                hi = mid
        return lo / len(xs)

    for s in elig:
        t = s["tech"]
        c = s["close"]
        trend = 0
        trend += 6 if t["ma20"] and c > t["ma20"] else 0
        trend += 6 if t["ma60"] and c > t["ma60"] else 0
        trend += 6 if t["ma20"] and t["ma60"] and t["ma20"] > t["ma60"] else 0
        trend += 6 if t.get("ma60_up") else 0
        trend += 6 if t["ma200"] and c > t["ma200"] else (3 if t["ma200"] is None and t["ma120"] and c > t["ma120"] else 0)
        rs_p = prank(r60, t.get("ret60"))
        rs = 20 * rs_p
        if t.get("rsi") and t["rsi"] > 80:
            rs -= 5
        ins = s.get("insti_hist") or []
        a_p = prank(acc, s["insti"] / s["volume"]) if s.get("insti") is not None and s.get("volume") else 0.5
        chips = 12 * a_p
        chips += 6 * sum(1 for x in ins[-3:] if x and x > 0) / 3
        chips += 4 if (s.get("trust") or 0) > 0 else 0
        mchg = s.get("margin_chg")
        chips += 3 if mchg is not None and mchg <= 0 else 0
        fund = 0
        yoy = s.get("rev_yoy")
        fund += 8 if yoy is not None and yoy >= 20 else (5 if yoy is not None and yoy > 0 else 0)
        fund += 4 if (s.get("rev_ytd") or 0) > 0 else 0
        fund += 3 if s.get("pe") and 0 < s["pe"] <= 25 else 0
        risk = 0
        ap = t.get("atr_pct")
        risk += 5 if ap is not None and ap < 0.03 else (3 if ap is not None and ap < 0.05 else 0)
        b20 = t.get("bias20")
        risk += 5 if b20 is not None and -0.03 <= b20 <= 0.10 else (2 if b20 is not None and b20 <= 0.15 else 0)
        total = max(0, min(100, trend + rs + chips + fund + risk - (15 if s["gates"] else 0)))
        s["score"] = round(total)
        s["rs"] = round(rs_p * 99)
        s["score_parts"] = {"趨勢": round(trend), "相對強弱": round(rs), "籌碼": round(chips),
                            "基本面": round(fund), "風險": round(risk)}


def composite_picks(stocks: dict, regime: dict, breaker_on: bool, n: int = 8) -> list[dict]:
    pool = [s for s in stocks.values() if s.get("score") is not None and not s["gates"]
            and (s.get("amount") or 0) >= 3e8 and s["market"] != "興櫃" and is_stock(s["code"])]
    pool.sort(key=lambda s: s["score"], reverse=True)
    out, used = [], {}
    for s in pool:
        ind = s.get("industry") or "?"
        if used.get(ind, 0) >= 2:
            continue
        sig = entry_exit_signals(s)
        t = s["tech"]
        allow = not breaker_on and regime["longterm"]["key"] != "watch"
        action = ("🟢 可分批" if allow and sig["entry"] else "⏳ 等訊號" if allow else "👀 觀察")
        atr_stop = s["close"] - 2 * t["atr"] if t.get("atr") else s["close"] * 0.93
        out.append({
            "code": s["code"], "name": s["name"], "market": s["market"], "industry": INDUSTRY.get(ind, ind),
            "close": s["close"], "pct": s["pct"], "score": s["score"], "parts": s["score_parts"], "rs": s.get("rs"),
            "rev_yoy": s.get("rev_yoy"), "pe": s.get("pe"), "signals": sig, "action": action,
            "stop": round(max(atr_stop, s["close"] * 0.92), 2), "target": round(s["close"] + 3 * (t.get("atr") or s["close"] * 0.03), 2),
            "spark": t.get("spark", [])[-40:],
        })
        used[ind] = used.get(ind, 0) + 1
        if len(out) >= n:
            break
    return out


# ═════════════════════════ 進出場訊號（新增）═════════════════════════
def entry_exit_signals(s: dict, stop_loss: float | None = None, cost: float | None = None) -> dict:
    t = s.get("tech") or {}
    c = s.get("close")
    entry, exit_, info = [], [], []
    if not t.get("ok") or not c:
        return {"entry": entry, "exit": exit_, "info": ["技術資料不足"]}
    uptrend = t["ma20"] and t["ma60"] and c > t["ma60"] and t["ma20"] > t["ma60"]
    if t.get("breakout20") and (t.get("vol_ratio") or 0) >= 1.5 and uptrend:
        entry.append("帶量突破 20 日高")
    if uptrend and t.get("bias20") is not None and -0.02 <= t["bias20"] <= 0.02 and (t.get("rsi") or 50) < 60:
        entry.append("多頭回測月線")
    if t.get("ma5_cross_ma20") and c > (t["ma60"] or 0):
        entry.append("5MA 黃金交叉 20MA")
    m = t.get("macd")
    if m and m["golden"] and m["dif"] < 0:
        info.append("MACD 低檔翻多")
    k = t.get("kd")
    if k and k["golden"] and k["k"] < 30:
        info.append("KD 低檔黃金交叉")

    if stop_loss and c <= stop_loss:
        exit_.append(f"跌破停損 {stop_loss:g}")
    if t["ma20"] and c < t["ma20"] and t.get("ma20_up") is False:
        exit_.append("跌破月線且月線下彎")
    if t["ma60"] and c < t["ma60"]:
        exit_.append("跌破季線")
    if m and m["dead"]:
        exit_.append("MACD 死亡交叉")
    if t.get("bias20") is not None and t["bias20"] > 0.15:
        exit_.append("乖離月線 >15%，分批停利")
    if t.get("rsi") and t["rsi"] > 80:
        info.append(f"RSI {t['rsi']:.0f} 過熱")
    if t.get("rsi") and t["rsi"] < 25:
        info.append(f"RSI {t['rsi']:.0f} 超賣")
    trail = None
    if t.get("atr") and t.get("hi20"):
        trail = round(max(t["hi20"], c) - 2.5 * t["atr"], 2)
    return {"entry": entry, "exit": exit_, "info": info, "trail_stop": trail}


# ═════════════════════════ 持股健診 ═════════════════════════
def holding_health(h: dict, s: dict | None, total_value: float) -> dict:
    price = (s or {}).get("close") or h.get("current_price")
    t = (s or {}).get("tech") or {}
    shares = h.get("shares") or 0
    cost = h.get("buy_price") or 0
    value = price * shares if price else 0
    pnl = (price - cost) * shares if price and cost else None
    stop = h.get("stop_loss")
    light, why, short = "🟢", "站上月線、順勢", "續抱"
    if stop and price and price <= stop:
        light, why, short = "🔴", f"已破停損 {stop:g}，建議依紀律處理", f"破停損{stop:g}，依紀律出"
    elif t.get("ma60") and price and price < t["ma60"] and t.get("ma20_up") is False:
        light, why, short = "🔴", "破季線且持續走弱", "破季線走弱，逢彈減"
    elif t.get("ma5") and price and (price < t["ma5"] or (t.get("ma20") and price < t["ma20"])):
        light, why, short = "🟡", "跌破 MA5 或月線，轉弱注意", "轉弱，盯月線"
    if h.get("stock_id", "").startswith("00") and light == "🔴" and not stop:
        light, why, short = "🟡", "核心 ETF 不設技術停損，控比重", "核心ETF，控比重"
    if (s or {}).get("market") == "興櫃" and not stop:
        light, why, short = "🟡", "興櫃題材長抱，控比重、不加碼", "興櫃長抱，不加碼"
    weight = value / total_value * 100 if total_value else 0
    flags = []
    if weight > 20:
        flags.append(f"占組合 {weight:.1f}% 超過 20% 上限")
    sig = entry_exit_signals(s, stop, cost) if s else {"entry": [], "exit": [], "info": []}
    return {
        "code": h.get("stock_id"), "name": h.get("stock_name"), "shares": shares, "cost": cost,
        "price": price, "value": round(value), "pnl": round(pnl) if pnl is not None else None,
        "pnl_pct": round((price / cost - 1) * 100, 2) if price and cost else None,
        "pct": (s or {}).get("pct"), "weight": round(weight, 1), "stop": stop, "target": h.get("target_price"),
        "light": light, "why": why, "short": short, "flags": flags, "signals": sig, "type": h.get("hold_type", ""),
        "ma5": t.get("ma5"), "ma20": t.get("ma20"), "ma60": t.get("ma60"), "market": (s or {}).get("market"),
        "spark": t.get("spark", [])[-60:], "notes": h.get("notes", ""),
    }


def portfolio_review(holdings: list[dict], stocks: dict, regime: dict, cash: float | None = None) -> dict:
    vals = []
    for h in holdings:
        s = stocks.get(h.get("stock_id"))
        p = (s or {}).get("close") or h.get("current_price") or 0
        vals.append(p * (h.get("shares") or 0))
    total = sum(vals) + (cash or 0)
    rows = [holding_health(h, stocks.get(h.get("stock_id")), total) for h in holdings]
    cost = sum((r["cost"] or 0) * r["shares"] for r in rows)
    mv = sum(r["value"] for r in rows)
    by_ind = {}
    for r in rows:
        ind = INDUSTRY.get((stocks.get(r["code"]) or {}).get("industry"), r["type"] or "其他")
        by_ind[ind] = by_ind.get(ind, 0) + r["value"]
    actions = []
    for r in sorted(rows, key=lambda r: -r["weight"]):
        if r["weight"] > 20:
            excess = (r["weight"] - 20) / 100 * total
            n_sh = int(excess / r["price"]) if r["price"] else 0
            actions.append({"level": "🚨", "code": r["code"], "text":
                            f"{r['code']} {r['name']} 占 {r['weight']:.1f}%＞20%，應降約 {n_sh:,} 股（約 {excess/1e4:.1f} 萬），分批趁強減碼、暫停加碼"})
    for r in rows:
        if r["light"] == "🔴":
            actions.append({"level": "🔴", "code": r["code"], "text": f"{r['code']} {r['name']}：{r['why']}"})
    for ind, v in by_ind.items():
        if total and v / total > 0.35:
            actions.append({"level": "🚨", "code": "", "text": f"產業「{ind}」占 {v/total*100:.0f}%＞35% 上限"})
    cash_pct = (cash / total * 100) if cash is not None and total else None
    if cash_pct is not None and cash_pct < regime["level"]["cash_min"]:
        actions.append({"level": "🟡", "code": "", "text": f"現金 {cash_pct:.0f}% 低於本級距下限 {regime['level']['cash_min']}%"})
    return {"rows": rows, "market_value": round(mv), "cost": round(cost), "pnl": round(mv - cost),
            "pnl_pct": round((mv / cost - 1) * 100, 2) if cost else None, "total": round(total),
            "cash": cash, "cash_pct": cash_pct,
            "by_industry": [{"name": k, "value": round(v), "pct": round(v / total * 100, 1) if total else 0}
                            for k, v in sorted(by_ind.items(), key=lambda x: -x[1])],
            "actions": actions}


def sector_heat(sectors: list[dict]) -> dict:
    xs = [s for s in sectors if s["name"].endswith("類指數") and s.get("pct") is not None]
    xs.sort(key=lambda s: s["pct"], reverse=True)
    return {"top": xs[:6], "bottom": xs[-6:][::-1], "all": xs,
            "median": median([s["pct"] for s in xs]) if xs else None}

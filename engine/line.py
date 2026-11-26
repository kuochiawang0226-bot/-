"""LINE 推播：依「LINE 推播排版範本 v2」組固定 5 則，並透過 Messaging API 推送。

金鑰一律從環境變數讀取（GitHub Secrets）：LINE_CHANNEL_ACCESS_TOKEN、LINE_USER_ID。
"""
from __future__ import annotations

import json
import os

import requests

FOOT = "⚠️ 技術面參考，非投資建議"


def _n(x, d=0):
    if x is None:
        return "—"
    return f"{x:,.{d}f}"


def _p(x, d=1):
    return "—" if x is None else f"{x:+.{d}f}%"


def _md(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{int(m)}/{int(d)}"


def build(rep: dict, site_url: str | None = None) -> list[dict]:
    m, today, basis = rep["market"], _md(rep["report_date"]), _md(rep["basis"])
    reg = m["regime"]
    b = reg["breadth"]
    msgs = []

    # ① 大盤
    tone = {"green": "🟢", "yellow": "🟡", "white": "⚪"}[reg["level"]["key"]]
    l1 = [f"【📊 大盤 {today}】{tone} {reg['stage']}", f"資料基準：{basis} 收盤", "",
          "〈🚦指數〉", f"・加權 {_n(reg['close'])}｜{_p(reg['pct'], 2)}",
          f"・量 {_n(m.get('amount_e'))} 億",
          f"・5MA {_n(reg['ma5'])}｜{_p(reg['vs_ma5'])}",
          f"・月線 {_n(reg['ma20'])}｜{_p(reg['vs_ma20'])}",
          f"・季線 {_n(reg['ma50'])}｜{_p(reg['vs_ma50'])}", "",
          "〈📈廣度與籌碼〉", f"・漲 {_n(b.get('up'))}／跌 {_n(b.get('down'))}（跌漲比 {reg['down_up']:.2f}）"]
    ins = m.get("insti_total")
    if ins:
        l1.append(f"・外資 {_n(ins['foreign']/1e7, 1)} 萬張｜投信 {_n(ins['trust']/1e7, 1)} 萬張")
    if m.get("margin_chg_e") is not None:
        l1.append(f"・融資 {m['margin_chg_e']:+.0f} 億")
    if reg.get("foreign_oi") is not None:
        l1.append(f"・外資期貨淨 {reg['foreign_oi']:+,.0f} 口")
    l1 += ["", "〈🎯訊號〉", f"・{rep['breaker']['text']}",
           f"・級距 {reg['level']['label']}：單檔≤{reg['level']['single_max']:g}%、現金≥{reg['level']['cash_min']}%",
           f"・警報 {reg['alarm_count']}/4 亮｜中長期{reg['longterm']['label']}", FOOT]
    msgs.append("\n".join(l1))

    # ② 外盤＋環境
    g = rep.get("global", {})
    l2 = [f"【🌏 外盤＋環境 {today}】", "", "〈🇺🇸 美股（🟡B）〉"]
    for sym in ("^DJI", "^GSPC", "^IXIC", "^SOX", "^VIX", "TSM", "NVDA"):
        if sym in g:
            l2.append(f"・{g[sym]['name']} {_n(g[sym]['close'], 2 if g[sym]['close'] < 1000 else 0)}｜{_p(g[sym]['pct'], 2)}")
        else:
            l2.append(f"・{sym} ⚪暫缺")
    ng = m.get("night")
    l2 += ["", "〈🌙 台指夜盤（🔵A）〉"]
    if ng and str(ng["session_end"]) > rep["basis"]:
        l2.append(f"・收 {_n(ng['close'])}｜{_p(ng['pct'], 2)}")
        diff = ng["close"] - reg["close"]
        l2.append(f"・較現貨{'高' if diff >= 0 else '低'}約 {abs(diff):,.0f} 點")
    else:
        l2.append("・⚪暫缺")
    ev = rep.get("events") or []
    l2 += ["", "〈🗓️ 事件雷達〉"] + ([f"・{e}" for e in ev[:3]] or ["・⚪暫缺"])
    hd = [n["title"] for n in (rep.get("news") or [])[:2]]
    if hd:
        l2 += ["", "〈📰 頭條〉"] + [f"・{t[:22]}" for t in hd]
    l2.append(FOOT)
    msgs.append("\n".join(l2))

    # ③ 持股
    pf = rep.get("portfolio")
    if pf:
        l3 = [f"【💼 持股 {today}】（{basis} 收盤）", ""]
        for r in pf["rows"]:
            note = r.get("short") if r["light"] != "🟢" else f"{_p(r['pnl_pct'], 0)} 續抱"
            l3.append(f"{r['light']} {r['code']} {r['name']} {_n(r['price'], 2 if (r['price'] or 0) < 100 else 0)}｜{note}")
        l3 += ["", f"總覽：市值約 {pf['market_value']/1e4:,.0f} 萬，未實現 {_p(pf['pnl_pct'])}"]
        acts = pf["actions"][:4]
        if acts:
            l3 += ["", "🚫 第一行動"]
            for a in acts:
                head, _, tail = a["text"].partition("，")
                l3.append(f"・{head}")
                if tail:
                    l3.append(f"　{tail}")
        l3.append(FOOT)
    else:
        l3 = [f"【💼 持股 {today}】", "", "・尚未登錄持股", "・請到網站「持股」頁新增", FOOT]
    msgs.append("\n".join(l3))

    # ④ 中長期觀察池
    l4 = [f"【👀 中長期觀察池 {today}】", rep["breaker"]["text"], f"大盤濾網：{reg['longterm']['label']}（{reg['longterm']['why']}）", ""]
    mt = rep["midterm"]
    if mt:
        l4.append("〈相對吸籌〉")
        for p in mt:
            l4.append(f"🔸 {p['code']} {p['name']} {_n(p['close'], 1)}｜{p['status']}")
            l4.append(f"　吸籌 {p['accum']}%｜停損 {_n(p['stop'], 1)}")
    else:
        l4.append("・今日無合格標的（不硬湊）")
    cp = [p for p in rep.get("composite", []) if p["code"] not in {x["code"] for x in mt}][:2]
    if cp:
        l4 += ["", "〈🧠 綜合評分高分〉"] + [f"🔸 {p['code']} {p['name']} {p['score']}分｜{p['action']}" for p in cp]
    vp = rep.get("value", [])[:2]
    if vp:
        l4 += ["", "〈💎 低估值〉"] + [f"🔸 {p['code']} {p['name']} 殖{p['yield']:.1f}% PE{p['pe']:.1f}" for p in vp]
    st = rep["stats"]["中長期"]
    if st.get("n"):
        l4 += ["", f"近{st['n']}筆：勝率 {st['win_rate']}%｜期望值 {st['expectancy']:+.2f}%"]
    l4.append(FOOT)
    msgs.append("\n".join(l4))

    # ⑤ 當沖
    d = rep["daytrade"]
    v = d["verdict"]
    l5 = [f"【🎯 當沖候選 {today}】{v['label']}"]
    if v["why"]:
        l5 += [f"・{w}" for w in v["why"][:3]]
    l5.append("")
    if d["picks"]:
        for p in d["picks"]:
            l5.append(f"🔸 {p['code']} {p['name']} {_n(p['close'], 1)}｜振幅{p['amp8']}%")
            l5.append(f"　1張約{p['lot_cost_w']}萬，50萬可買{p['lots']}張{('｜' + p['tag'][:6]) if p['tag'] else ''}")
        l5 += ["", "〈📌 鐵律〉", "・站穩 VWAP 才進、破 VWAP 走", "・-2~3% 硬停損、15~20分不動先退",
               "・半數 +3~5% 落袋、餘移動停利", "・收盤前一定平倉"]
    else:
        l5.append("・今日無合格標的")
    if v["key"] == "red":
        l5 += ["", "🚫 今日停做：只追蹤、不進場"]
    if site_url:
        l5 += ["", f"🔗 完整報告 {site_url}"]
    l5.append(FOOT)
    msgs.append("\n".join(l5))
    return [{"type": "text", "text": t[:4900]} for t in msgs]


def push(messages: list[dict]) -> tuple[bool, str]:
    token, uid = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"), os.environ.get("LINE_USER_ID")
    if not token or not uid:
        return False, "未設定 LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID"
    ok_all, info = True, []
    for i in range(0, len(messages), 5):
        r = requests.post("https://api.line.me/v2/bot/message/push", timeout=30,
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                          data=json.dumps({"to": uid, "messages": messages[i:i + 5]}, ensure_ascii=False).encode())
        info.append(f"HTTP {r.status_code}")
        ok_all &= r.status_code == 200
    return ok_all, "、".join(info)

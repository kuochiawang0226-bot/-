"""LINE 推播：網站為主，LINE 每天早上只推 1 則摘要＋網站連結。

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


def build(rep: dict, site_url: str | None = None, include_holdings: bool = True) -> list[dict]:
    """網站為主、LINE 為輔：每天早上 1 則摘要，詳細內容點連結看網站。"""
    m, today = rep["market"], _md(rep["report_date"])
    reg, brk = m["regime"], rep["breaker"]
    tone = {"green": "🟢", "yellow": "🟡", "white": "⚪"}[reg["level"]["key"]]
    L = [f"【台股晨報 {today}】{tone} {reg['stage']}",
         f"加權 {_n(reg['close'])}（{_p(reg['pct'], 2)}）｜距月線 {_p(reg['vs_ma20'])}"]
    ng, g = m.get("night"), rep.get("global", {})
    ext = []
    if ng and str(ng.get("session_end")) > rep["basis"]:
        ext.append(f"夜盤 {_p(ng['pct'], 2)}")
    if "^SOX" in g:
        ext.append(f"費半 {_p(g['^SOX']['pct'], 2)}")
    if "^VIX" in g:
        ext.append(f"VIX {g['^VIX']['close']:.1f}")
    if ext:
        L.append("｜".join(ext))
    L += ["", "🚦 今日定調",
          f"・中長期：{reg['longterm']['label']}{'（斷路器啟動）' if brk['on'] else ''}",
          f"・當沖：{reg['daytrade']['label']}",
          f"・部位：單檔≤{reg['level']['single_max']:g}%、現金≥{reg['level']['cash_min']}%"]
    pf = rep.get("portfolio")
    if include_holdings and pf:
        acts = pf.get("actions") or []
        if acts:
            L += ["", f"💼 持股要處理 {len(acts)} 項"]
            for a in acts[:3]:
                L.append("・" + a["text"].split("，")[0])
            if len(acts) > 3:
                L.append(f"・其餘 {len(acts) - 3} 項請看網站")
        else:
            L += ["", "💼 持股：沒有超標或破停損"]
    elif not include_holdings:
        L += ["", "💼 持股：（只在 LINE 與解鎖後的網站顯示）"]
    go = [p for p in rep.get("midterm", []) if "🟢" in p.get("status", "")]
    watch = [f"{p['code']} {p['name']}" for p in rep.get("midterm", [])[:3]]
    if go:
        L += ["", "🎯 可分批：" + "、".join(f"{p['code']} {p['name']}" for p in go)]
    elif watch:
        L += ["", "👀 觀察：" + "、".join(watch)]
    if site_url:
        L += ["", f"🔗 完整內容與當沖即時：{site_url}"]
    L.append(FOOT)
    return [{"type": "text", "text": "\n".join(L)}]


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

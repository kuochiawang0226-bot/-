"""盤中即時更新：持股與觀察名單報價＋當沖即時判斷，寫入 site/data/live.json。

python -m engine.run_live                 # 一般更新
python -m engine.run_live --asof 09:45    # 回放：用今天的 1 分 K 模擬 09:45 當下的判斷（測試用）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import os

from . import intraday, sources as src
from .crypto import encrypt
from .run_daily import TW, load_holdings

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site" / "data"


def avg_volumes(codes: list[str]) -> dict:
    """近 5 日平均成交量（股），讀每日產生的個股詳細檔。"""
    out = {}
    for c in codes:
        p = SITE / "stock" / f"{c}.json"
        if p.exists():
            bars = json.loads(p.read_text(encoding="utf-8")).get("bars") or []
            vs = [b[5] for b in bars[-5:] if b[5]]
            if vs:
                out[c] = sum(vs) / len(vs)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", help="回放時間 HH:MM")
    a = ap.parse_args()
    now = dt.datetime.now(TW)
    rep_p = SITE / "report.json"
    rep = json.loads(rep_p.read_text(encoding="utf-8")) if rep_p.exists() else {}

    # ── 一般報價（持股＋各名單＋成交金額前 30）
    codes: dict[str, str] = {}
    held: set[str] = set()
    try:
        for h in load_holdings().get("holdings", []):
            codes[h["stock_id"]] = "上市"
            held.add(h["stock_id"])
    except Exception as e:  # noqa: BLE001
        print("讀取持股失敗：", e, file=sys.stderr)
    public: set[str] = set()
    for key in ("midterm", "composite", "value"):
        for p in rep.get(key, []):
            codes[p["code"]] = p.get("market", "上市")
            public.add(p["code"])
    dtp = rep.get("daytrade") or {}
    for p in dtp.get("watch", []) or dtp.get("picks", []):
        codes[p["code"]] = p.get("market", "上市")
        public.add(p["code"])
    stocks_p = SITE / "stocks.json"
    if stocks_p.exists():
        table = json.loads(stocks_p.read_text(encoding="utf-8"))
        mk = {r["c"]: {"市": "上市", "櫃": "上櫃", "興": "興櫃"}.get(r["m"], "上市") for r in table}
        for r in sorted(table, key=lambda r: -(r.get("amt") or 0))[:30]:   # 成交金額前 30 大一併更新
            codes.setdefault(r["c"], mk[r["c"]])
            public.add(r["c"])
        codes = {c: mk.get(c, m) for c, m in codes.items()}
    q = src.realtime_quotes(sorted(codes.items())) if not a.asof else {}
    idx = {}
    for sym, name in (("^TWII", "加權指數"),):
        try:
            y = src.yahoo_chart(sym, "1d", "5m", cache=False)
            idx[sym] = {"name": name, "price": y["price"], "prev": y["prev_close"],
                        "time": y["time"].strftime("%H:%M"), "intraday": [round(b["close"], 2) for b in y["bars"]]}
        except Exception:  # noqa: BLE001
            pass

    # ── 當沖即時判斷
    asof = dt.time.fromisoformat(a.asof) if a.asof else None
    watch = dtp.get("watch") or dtp.get("picks") or []
    mon = intraday.monitor(dtp, now, avg_volumes([w["code"] for w in watch]), asof) if watch else None

    # 只因為持有才抓的報價：有密碼時改放加密檔，公開網站看不出持有哪些股票
    pw = os.environ.get("STOCK_PASSWORD")
    private_q = {c: v for c, v in q.items() if c in held and c not in public}
    SITE.mkdir(parents=True, exist_ok=True)
    q = {c: v for c, v in q.items() if c not in private_q}
    if pw:
        (SITE / "live_private.enc.json").write_text(json.dumps(encrypt({"quotes": private_q}, pw)), encoding="utf-8")
    else:  # 本機／預覽模式：明文檔已列入 .gitignore，不會上傳
        (SITE / "live_private.json").write_text(json.dumps({"quotes": private_q}, ensure_ascii=False), encoding="utf-8")
    (SITE / "live.json").write_text(json.dumps({"updated": now.isoformat(timespec="minutes"), "quotes": q, "index": idx,
                                                "daytrade": mon}, ensure_ascii=False), encoding="utf-8")
    if mon:
        c = mon["counts"]
        print(f"當沖即時（{mon['updated'][11:16]}）：可進 {c['go']}、觀察 {c['watch']}、不適合 {c['no']}")
        for i in mon["items"]:
            print(f"  {i['label']:<14} {i['code']} {i['name']} 現價 {i.get('price')} VWAP {i.get('vwap')}  {'；'.join(i.get('reasons', []))[:70]}")
    print(f"即時報價 {len(q)} 檔、指數 {len(idx)} 個")


if __name__ == "__main__":
    main()

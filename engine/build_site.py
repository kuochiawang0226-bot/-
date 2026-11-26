"""把 site/ 組成可部署的 _site/（補上完整 HTML 外框；不複製未加密的個資檔）。"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, OUT = ROOT / "site", ROOT / "_site"
HEAD = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="robots" content="noindex,nofollow"></head><body>')
import os

# 未加密個資絕不部署；沒設密碼時 LINE 預覽含持股，也不部署
SKIP = {"private.json", "live_private.json"} | (set() if os.environ.get("STOCK_PASSWORD") else {"line_preview.json"})


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "data").mkdir(parents=True)
    (OUT / "index.html").write_text(HEAD + (SRC / "index.html").read_text(encoding="utf-8") + "</body></html>",
                                    encoding="utf-8")
    for f in (SRC / "data").glob("*.json"):
        if f.name not in SKIP:
            shutil.copy2(f, OUT / "data" / f.name)
    if (SRC / "data" / "stock").exists():
        shutil.copytree(SRC / "data" / "stock", OUT / "data" / "stock")
    (OUT / ".nojekyll").write_text("")
    print("網站已組好：", sorted(p.name for p in (OUT / "data").iterdir()))


if __name__ == "__main__":
    main()

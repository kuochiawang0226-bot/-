"""把 site/ 組成可部署的 _site/（補上完整 HTML 外框、App 圖示與安裝設定；不複製未加密的個資檔）。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, OUT = ROOT / "site", ROOT / "_site"
HEAD = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="robots" content="noindex,nofollow">'
        # 手機主畫面 App 圖示與全螢幕開啟
        '<link rel="icon" type="image/svg+xml" href="icons/icon.svg">'
        '<link rel="icon" type="image/png" sizes="32x32" href="icons/favicon-32.png">'
        '<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">'
        '<link rel="manifest" href="manifest.webmanifest">'
        '<meta name="theme-color" content="#0f1a26">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-title" content="台股作戰台">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        # 全螢幕時避開瀏海與底部橫條
        '<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
        '[hidden]{display:none!important}</style>'
        '</head><body>')

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
    # 個股詳細檔：本機剛產生的優先，否則用 data 分支（排程會把它取到 databranch/）
    stock_dir = SRC / "data" / "stock" if (SRC / "data" / "stock").exists() else ROOT / "databranch" / "stock"
    if stock_dir.exists():
        shutil.copytree(stock_dir, OUT / "data" / "stock")
    if (SRC / "icons").exists():
        shutil.copytree(SRC / "icons", OUT / "icons")
    if (SRC / "manifest.webmanifest").exists():
        shutil.copy2(SRC / "manifest.webmanifest", OUT / "manifest.webmanifest")
    (OUT / ".nojekyll").write_text("")
    print("網站已組好：", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()

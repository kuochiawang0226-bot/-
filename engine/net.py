"""HTTP 抓取層：限流、重試、快取。

- 證交所連續請求會回 307 或安全阻擋頁，因此同主機請求間隔至少 3.5 秒，並自動重試。
- 同一天抓過的資料存到 .cache/，重跑時不再打官方網站。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 各主機最小間隔（秒）
HOST_GAP = {
    "www.twse.com.tw": 5.0,
    "openapi.twse.com.tw": 1.0,
    "www.tpex.org.tw": 1.5,
    "www.taifex.com.tw": 2.0,
    "mis.twse.com.tw": 1.0,
}
_last: dict[str, float] = {}
_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9"})


class FetchError(RuntimeError):
    pass


def _wait(host: str) -> None:
    gap = HOST_GAP.get(host, 0.3)
    dt = time.time() - _last.get(host, 0)
    if dt < gap:
        time.sleep(gap - dt)
    _last[host] = time.time()


def _cache_path(url: str) -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    return CACHE / f"{h}.dat"


def get(url: str, *, cache: bool = True, retries: int = 4, timeout: int = 40,
        headers: dict | None = None) -> str:
    """回傳文字內容。cache=True 時同一個網址只抓一次（適合歷史/盤後資料）。"""
    cp = _cache_path(url)
    if cache and cp.exists():
        return cp.read_text(encoding="utf-8")
    host = urlparse(url).netloc
    err: Exception | None = None
    for i in range(retries):
        _wait(host)
        try:
            r = _session.get(url, timeout=timeout, headers=headers or {}, allow_redirects=False)
            if r.status_code in (301, 302, 307, 308, 429, 503):
                raise FetchError(f"HTTP {r.status_code}（限流）")
            if r.status_code == 404:
                raise FetchError(f"{url} 不存在（404）")
            r.raise_for_status()
            r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
            text = r.text
            if "FOR SECURITY REASONS" in text[:2000]:
                raise FetchError("被網站安全機制阻擋")
            if cache:
                CACHE.mkdir(exist_ok=True)
                cp.write_text(text, encoding="utf-8")
            return text
        except Exception as e:  # noqa: BLE001
            err = e
            if "404" in str(e):
                break
            # 證交所被限流後要冷卻較久才會恢復
            time.sleep((20 if host == "www.twse.com.tw" else 4) * (i + 1))
    raise FetchError(f"{url} 抓取失敗：{err}")


def get_json(url: str, **kw):
    text = get(url, **kw)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # 壞資料不要留在快取裡
        _cache_path(url).unlink(missing_ok=True)
        raise FetchError(f"{url} 不是 JSON：{text[:120]!r}") from e


def clear_cache_older_than(days: int = 10) -> None:
    if not CACHE.exists():
        return
    cutoff = time.time() - days * 86400
    for p in CACHE.iterdir():
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default

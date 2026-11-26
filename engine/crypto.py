"""個資加密：AES-256-GCM＋PBKDF2-SHA256（25 萬次）。

格式與網站端 WebCrypto 完全相容：{"v":1,"salt":b64,"iv":b64,"ct":b64}（ct 末 16 bytes 為驗證標籤）。
持股、損益等個資只以加密形式存進存放庫與網站；密碼存在 GitHub Secrets（STOCK_PASSWORD）與您自己的瀏覽器。
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITER = 250_000


def _key(password: str, salt: bytes) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(password.encode())


def encrypt(obj, password: str) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(password, salt)).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode(), None)
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return {"v": 1, "iter": ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def decrypt(blob: dict, password: str):
    d = lambda k: base64.b64decode(blob[k])  # noqa: E731
    pt = AESGCM(_key(password, d("salt"))).decrypt(d("iv"), d("ct"), None)
    return json.loads(pt.decode())

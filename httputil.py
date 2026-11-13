"""带代理和重试的 HTTP JSON / 文本请求。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

UA = "arcus-signal/1.0"


def opener_for(proxy: str | None) -> urllib.request.OpenerDirector:
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def get_bytes(url: str, proxy: str | None = None, timeout: int = 25, retries: int = 3) -> bytes:
    last: Exception | None = None
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    opener = opener_for(proxy)
    for i in range(retries):
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise RuntimeError(str(last) if last else "请求失败")


def get_json(url: str, proxy: str | None = None, timeout: int = 25, retries: int = 3) -> Any:
    raw = get_bytes(url, proxy=proxy, timeout=timeout, retries=retries)
    return json.loads(raw.decode() or "null")


def get_text(url: str, proxy: str | None = None, timeout: int = 25, retries: int = 3) -> str:
    raw = get_bytes(url, proxy=proxy, timeout=timeout, retries=retries)
    return raw.decode("utf-8", "replace")

"""从本文件夹读取 API.txt。只校验格式，绝不打印密钥。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

REQUIRED = ("ARCUS_SIGNING_KEY", "ARCUS_ACCOUNT_INDEX", "ARCUS_WALLET_ADDRESS")
_SECRET_HEX = re.compile(r"(?i)(?:0x)?[0-9a-f]{64}")


class ConfigError(Exception):
    pass


@dataclass
class Keys:
    arcus_signing_key: str
    arcus_account_index: int
    arcus_wallet_address: str
    proxy: str | None
    source_name: str


def redact(text: str) -> str:
    if text is None:
        return ""
    return _SECRET_HEX.sub("[已隐藏]", str(text))


def short_addr(addr: str) -> str:
    a = (addr or "").strip()
    if len(a) >= 12 and a.lower().startswith("0x"):
        return f"{a[:6]}…{a[-4:]}"
    return a or "—"


def _parse_kv_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def _norm_hex64(raw: str, field: str) -> str:
    s = (raw or "").strip()
    if s.startswith(("0x", "0X")):
        s = s[2:]
    if len(s) != 64 or any(c not in "0123456789abcdefABCDEF" for c in s):
        raise ConfigError(f"{field} 格式不对：应为 64 位十六进制（可带或不带 0x）。")
    return s.lower()


def _norm_addr(raw: str, field: str) -> str:
    s = (raw or "").strip()
    if not s.startswith(("0x", "0X")):
        s = "0x" + s
    body = s[2:]
    if len(body) != 40 or any(c not in "0123456789abcdefABCDEF" for c in body):
        raise ConfigError(f"{field} 格式不对：应为 0x 加 40 位十六进制地址。")
    return "0x" + body.lower()


def apply_proxy(proxy: str | None) -> None:
    if not proxy:
        return
    os.environ["HTTP_PROXY"] = proxy
    os.environ["HTTPS_PROXY"] = proxy
    os.environ["http_proxy"] = proxy
    os.environ["https_proxy"] = proxy


def load_proxy_only(directory: Path | None = None) -> str | None:
    directory = Path(directory) if directory else Path(__file__).resolve().parent
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("https_proxy") or "").strip()
    for name in ("API.txt", ".env"):
        p = directory / name
        if p.is_file():
            data = _parse_kv_file(p)
            proxy = (data.get("PROXY") or proxy).strip() or proxy
            break
    return proxy or None


def load_keys(directory: Path | None = None) -> Keys:
    directory = Path(directory) if directory else Path(__file__).resolve().parent
    path = None
    for name in ("API.txt", ".env"):
        if (directory / name).is_file():
            path = directory / name
            break
    if path is None:
        raise ConfigError(f"未找到配置文件。请把 API.txt 放在 {directory} 下，不要把密钥发到聊天里。")
    data = _parse_kv_file(path)
    missing = [k for k in REQUIRED if not str(data.get(k, "")).strip()]
    if missing:
        raise ConfigError(f"{path.name} 缺少字段：{', '.join(missing)}")
    try:
        account_index = int(str(data["ARCUS_ACCOUNT_INDEX"]).strip())
    except ValueError as e:
        raise ConfigError("ARCUS_ACCOUNT_INDEX 必须是 0–9 的整数。") from e
    if account_index < 0 or account_index > 9:
        raise ConfigError("ARCUS_ACCOUNT_INDEX 必须是 0–9 的整数。")
    proxy = (data.get("PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").strip() or None
    return Keys(
        arcus_signing_key=_norm_hex64(data["ARCUS_SIGNING_KEY"], "ARCUS_SIGNING_KEY"),
        arcus_account_index=account_index,
        arcus_wallet_address=_norm_addr(data["ARCUS_WALLET_ADDRESS"], "ARCUS_WALLET_ADDRESS"),
        proxy=proxy,
        source_name=path.name,
    )

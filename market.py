"""免费行情：CoinGecko 市值榜、币安现货K线、币安合约持仓/资金费/多空比。"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from httputil import get_json

SH = timezone(timedelta(hours=8))

STABLE_SYMBOLS = {
    "USDT", "USDC", "DAI", "FDUSD", "USDE", "TUSD", "USDD", "FRAX", "PYUSD", "USD1",
    "USDP", "GUSD", "LUSD", "CRLUSD", "USDS", "BUSD", "EURC", "EURT", "USDG", "USD0",
    "USDY", "GHO", "SUSD", "CRVUSD", "AEUR", "EURS", "CUSD", "OUSD", "USDX", "DOLA",
    "MIM", "USDJ", "USTC", "USDN", "RSV", "USDB", "HUSD", "USDK", "IDRT", "BIDR", "UST",
}
GOLD_PEGS = {"PAXG", "XAUT"}
WRAPS = {
    "WBTC", "WETH", "STETH", "CBETH", "RETH", "WEETH", "WSTETH", "TBTC", "WBETH",
    "METH", "RSETH", "EZETH", "SWETH", "MSOL", "JITOSOL", "BNSOL", "CBBTC", "LBTC",
    "SOLVBTC", "WBNB", "WAVAX",
}
NAME_HINTS = [
    "usd coin", "tether", "trueusd", "first digital usd", "ethena usde", "dai stable",
    "paypal usd", "global dollar", "wrapped bitcoin", "wrapped ether", "pax gold",
    "tether gold", "wrapped staked", "binance peg", "bridged ",
]
ALIASES = {"RENDER": "RENDER", "RNDR": "RENDER", "POL": "POL", "MATIC": "POL", "BEAM": "BEAMX"}

CG_MARKETS = (
    "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
    "&order=market_cap_desc&per_page=150&page=1&sparkline=false"
    "&price_change_percentage=1h,24h,7d"
)
BN_SPOT = "https://api.binance.com/api/v3"
BN_FAPI = "https://fapi.binance.com"


def is_excluded(coin: dict) -> str | None:
    s = (coin.get("symbol") or "").upper()
    name = (coin.get("name") or "").lower()
    cid = (coin.get("id") or "").lower()
    if s in STABLE_SYMBOLS or s in GOLD_PEGS:
        return "stablecoin/peg"
    if s in WRAPS:
        return "wrapped/peg"
    if any(h in name for h in NAME_HINTS):
        return "stablecoin/peg-name"
    if "wrapped-bitcoin" in cid or "wrapped-ethereum" in cid:
        return "wrapped/peg"
    return None


def ts_shanghai(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, SH).strftime("%Y-%m-%d %H:%M")


def fetch_watchlist(proxy: str | None = None) -> list[dict]:
    raw = get_json(CG_MARKETS, proxy=proxy, timeout=40)
    if not isinstance(raw, list):
        raise RuntimeError("CoinGecko 市值榜格式异常。")
    kept = []
    for c in raw:
        if is_excluded(c):
            continue
        kept.append({
            "cg_rank": c.get("market_cap_rank"),
            "id": c.get("id"),
            "symbol": (c.get("symbol") or "").upper(),
            "name": c.get("name"),
            "price": c.get("current_price"),
            "market_cap": c.get("market_cap"),
            "total_volume": c.get("total_volume"),
        })
        if len(kept) >= 100:
            break
    return kept


def fmt_market_cap(cap: float) -> str:
    yi = float(cap) / 1e8
    if yi >= 0.1:
        s = f"{yi:.2f}".rstrip("0").rstrip(".")
        return f"{s}亿美元"
    wan = float(cap) / 1e4
    return f"{wan:.0f}万美元"


def lookup_market_cap(symbol: str, caps: dict) -> float | None:
    s = (symbol or "").upper().replace("-USD", "").replace("-USDT", "").replace("-USDC", "")
    keys = []
    for item in (s, ALIASES.get(s, s)):
        if item and item not in keys:
            keys.append(item)
    for key in keys:
        row = caps.get(key)
        if not row:
            continue
        try:
            return float(row["market_cap"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def crypto_skip_reason(cap: float | None, min_cap: float) -> str | None:
    if not min_cap or min_cap <= 0:
        return None
    if cap is None:
        return "未查到市值"
    if cap < min_cap:
        return f"市值约 {fmt_market_cap(cap)}，低于 {fmt_market_cap(min_cap)}"
    return None


_cap_cache: dict[str, Any] = {"ts": 0.0, "data": {}, "error": None}


def fetch_crypto_market_caps(proxy: str | None = None, min_cap: float = 100_000_000) -> dict[str, dict]:
    """分页拉取 CoinGecko 市值，直到市值落到门槛以下。同一代码保留市值更高的一条。"""
    out: dict[str, dict] = {}
    per_page = 250
    for page in range(1, 6):
        url = (
            "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
            f"&order=market_cap_desc&per_page={per_page}&page={page}&sparkline=false"
        )
        raw = get_json(url, proxy=proxy, timeout=40)
        if not isinstance(raw, list) or not raw:
            break
        last_cap = None
        for c in raw:
            try:
                cap_f = float(c["market_cap"]) if c.get("market_cap") is not None else None
            except (TypeError, ValueError):
                cap_f = None
            last_cap = cap_f
            symbol = (c.get("symbol") or "").upper()
            if not symbol or cap_f is None:
                continue
            prev = out.get(symbol)
            if prev is None or cap_f > float(prev["market_cap"]):
                out[symbol] = {
                    "market_cap": cap_f,
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "cg_rank": c.get("market_cap_rank"),
                }
        if last_cap is not None and last_cap < min_cap:
            break
        if len(raw) < per_page:
            break
        time.sleep(1.2)
    if not out:
        raise RuntimeError("CoinGecko 市值榜为空。")
    return out


def get_crypto_market_caps(proxy: str | None = None, min_cap: float = 100_000_000, max_age: float = 6 * 3600) -> tuple[dict, str | None]:
    """返回 (市值表, 错误说明)。拉取失败时，6 小时内沿用上次成功结果。"""
    now = time.time()
    try:
        data = fetch_crypto_market_caps(proxy, min_cap=min_cap)
    except Exception as e:
        err = str(e)
        cached = _cap_cache.get("data") or {}
        ts = float(_cap_cache.get("ts") or 0)
        if cached and now - ts <= max_age:
            return cached, f"{err}，改用 {int((now - ts) / 60)} 分钟前的市值缓存"
        return {}, err
    _cap_cache["ts"] = now
    _cap_cache["data"] = data
    _cap_cache["error"] = None
    return data, None



def fetch_binance_spot_usdt(proxy: str | None = None) -> set[str]:
    info = get_json(f"{BN_SPOT}/exchangeInfo", proxy=proxy, timeout=40)
    out = set()
    for s in info.get("symbols") or []:
        if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
            out.add(s["symbol"])
    return out


def fetch_binance_fut_usdt(proxy: str | None = None) -> set[str]:
    try:
        info = get_json(f"{BN_FAPI}/fapi/v1/exchangeInfo", proxy=proxy, timeout=40)
    except Exception:
        return set()
    out = set()
    for s in info.get("symbols") or []:
        if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL":
            out.add(s["symbol"])
    return out


def map_pair(symbol: str, spot: set[str]) -> str | None:
    base = ALIASES.get(symbol, symbol)
    if base and f"{base}USDT" in spot:
        return f"{base}USDT"
    if symbol == "BEAM" and "BEAMXUSDT" in spot:
        return "BEAMXUSDT"
    return None


def parse_klines(raw: list) -> list[dict]:
    rows = []
    for c in raw:
        rows.append({
            "ot": int(c[0]),
            "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4]),
            "v": float(c[5]), "ct": int(c[6]),
            "ts": ts_shanghai(int(c[0])),
        })
    return rows


def fetch_klines(pair: str, interval: str, limit: int = 120, proxy: str | None = None) -> list[dict]:
    url = f"{BN_SPOT}/klines?symbol={pair}&interval={interval}&limit={limit}"
    raw = get_json(url, proxy=proxy, timeout=30)
    if not isinstance(raw, list):
        raise RuntimeError(f"K线格式异常：{pair} {interval}")
    return parse_klines(raw)


def _safe_json(url: str, proxy: str | None) -> Any:
    try:
        return get_json(url, proxy=proxy, timeout=20, retries=2)
    except Exception:
        return None


def fetch_deriv(pair: str, proxy: str | None = None) -> dict[str, Any]:
    """币安 U 本位合约公开数据。没有对应永续时返回 available=False。"""
    oi_now = _safe_json(f"{BN_FAPI}/fapi/v1/openInterest?symbol={pair}", proxy)
    oi_hist = _safe_json(
        f"{BN_FAPI}/futures/data/openInterestHist?symbol={pair}&period=4h&limit=8", proxy
    )
    funding = _safe_json(f"{BN_FAPI}/fapi/v1/fundingRate?symbol={pair}&limit=4", proxy)
    premium = _safe_json(f"{BN_FAPI}/fapi/v1/premiumIndex?symbol={pair}", proxy)
    ls = _safe_json(
        f"{BN_FAPI}/futures/data/globalLongShortAccountRatio?symbol={pair}&period=4h&limit=4", proxy
    )
    taker = _safe_json(
        f"{BN_FAPI}/futures/data/takerlongshortRatio?symbol={pair}&period=4h&limit=4", proxy
    )
    if not any([oi_now, oi_hist, funding, premium]):
        return {"available": False, "pair": pair}

    def _f(x, *keys):
        if isinstance(x, dict):
            for k in keys:
                if x.get(k) not in (None, ""):
                    try:
                        return float(x[k])
                    except (TypeError, ValueError):
                        pass
        return None

    oi = None
    oi_chg = None
    if isinstance(oi_now, dict):
        oi = _f(oi_now, "openInterest")
    if isinstance(oi_hist, list) and len(oi_hist) >= 2:
        a, b = _f(oi_hist[-2], "sumOpenInterest"), _f(oi_hist[-1], "sumOpenInterest")
        if a and b:
            oi = b
            oi_chg = (b / a - 1) * 100
    fund = None
    if isinstance(funding, list) and funding:
        fund = _f(funding[-1], "fundingRate")
        if fund is not None:
            fund *= 100  # 转成百分数，0.01 表示 0.01%
    elif isinstance(premium, dict):
        fund = _f(premium, "lastFundingRate")
        if fund is not None:
            fund *= 100
    ls_ratio = None
    if isinstance(ls, list) and ls:
        ls_ratio = _f(ls[-1], "longShortRatio")
    taker_ratio = None
    if isinstance(taker, list) and taker:
        taker_ratio = _f(taker[-1], "buySellRatio")
    return {
        "available": True,
        "pair": pair,
        "oi": oi,
        "oi_chg_pct": oi_chg,
        "funding_pct": fund,
        "long_short_ratio": ls_ratio,
        "taker_buy_sell": taker_ratio,
    }


def classify_price_vs_oi(price_chg: float | None, oi_chg: float | None, eps: float = 0.2) -> str:
    if price_chg is None or oi_chg is None:
        return "na"
    if abs(price_chg) < eps and abs(oi_chg) < eps:
        return "flat"
    if price_chg > 0 and oi_chg > 0:
        return "new_longs"
    if price_chg > 0 and oi_chg < 0:
        return "short_cover"
    if price_chg < 0 and oi_chg > 0:
        return "new_shorts"
    if price_chg < 0 and oi_chg < 0:
        return "long_cover"
    return "mixed"


def fetch_fear_greed(proxy: str | None = None) -> dict | None:
    data = _safe_json("https://api.alternative.me/fng/?limit=1", proxy)
    if not isinstance(data, dict):
        return None
    rows = data.get("data") or []
    if not rows:
        return None
    row = rows[0]
    return {"value": row.get("value"), "label": row.get("value_classification")}


def fetch_many_klines(pairs: list[str], proxy: str | None = None, workers: int = 8) -> dict[str, dict]:
    out: dict[str, dict] = {}

    def one(pair: str):
        d1 = fetch_klines(pair, "1d", 80, proxy)
        time.sleep(0.05)
        h4 = fetch_klines(pair, "4h", 120, proxy)
        return pair, {"d1": d1, "h4": h4}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, p) for p in pairs]
        for fut in as_completed(futs):
            try:
                pair, data = fut.result()
                out[pair] = data
            except Exception as e:
                # 单个失败不中断整轮
                pass
    return out



ARCUS_CANDLES = "https://api.arcus.xyz/v1/candles"


def ts_shanghai_us(us: int) -> str:
    return datetime.fromtimestamp(us / 1_000_000, SH).strftime("%Y-%m-%d %H:%M")


def parse_arcus_candles(raw: list) -> list[dict]:
    rows = []
    for c in raw or []:
        try:
            ot = int(c.get("openTime") or 0)
            rows.append({
                "ot": ot // 1000,
                "o": float(c["open"]),
                "h": float(c["high"]),
                "l": float(c["low"]),
                "c": float(c["close"]),
                "v": float(c.get("volume") or 0),
                "ts": ts_shanghai_us(ot),
            })
        except (TypeError, ValueError, KeyError):
            continue
    rows.sort(key=lambda x: x["ot"])
    return rows


def fetch_arcus_candles(market: str, timeframe: str, days: int = 80, proxy: str | None = None) -> list[dict]:
    import time as _t
    now_us = int(_t.time() * 1_000_000)
    from_us = now_us - int(days * 86400 * 1_000_000)
    url = f"{ARCUS_CANDLES}?market={market}&timeframe={timeframe}&from={from_us}&to={now_us}"
    body = get_json(url, proxy=proxy, timeout=30, retries=3)
    if not isinstance(body, dict):
        raise RuntimeError(f"Arcus K线格式异常：{market} {timeframe}")
    return parse_arcus_candles(body.get("candles") or [])


def fetch_many_arcus_candles(markets: list[str], proxy: str | None = None, workers: int = 8) -> dict[str, dict]:
    out: dict[str, dict] = {}

    def one(name: str):
        d1 = fetch_arcus_candles(name, "1d", 90, proxy)
        time.sleep(0.04)
        h4 = fetch_arcus_candles(name, "4h", 40, proxy)
        return name, {"d1": d1, "h4": h4}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, m) for m in markets]
        for fut in as_completed(futs):
            try:
                name, data = fut.result()
                out[name] = data
            except Exception:
                pass
    return out

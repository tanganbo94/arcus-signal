"""只用 Arcus 查账户和下单。默认不会发送订单。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Optional
from zoneinfo import ZoneInfo

from httputil import opener_for
from keys import Keys, redact, short_addr

ARCUS_BASE = "https://api.arcus.xyz"
UA = "arcus-signal/1.0"
TZ = ZoneInfo("Asia/Shanghai")
SLIPPAGE = Decimal("0.05")
GTT_MONTHS_US = 40 * 86_400 * 1_000_000
OP_PLACE = 1
OP_CANCEL = 2
OP_PLACE_UNTRIGGERED = 4
SIDE = {"BUY": 0, "SELL": 1}
TIF = {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}

ed25519 = None


class TradeError(Exception):
    pass


def _load_crypto() -> None:
    global ed25519
    if ed25519 is not None:
        return
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed
    except ImportError:
        raise SystemExit("缺少依赖。请在本文件夹执行： py -m pip install -r requirements.txt")
    ed25519 = _ed


def http_json(method: str, url: str, body: Optional[dict] = None, headers: Optional[dict] = None,
              timeout: int = 20, proxy: str | None = None) -> tuple[int, Any]:
    data = None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    opener = opener_for(proxy)
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read()
            payload = json.loads(raw.decode() or "null") if raw else {}
            return r.status, payload
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        try:
            payload = json.loads(raw.decode() or "{}")
        except Exception:
            payload = {"error": redact(raw.decode("utf-8", "replace")[:300] or e.reason)}
        return e.code, payload
    except Exception as e:
        raise RuntimeError(redact(str(e))) from None


def arcus_priv(keys: Keys):
    _load_crypto()
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(keys.arcus_signing_key))
    api_key = priv.public_key().public_bytes_raw().hex()
    return priv, api_key


def arcus_get(path: str, params: dict, api_key: Optional[str] = None, proxy: str | None = None) -> tuple[int, Any]:
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{ARCUS_BASE}{path}?{q}"
    headers = {"X-API-Key": api_key} if api_key else None
    return http_json("GET", url, headers=headers, proxy=proxy)


def _explain(status: int, body: Any) -> str:
    err = ""
    if isinstance(body, dict):
        err = str(body.get("error") or body.get("message") or "")
    err = redact(err)
    if status == 401:
        return f"Arcus 认证失败（401）{('：' + err) if err else '。'}请确认 ARCUS_SIGNING_KEY 是只显示一次的 Signing Key，且电脑时间准确。"
    if status == 403:
        return f"Arcus 拒绝访问（403）{('：' + err) if err else '。'}地址或子账户与 API Key 授权不一致。"
    if status == 429:
        return f"Arcus 触发限速（429）{('：' + err) if err else ''}，请稍后再试。"
    return f"Arcus 请求失败（HTTP {status}）{('：' + err) if err else '。'}"


def arcus_markets(proxy: str | None = None) -> list[dict]:
    st, body = arcus_get("/v1/markets", {}, proxy=proxy)
    if st != 200 or not isinstance(body, dict):
        raise TradeError(_explain(st, body) if st != 200 else "Arcus 市场列表格式异常。")
    return body.get("markets") or []


def norm_ticker(text: str) -> str:
    return (text or "").strip().upper().replace("-USD", "").replace("-USDC", "").replace("-USDT", "")


def find_market(markets: list[dict], coin: str) -> dict:
    want = norm_ticker(coin.split(":", 1)[-1] if ":" in coin else coin)
    online = [m for m in markets if m.get("status") == "ONLINE"]
    hits = []
    for m in online:
        base = norm_ticker(m.get("baseAsset") or "")
        disp = norm_ticker(m.get("marketDisplayName") or "")
        if want in {base, disp}:
            hits.append(m)
    if not hits:
        raise TradeError(f"Arcus 上没有名为 {coin} 的在线永续。")
    if len(hits) > 1:
        names = ", ".join(f"{h.get('marketDisplayName')}(id={h.get('marketId')})" for h in hits)
        raise TradeError(f"Arcus 上 {coin} 匹配到多个市场：{names}。请写更精确的名称，例如 BTC-USD。")
    return hits[0]


def listed_bases(markets: list[dict]) -> set[str]:
    out = set()
    for m in markets:
        if m.get("status") == "ONLINE":
            out.add(norm_ticker(m.get("baseAsset") or ""))
            out.add(norm_ticker(m.get("marketDisplayName") or ""))
    out.discard("")
    return out


def arcus_status(keys: Keys) -> dict:
    try:
        _priv, api_key = arcus_priv(keys)
    except Exception:
        raise TradeError("无法从 ARCUS_SIGNING_KEY 推导公钥：请确认该字段是 64 位十六进制的 Ed25519 私钥。")
    st, body = arcus_get("/v1/apiKeys", {"address": keys.arcus_wallet_address}, api_key, keys.proxy)
    if st != 200:
        raise TradeError(_explain(st, body))
    entries = (body or {}).get("apiKeys") or []
    matched = next((e for e in entries if str(e.get("apiKey") or "").lower() == api_key.lower()), None)
    if not matched:
        raise TradeError("Arcus 上未找到与当前 ARCUS_SIGNING_KEY 对应的 API Key。请到 https://app.arcus.xyz/api-keys 核对。")
    status = (matched.get("status") or "").upper()
    if status and status != "ACTIVE":
        raise TradeError(f"这把 Arcus API Key 当前状态为 {status}，不能用于交易。")
    all_sub = bool(matched.get("allSubaccounts"))
    key_index = matched.get("accountIndex")
    if not all_sub and key_index is not None and int(key_index) != keys.arcus_account_index:
        raise TradeError(
            f"这把 API Key 授权的子账户是 {key_index}，但 API.txt 里 ARCUS_ACCOUNT_INDEX={keys.arcus_account_index}。"
        )
    acc_st, acc = arcus_get(
        "/v1/account",
        {"address": keys.arcus_wallet_address, "accountIndex": keys.arcus_account_index},
        api_key,
        keys.proxy,
    )
    empty = acc_st == 404
    if acc_st not in (200, 404):
        raise TradeError(_explain(acc_st, acc))
    if not isinstance(acc, dict):
        acc = {}
    positions = []
    pos_map = acc.get("positions") or {}
    if isinstance(pos_map, dict):
        positions = list(pos_map.values())
    elif isinstance(pos_map, list):
        positions = pos_map
    return {
        "ok": True,
        "api_name": matched.get("apiWalletName") or "",
        "empty": empty,
        "account": acc,
        "positions": [p for p in positions if p],
    }


def snap_decimal(value, unit, rounding=ROUND_HALF_UP) -> Decimal:
    step = Decimal(str(unit))
    n = Decimal(str(value)) / step
    n = n.to_integral_value(rounding=rounding)
    if n <= 0:
        raise TradeError(f"数量或价格对齐步长 {unit} 后变为 0，请加大数量或价格。")
    return n * step


def to_engine_int(value, unit) -> int:
    n = Decimal(str(value)) / Decimal(str(unit))
    if n != n.to_integral_value():
        raise TradeError(f"{value} 不是 {unit} 的整数倍，无法按官方规则签名。")
    return int(n)


def dec_str(value: Decimal) -> str:
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def build_order(keys: Keys, market: dict, side: str, size, price: Optional[str] = None, reduce_only: bool = False) -> dict:
    tick, step = market["tickSize"], market["stepSize"]
    mid = market.get("markPrice") or market.get("oraclePrice") or market.get("lastTradePrice")
    if mid is None:
        raise TradeError(f"Arcus 市场 {market.get('marketDisplayName')} 没有标记价，无法下单。")
    mid_d = Decimal(str(mid))
    qty = snap_decimal(size, step)
    is_buy = side == "BUY"
    if price:
        px = snap_decimal(price, tick)
        order_type, tif = "LIMIT", "GTT"
    else:
        raw = mid_d * (1 + SLIPPAGE) if is_buy else mid_d * (1 - SLIPPAGE)
        px = snap_decimal(raw, tick, rounding=ROUND_CEILING if is_buy else ROUND_FLOOR)
        order_type, tif = "MARKET", "IOC"
    band = mid_d * Decimal("0.10")
    if abs(px - mid_d) > band + Decimal(str(tick)):
        raise TradeError("保护限价超出标记价 10% 的允许范围，请改用 --price 指定限价。")
    ts = time.time_ns()
    good_til = int(time.time() * 1_000_000) + GTT_MONTHS_US
    addr = keys.arcus_wallet_address.lower()
    rflag = 1 if reduce_only else 0
    payload = (
        f'{{"ad":"{addr}","ai":{keys.arcus_account_index},"ct":{ts},"g":{good_til * 1000},'
        f'"m":{int(market["marketId"])},"op":{OP_PLACE},"p":{to_engine_int(px, tick)},'
        f'"q":{to_engine_int(qty, step)},"r":{rflag},"s":{SIDE[side]},"t":{TIF[tif]},"v":1}}'
    )
    body = {
        "address": keys.arcus_wallet_address,
        "accountIndex": keys.arcus_account_index,
        "marketId": int(market["marketId"]),
        "orderSide": side,
        "orderType": order_type,
        "quantity": dec_str(qty),
        "price": dec_str(px),
        "timeInForce": tif,
        "goodTilTime": str(good_til),
        "timestamp": ts,
    }
    if reduce_only:
        body["reduceOnly"] = True
    return {
        "venue": "Arcus",
        "market": market.get("marketDisplayName"),
        "market_id": int(market["marketId"]),
        "side": side,
        "type": order_type,
        "tif": tif,
        "size": dec_str(qty),
        "price": dec_str(px),
        "mark": str(mid),
        "account": short_addr(keys.arcus_wallet_address),
        "account_index": keys.arcus_account_index,
        "reduce_only": bool(reduce_only),
        "_payload": payload,
        "_body": body,
        "_ts": ts,
    }


def place_order(keys: Keys, planned: dict) -> Any:
    priv, api_key = arcus_priv(keys)
    sig = priv.sign(planned["_payload"].encode()).hex()
    url = f"{ARCUS_BASE}/v1/placeOrder?{urllib.parse.urlencode({'address': keys.arcus_wallet_address})}"
    st, body = http_json(
        "POST",
        url,
        planned["_body"],
        {"X-API-Key": api_key, "X-Timestamp": str(planned["_ts"]), "X-Signature": sig},
        proxy=keys.proxy,
    )
    if st not in (200, 202):
        raise TradeError(_explain(st, body))
    return body


def tpsl_close_side(position_side: str) -> str:
    return "SELL" if str(position_side).upper() in {"LONG", "BUY"} else "BUY"


def tpsl_trigger_ok(tpsl_type: str, close_side: str, stop_price, mark) -> bool:
    """触发价必须在标记价正确一侧，否则交易所会拒单。"""
    if mark is None or stop_price is None:
        return False
    sp, mk = float(stop_price), float(mark)
    typ = str(tpsl_type).upper()
    side = str(close_side).upper()
    if typ == "STOP_LOSS":
        return sp < mk if side == "SELL" else sp > mk
    if typ == "TAKE_PROFIT":
        return sp > mk if side == "SELL" else sp < mk
    return False


def tpsl_order_price(order: dict):
    if not isinstance(order, dict):
        return None
    return order.get("triggerPrice") or order.get("stopPrice") or order.get("price")


def same_tick(a, b, tick) -> bool:
    if a is None or b is None or tick is None:
        return False
    try:
        return snap_decimal(a, tick) == snap_decimal(b, tick)
    except Exception:
        return abs(float(a) - float(b)) <= float(tick)


def build_tpsl_order(
    keys: Keys,
    market: dict,
    close_side: str,
    size,
    tpsl_type: str,
    stop_price,
    *,
    is_position_tpsl: bool = True,
) -> dict:
    """仓位级减仓止盈/止损。签名 op=4（placeUntriggered）。保护价相对触发价滑点 5%，须在 10% 以内。"""
    typ = str(tpsl_type).upper()
    if typ not in {"STOP_LOSS", "TAKE_PROFIT"}:
        raise TradeError("止盈止损类型只能是 STOP_LOSS 或 TAKE_PROFIT。")
    if close_side not in SIDE:
        raise TradeError(f"平仓方向只能是 {', '.join(SIDE)}。")
    tick, step = market["tickSize"], market["stepSize"]
    stop = snap_decimal(stop_price, tick)
    is_buy = close_side == "BUY"
    raw = stop * (1 + SLIPPAGE) if is_buy else stop * (1 - SLIPPAGE)
    px = snap_decimal(raw, tick, rounding=ROUND_CEILING if is_buy else ROUND_FLOOR)
    band = stop * Decimal("0.10")
    if abs(px - stop) > band:
        raise TradeError("止盈止损保护价超出触发价 10%。")
    qty = snap_decimal(size, step)
    if qty <= 0:
        raise TradeError("止盈止损数量必须大于 0。")
    ts = time.time_ns()
    good_til = int(time.time() * 1_000_000) + GTT_MONTHS_US
    addr = keys.arcus_wallet_address.lower()
    payload = (
        f'{{"ad":"{addr}","ai":{keys.arcus_account_index},"ct":{ts},"g":{good_til * 1000},'
        f'"m":{int(market["marketId"])},"op":{OP_PLACE_UNTRIGGERED},"p":{to_engine_int(px, tick)},'
        f'"q":{to_engine_int(qty, step)},"r":1,"s":{SIDE[close_side]},"t":{TIF["IOC"]},"v":1}}'
    )
    body = {
        "address": keys.arcus_wallet_address,
        "accountIndex": keys.arcus_account_index,
        "marketId": int(market["marketId"]),
        "orderSide": close_side,
        "orderType": "MARKET",
        "quantity": dec_str(qty),
        "price": dec_str(px),
        "timeInForce": "IOC",
        "goodTilTime": str(good_til),
        "timestamp": ts,
        "reduceOnly": True,
        "tpslType": typ,
        "stopPrice": dec_str(stop),
        "isPositionTPSL": bool(is_position_tpsl),
    }
    return {
        "market": market["marketDisplayName"],
        "side": close_side,
        "qty": dec_str(qty),
        "price": dec_str(px),
        "stop_price": dec_str(stop),
        "tpsl_type": typ,
        "type": "MARKET",
        "tif": "IOC",
        "reduce_only": True,
        "is_position_tpsl": bool(is_position_tpsl),
        "_payload": payload,
        "_body": body,
        "_ts": ts,
    }


def cancel_order(keys: Keys, market: dict, order_id: str) -> Any:
    if not order_id:
        raise TradeError("取消订单需要 orderId。")
    priv, api_key = arcus_priv(keys)
    ts = time.time_ns()
    addr = keys.arcus_wallet_address.lower()
    mid = int(market["marketId"])
    payload = (
        f'{{"ad":"{addr}","ai":{keys.arcus_account_index},"ct":{ts},"id":"{order_id}","m":{mid},"op":{OP_CANCEL},"v":1}}'
    )
    body = {
        "kind": "orderId",
        "address": keys.arcus_wallet_address,
        "accountIndex": keys.arcus_account_index,
        "marketId": mid,
        "orderId": str(order_id),
        "timestamp": ts,
    }
    sig = priv.sign(payload.encode()).hex()
    url = f"{ARCUS_BASE}/v1/cancelOrder?{urllib.parse.urlencode({'address': keys.arcus_wallet_address})}"
    st, raw = http_json(
        "POST",
        url,
        body,
        {"X-API-Key": api_key, "X-Timestamp": str(ts), "X-Signature": sig},
        proxy=keys.proxy,
    )
    if st not in (200, 201, 202):
        raise TradeError(_explain(st, raw))
    return raw


def list_open_orders(
    keys: Keys,
    market: dict | None = None,
    status: str = "UNTRIGGERED",
) -> list[dict]:
    _, api_key = arcus_priv(keys)
    params: dict[str, Any] = {
        "address": keys.arcus_wallet_address,
        "accountIndex": keys.arcus_account_index,
        "limit": 100,
    }
    if status:
        params["status"] = status
    if market:
        params["market"] = market.get("marketDisplayName") or str(market.get("marketId"))
    st, body = arcus_get("/v1/openOrders", params, api_key, keys.proxy)
    if st != 200:
        raise TradeError(_explain(st, body))
    if not isinstance(body, dict):
        raise TradeError("Arcus 开放订单格式异常。")
    orders = body.get("orders")
    return orders if isinstance(orders, list) else []


def tpsl_order_id(order: dict) -> str:
    if not isinstance(order, dict):
        return ""
    return str(order.get("orderId") or order.get("id") or "")



def fnum(x, digits=6) -> str:
    if x is None or x == "":
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    if abs(v) >= 1:
        return f"{v:.4f}"
    return f"{v:.{digits}f}"


def print_status(keys: Keys, data: dict) -> None:
    print("=== Arcus ===")
    print(f"认证：成功  钱包：{short_addr(keys.arcus_wallet_address)}  子账户：{keys.arcus_account_index}")
    if data.get("api_name"):
        print(f"钥匙名称：{data['api_name']}")
    if data.get("empty"):
        print("账户尚无活动。权益与保证金按 0 显示。")
        return
    acc = data.get("account") or {}
    print(f"权益 equity：{fnum(acc.get('equity'))}")
    print(f"可用保证金 freeCollateral：{fnum(acc.get('freeCollateral'))}")
    pos = data.get("positions") or []
    if not pos:
        print("持仓：无")
        return
    print(f"持仓：{len(pos)} 笔")
    for p in pos:
        print(
            f"  {p.get('marketDisplayName') or p.get('marketId')} "
            f"{p.get('side')} 数量 {fnum(p.get('size'))} "
            f"均价 {fnum(p.get('averageEntryPrice'))} "
            f"未实现盈亏 {fnum(p.get('unrealizedPnl'))}"
        )



def market_mark(market: dict):
    return market.get("markPrice") or market.get("oraclePrice") or market.get("lastTradePrice")


def position_name(pos: dict) -> str:
    return str(pos.get("marketDisplayName") or pos.get("market") or pos.get("marketId") or "")


def position_side(pos: dict) -> str:
    raw = str(pos.get("side") or pos.get("positionSide") or "").upper()
    if raw in {"LONG", "BUY"}:
        return "LONG"
    if raw in {"SHORT", "SELL"}:
        return "SHORT"
    try:
        size = float(pos.get("size") or pos.get("netSize") or 0)
    except (TypeError, ValueError):
        size = 0
    return "LONG" if size > 0 else "SHORT" if size < 0 else ""


def position_size(pos: dict):
    for k in ("size", "netSize", "quantity", "absSize"):
        if pos.get(k) not in (None, ""):
            try:
                return abs(__import__("decimal").Decimal(str(pos[k])))
            except Exception:
                continue
    return __import__("decimal").Decimal("0")


def position_entry(pos: dict):
    for k in ("averageEntryPrice", "entryPrice", "avgEntryPrice"):
        if pos.get(k) not in (None, ""):
            try:
                return float(pos[k])
            except (TypeError, ValueError):
                continue
    return None


def position_mark(pos: dict):
    """优先用账户仓位上的标记价；没有的话用均价和未实现盈亏反推，避免用过期的市场快照误平。"""
    for k in ("markPrice", "mark", "oraclePrice", "lastPrice", "indexPrice"):
        if pos.get(k) not in (None, ""):
            try:
                return float(pos[k])
            except (TypeError, ValueError):
                continue
    entry = position_entry(pos)
    side = position_side(pos)
    try:
        size = float(position_size(pos))
        upnl = float(pos.get("unrealizedPnl") if pos.get("unrealizedPnl") not in (None, "") else (pos.get("unrealized") or 0))
    except (TypeError, ValueError):
        return None
    if entry is None or size <= 0:
        return None
    if side == "LONG":
        return entry + upnl / size
    if side == "SHORT":
        return entry - upnl / size
    return None



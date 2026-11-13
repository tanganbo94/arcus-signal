#!/usr/bin/env python3
"""本地自动盯盘：扫描 Arcus 全部在线品种（含美股），按威科夫结构开仓，并用结构止盈止损管理仓位。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from arcus import (
    TradeError,
    arcus_markets,
    arcus_status,
    build_order,
    build_tpsl_order,
    cancel_order,
    find_market,
    fnum,
    list_open_orders,
    market_mark,
    place_order,
    position_entry,
    position_mark,
    position_name,
    position_side,
    position_size,
    same_tick,
    tpsl_close_side,
    tpsl_order_id,
    tpsl_order_price,
    tpsl_trigger_ok,
)
from keys import ConfigError, apply_proxy, load_keys, redact, short_addr
from market import classify_price_vs_oi, crypto_skip_reason, fetch_deriv, fetch_many_arcus_candles, fmt_market_cap, get_crypto_market_caps, lookup_market_cap
from news import fetch_news, filter_auto_by_news, news_blocks_entry
from wyckoff import analyze

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "watch_state.json"
LOG_FILE = ROOT / "logs" / "watch.log"
TZ = ZoneInfo("Asia/Shanghai")
NY = ZoneInfo("America/New_York")
MIN_RR = 1.5
MIN_RR_FAILED_SPRING = 2.0
TARGET_R = 2.0
MIN_STOP_PCT = 0.005  # 标记价到止损不足 0.5%，视为噪声，不开仓
SCRATCH_HOURS = 24
SCRATCH_R = 0.5
SCAN_MINUTES_US_RTH = 10


# 模块里可能没有 close_position 这个名字的封装，下面用 build_order(..., reduce_only=True)


def _win_utf8() -> None:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def now_sh() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, *, console: bool = True) -> None:
    line = f"{now_sh()}  {msg}"
    if console:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("utf-8", "replace").decode("utf-8", "replace"), flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def format_status_line(st: dict) -> str:
    acc = st.get("account") or {}
    pos = st.get("positions") or []
    if st.get("empty") and not pos:
        return "账户权益 0，持仓无"
    parts = [f"权益 {fnum(acc.get('equity'))}", f"可用保证金 {fnum(acc.get('freeCollateral'))}"]
    if not pos:
        parts.append("持仓无")
    else:
        bits = []
        for p in pos:
            name = position_name(p) or "—"
            side = "多" if position_side(p) == "LONG" else "空"
            bits.append(
                f"{name}{side} 均价 {fnum(position_entry(p))} 未实现 {fnum(p.get('unrealizedPnl'))}"
            )
        parts.append(f"持仓 {len(pos)} 笔：" + "；".join(bits))
    return "，".join(parts)


def position_key(st: dict) -> tuple:
    names = []
    for p in st.get("positions") or []:
        names.append((position_name(p), position_side(p)))
    names.sort()
    return tuple(names)


def load_state() -> dict:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    state["updated"] = now_sh()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def settings_from_api() -> dict:
    from keys import _parse_kv_file
    data = {}
    for name in ("API.txt", ".env"):
        p = ROOT / name
        if p.is_file():
            data = _parse_kv_file(p)
            break

    def num(key, default, typ=float):
        raw = str(data.get(key, "")).strip()
        if not raw:
            return default
        try:
            return typ(raw)
        except ValueError:
            return default

    return {
        "notional": num("NOTIONAL", 50.0),
        "max_positions": int(num("MAX_POSITIONS", 5, float)),
        "loop_seconds": int(num("LOOP_SECONDS", 60, float)),
        "scan_minutes": int(num("SCAN_MINUTES", 15, float)),
        "min_market_cap": num("MIN_MARKET_CAP", 100_000_000.0),
    }


def in_trading_hours(market: dict, now: datetime | None = None) -> bool:
    if market.get("isOutsideRth") is True:
        return False
    hours = market.get("regularTradingHours") or {}
    start = hours.get("startSecondsOfDay")
    end = hours.get("endSecondsOfDay")
    if start is None or end is None:
        return True
    tzname = hours.get("timezone") or "America/New_York"
    try:
        zone = ZoneInfo(tzname)
    except Exception:
        zone = NY
    now = now.astimezone(zone) if now is not None else datetime.now(zone)
    if now.weekday() >= 5:
        return False
    secs = now.hour * 3600 + now.minute * 60 + now.second
    start, end = int(start), int(end)
    if hours.get("isOvernight"):
        return secs >= start or secs <= end
    return start <= secs <= end



def us_equity_rth_open(markets: list[dict] | None = None, now: datetime | None = None) -> bool:
    """是否处于美股常规开盘时段（优先用 Arcus 市场的交易时段，周末不开）。"""
    markets = markets or []
    pick = next((m for m in markets if m.get("marketDisplayName") in {"SPY-USD", "QQQ-USD"}), None)
    if pick is None:
        pick = next(
            (
                m
                for m in markets
                if (m.get("category") or "").upper() in {"EQUITIES", "INDICES"}
                and (m.get("regularTradingHours") or {}).get("startSecondsOfDay") is not None
            ),
            None,
        )
    if pick is not None:
        if pick.get("isOutsideRth") is True:
            return False
        hours = pick.get("regularTradingHours") or {}
        tzname = hours.get("timezone") or "America/New_York"
        try:
            zone = ZoneInfo(tzname)
        except Exception:
            zone = NY
        current = now.astimezone(zone) if now is not None else datetime.now(zone)
        if current.weekday() >= 5:
            return False
        if hours.get("startSecondsOfDay") is not None:
            return in_trading_hours(pick, now=current)
    current = now.astimezone(NY) if now is not None else datetime.now(NY)
    if current.weekday() >= 5:
        return False
    secs = current.hour * 3600 + current.minute * 60 + current.second
    return (9 * 3600 + 30 * 60) <= secs <= 16 * 3600


def current_scan_minutes(cfg: dict, markets: list[dict] | None = None, now: datetime | None = None) -> int:
    off = int(cfg.get("scan_minutes") or 15)
    if us_equity_rth_open(markets, now=now):
        return SCAN_MINUTES_US_RTH
    return off


def bench_rets(d1: list[dict]) -> dict:
    def ret(n):
        if len(d1) < n + 1 or d1[-(n + 1)]["c"] == 0:
            return None
        return (d1[-1]["c"] / d1[-(n + 1)]["c"] - 1) * 100
    return {"r1": ret(1), "r3": ret(3), "r7": ret(7)}


def r_multiple(entry, stop, target, side: str) -> float | None:
    try:
        entry, stop, target = float(entry), float(stop), float(target)
    except (TypeError, ValueError):
        return None
    risk = (entry - stop) if side == "long" else (stop - entry)
    reward = (target - entry) if side == "long" else (entry - target)
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk



def stop_breached(side: str, mark, stop) -> bool:
    try:
        mark, stop = float(mark), float(stop)
    except (TypeError, ValueError):
        return False
    if side == "long":
        return mark <= stop
    return mark >= stop


def live_mark_blocks_entry(
    side: str,
    mark,
    stop,
    target,
    min_rr: float,
    confirm=None,
    min_stop_pct: float = MIN_STOP_PCT,
) -> str | None:
    """开仓前用最新标记价再核对一次。返回跳过原因，或 None 表示可以开。"""
    if mark is None:
        return "没有最新标记价"
    if stop_breached(side, mark, stop):
        return f"最新标记价 {fnum(mark)} 已在止损 {fnum(stop)} 错侧或已触发"
    if confirm is not None:
        try:
            c, m = float(confirm), float(mark)
        except (TypeError, ValueError):
            c = None
        else:
            if (side == "long" and m <= c) or (side == "short" and m >= c):
                return f"最新标记价 {fnum(mark)} 已失守确认位 {fnum(c)}"
    if target is not None:
        try:
            t, m = float(target), float(mark)
        except (TypeError, ValueError):
            t = None
        if t is not None and ((side == "long" and m >= t) or (side == "short" and m <= t)):
            return f"最新标记价 {fnum(mark)} 已到或超过目标 {fnum(target)}"
    rr = r_multiple(mark, stop, target, side)
    if rr is None or rr < min_rr:
        shown = fnum(rr) if rr is not None else "无法计算"
        return f"按最新标记价 {fnum(mark)} 计算盈亏比不足（{shown}，需要至少 {fnum(min_rr)}）"
    try:
        m, s = float(mark), float(stop)
        risk = (m - s) if side == "long" else (s - m)
        if m > 0 and risk / m < min_stop_pct:
            return (
                f"最新标记价到止损仅 {risk / m * 100:.2f}%，"
                f"低于最小风险 {min_stop_pct * 100:.1f}%，按噪声处理"
            )
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return None


def filter_auto_by_live_mark(
    candidates: list[dict],
    markets_by_name: dict | None,
) -> tuple[list[dict], list[tuple[dict, str]]]:
    """用最新标记价重算盈亏比并核对确认位。

    返回（仍可自动开仓的信号, [(信号, 原因)...]）。没有行情快照时不做这次过滤，沿用收盘价结果。
    实时盈亏比不够或失守确认位的，不列入自动开仓，也不在这里把结构标成已处理。
    """
    if not markets_by_name:
        return list(candidates), []
    kept: list[dict] = []
    blocked: list[tuple[dict, str]] = []
    for s in candidates:
        info = s.get("info") or {}
        name = s.get("name")
        m = markets_by_name.get(name) or s.get("market")
        if name in markets_by_name:
            s["market"] = m
        mid = market_mark(m) if m else None
        reason = live_mark_blocks_entry(
            info.get("side"),
            mid,
            s.get("stop"),
            s.get("target"),
            min_auto_rr(info.get("event")),
            confirm=info.get("stop"),
        )
        if reason:
            blocked.append((s, reason))
            continue
        live_rr = r_multiple(mid, s.get("stop"), s.get("target"), info.get("side"))
        if live_rr is not None:
            s["rr_closed"] = s.get("rr")
            s["rr"] = live_rr
        kept.append(s)
    return kept, blocked


def structure_already_gone(side: str, mark, stop, target) -> bool:
    """价格已破止损或已到目标，这个结构不再开仓。"""
    if mark is None or stop is None:
        return False
    if stop_breached(side, mark, stop):
        return True
    if target is None:
        return False
    try:
        t, m = float(target), float(mark)
    except (TypeError, ValueError):
        return False
    return (side == "long" and m >= t) or (side == "short" and m <= t)


def min_auto_rr(event: str | None) -> float:
    return MIN_RR_FAILED_SPRING if event == "弹簧失败" else MIN_RR


def parse_opened_at(ts: str | None) -> datetime | None:
    if not ts:
        return None
    raw = str(ts).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def cap_target_to_2r(entry, stop, target, side: str):
    """目标取 2 倍风险和区间对侧里更近的那个。用开仓时的结构止损算风险，不受跟踪止损影响。"""
    if entry is None or stop is None:
        return None if target is None else float(target)
    entry, stop = float(entry), float(stop)
    risk = (entry - stop) if side == "long" else (stop - entry)
    if risk <= 0:
        return None if target is None else float(target)
    two_r = entry + TARGET_R * risk if side == "long" else entry - TARGET_R * risk
    if target is None:
        return two_r
    target = float(target)
    return min(target, two_r) if side == "long" else max(target, two_r)


def should_scratch(plan: dict, mark: float, now: datetime | None = None) -> bool:
    """开仓超过 24 小时仍未走到 0.5 倍风险，撤出。继承来的旧仓不按这条处理。"""
    if plan.get("inherited"):
        return False
    opened = parse_opened_at(plan.get("opened_at"))
    if opened is None:
        return False
    now = now or datetime.now(TZ)
    if now - opened < timedelta(hours=SCRATCH_HOURS):
        return False
    entry = plan.get("entry")
    init_stop = plan.get("init_stop") if plan.get("init_stop") is not None else plan.get("stop")
    if entry is None or init_stop is None:
        return False
    entry, init_stop, mark = float(entry), float(init_stop), float(mark)
    risk = abs(entry - init_stop)
    if risk <= 0:
        return False
    moved = (mark - entry) if plan.get("side") == "long" else (entry - mark)
    return moved < SCRATCH_R * risk


def structure_levels(info: dict, side: str, last: float) -> tuple[float | None, float | None]:
    stop = info.get("stop")
    if side == "long":
        target = info.get("high20")
        if stop is not None:
            stop = float(stop) * 0.998
        if target is not None and last >= float(target):
            # 已到区间上沿，用区间高度上推一倍作为目标
            low20, high20 = info.get("low20"), info.get("high20")
            if low20 and high20:
                target = float(high20) + (float(high20) - float(low20))
    else:
        target = info.get("low20")
        if stop is not None:
            stop = float(stop) * 1.002
        if target is not None and last <= float(target):
            low20, high20 = info.get("low20"), info.get("high20")
            if low20 and high20:
                target = float(low20) - (float(high20) - float(low20))
    if stop is not None:
        target = cap_target_to_2r(last, stop, target, side)
    return (None if stop is None else float(stop), None if target is None else float(target))


def trail_stop(plan: dict, mark: float, h4: list[dict]) -> float:
    stop = float(plan["stop"])
    entry = float(plan["entry"])
    side = plan["side"]
    target = plan.get("target")
    risk = abs(entry - stop) if stop else 0
    if risk <= 0:
        return stop
    if side == "long":
        if mark >= entry + risk and stop < entry:
            stop = entry
        swing = None
        if len(h4) >= 5:
            swing = min(x["l"] for x in h4[-5:-1])
        if mark >= entry + risk and swing and entry <= swing < mark and swing > stop:
            stop = swing
        if target and mark >= entry + 0.6 * max(float(target) - entry, risk) and swing and swing > stop and swing < mark:
            stop = max(stop, swing)
    else:
        if mark <= entry - risk and (stop > entry or stop == float(plan["stop"])):
            if stop > entry:
                stop = entry
        swing = None
        if len(h4) >= 5:
            swing = max(x["h"] for x in h4[-5:-1])
        if mark <= entry - risk and swing and mark < swing <= entry and swing < stop:
            stop = swing
        if target and mark <= entry - 0.6 * max(entry - float(target), risk) and swing and swing < stop and swing > mark:
            stop = min(stop, swing)
    return stop


def flatten(keys, market: dict, pos: dict, live: bool, reason: str) -> bool:
    side = position_side(pos)
    size = position_size(pos)
    if size <= 0 or not side:
        return False
    close_side = "SELL" if side == "LONG" else "BUY"
    name = position_name(pos)
    try:
        planned = build_order(keys, market, close_side, size, reduce_only=True)
    except TradeError as e:
        log(f"平仓下单构建失败 {name}：{redact(str(e))}")
        return False
    log(f"{'准备平仓' if not live else '开始平仓'} {name} {side} 数量 {planned['size']}  原因：{reason}")
    if not live:
        return False
    try:
        result = place_order(keys, planned)
    except Exception as e:
        log(f"平仓失败 {name}：{redact(str(e))}")
        return False
    status = ""
    if isinstance(result, dict):
        status = str(result.get("status") or "")
        filled = result.get("filledSize")
        log(f"平仓已受理 {name}  状态 {status or '—'}  成交 {filled or '—'}")
    else:
        log(f"平仓已受理 {name}")
    cancel_exchange_tpsl(keys, market, name)
    return True


def open_trade(keys, market: dict, side: str, notional: float, live: bool) -> dict | None:
    mid = market_mark(market)
    if mid is None:
        raise TradeError(f"{market.get('marketDisplayName')} 没有标记价。")
    mid_d = Decimal(str(mid))
    size = Decimal(str(notional)) / mid_d
    min_size = market.get("minOrderSize")
    if min_size and size < Decimal(str(min_size)):
        size = Decimal(str(min_size))
    min_notional = market.get("minOrderNotional")
    if min_notional and size * mid_d < Decimal(str(min_notional)):
        size = Decimal(str(min_notional)) / mid_d
    order_side = "BUY" if side == "long" else "SELL"
    planned = build_order(keys, market, order_side, size)
    name = planned["market"]
    log(
        f"{'预览开仓' if not live else '开始开仓'} {name} "
        f"{'做多' if side == 'long' else '做空'} 数量 {planned['size']} "
        f"保护价 {planned['price']}  标记 {planned['mark']}"
    )
    if not live:
        return {"dry_run": True, **{k: planned[k] for k in ("market", "side", "size", "price", "mark")}}
    result = place_order(keys, planned)
    filled = None
    avg = None
    status = ""
    if isinstance(result, dict):
        status = str(result.get("status") or "")
        filled = result.get("filledSize") or result.get("quantity")
        log(f"开仓已受理 {name}  状态 {status or '—'}  数量 {filled or planned['size']}")
        if str(status).upper() in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
            raise TradeError(f"{name} 订单未成交，状态 {status}。")
    return {
        "result_status": status,
        "size": str(filled or planned["size"]),
        "mark": planned["mark"],
        "price": planned["price"],
        "order": {k: result.get(k) for k in ("orderId", "status", "filledSize", "price") if isinstance(result, dict) and k in result},
    }

def _tpsl_label(typ: str) -> str:
    return "止损" if typ == "STOP_LOSS" else "止盈"


def cancel_exchange_tpsl(keys, market: dict, name: str) -> None:
    try:
        opens = list_open_orders(keys, market, status="UNTRIGGERED")
    except Exception as e:
        log(f"{name} 平仓后查询交易所挂单失败：{redact(str(e))}")
        return
    for o in opens:
        typ = str((o or {}).get("tpslType") or "").upper()
        if typ not in {"STOP_LOSS", "TAKE_PROFIT"}:
            continue
        oid = tpsl_order_id(o)
        if not oid:
            continue
        try:
            cancel_order(keys, market, oid)
            log(f"{name} 已撤销交易所残留{_tpsl_label(typ)}")
        except Exception as e:
            log(f"{name} 撤销交易所残留{_tpsl_label(typ)}失败：{redact(str(e))}")


def _place_one_tpsl(keys, market, name: str, close_side: str, size, typ: str, px) -> dict | None:
    planned = build_tpsl_order(keys, market, close_side, size, typ, px)
    result = place_order(keys, planned)
    oid = ""
    if isinstance(result, dict):
        oid = tpsl_order_id(result)
    log(
        f"  已在交易所挂上 {name} 仓位{_tpsl_label(typ)} 触发价 {planned['stop_price']}  保护价 {planned['price']}"
        + (f"  单号 {oid}" if oid else "")
    )
    return {"id": oid, "px": planned["stop_price"]}


def sync_exchange_tpsl(keys, market: dict, pos: dict, plan: dict, live: bool) -> None:
    if not live or not plan:
        return
    name = position_name(pos)
    side = position_side(pos)
    size = position_size(pos)
    if not name or not side or size <= 0:
        return
    close_side = tpsl_close_side(side)
    stop, target = plan.get("stop"), plan.get("target")
    mark = market_mark(market)
    tick = market.get("tickSize")
    wanted = []
    if stop is not None:
        wanted.append(("STOP_LOSS", stop))
    if target is not None:
        wanted.append(("TAKE_PROFIT", target))
    if not wanted:
        return
    rec = plan.setdefault("ex_tpsl", {})
    already = True
    for typ, px in wanted:
        item = rec.get(typ) or {}
        if not item.get("id") or not same_tick(item.get("px"), px, tick):
            already = False
            break
    if already:
        return
    try:
        opens = list_open_orders(keys, market, status="UNTRIGGERED")
    except Exception as e:
        log(f"{name} 查询交易所止盈止损失败：{redact(str(e))}")
        return
    by_type = {}
    for o in opens:
        typ = str((o or {}).get("tpslType") or "").upper()
        if typ in {"STOP_LOSS", "TAKE_PROFIT"} and typ not in by_type:
            by_type[typ] = o
    for typ, px in wanted:
        existing = by_type.get(typ)
        exist_px = tpsl_order_price(existing) if existing else None
        if existing and same_tick(exist_px, px, tick):
            rec[typ] = {"id": tpsl_order_id(existing), "px": str(exist_px)}
            continue
        if mark is not None and not tpsl_trigger_ok(typ, close_side, px, mark):
            log(f"  {name} 交易所{_tpsl_label(typ)}触发价 {fnum(px)} 已在标记价错侧，暂不挂，由本地盯盘处理。")
            continue
        if existing:
            oid = tpsl_order_id(existing)
            try:
                cancel_order(keys, market, oid)
                log(f"  {name} 已撤销旧的交易所{_tpsl_label(typ)}（触发价 {fnum(exist_px)}）")
                rec.pop(typ, None)
                time.sleep(0.4)
            except Exception as e:
                log(f"  {name} 撤销旧交易所{_tpsl_label(typ)}失败：{redact(str(e))}")
                continue
        try:
            rec[typ] = _place_one_tpsl(keys, market, name, close_side, size, typ, px) or rec.get(typ)
        except Exception as e:
            msg = redact(str(e))
            log(f"  {name} 挂交易所{_tpsl_label(typ)}失败：{msg}")
            if "POSITION_TPSL_ALREADY_EXISTS" not in str(e).upper() and "already" not in str(e).lower():
                continue
            try:
                time.sleep(0.4)
                opens2 = list_open_orders(keys, market, status="UNTRIGGERED")
                for o in opens2:
                    if str((o or {}).get("tpslType") or "").upper() == typ:
                        cancel_order(keys, market, tpsl_order_id(o))
                        time.sleep(0.4)
                        rec[typ] = _place_one_tpsl(keys, market, name, close_side, size, typ, px)
                        break
            except Exception as e2:
                log(f"  {name} 重挂交易所{_tpsl_label(typ)}失败：{redact(str(e2))}")




_missing_hits: dict[str, int] = {}


def reconcile_missing_positions(keys, markets_by_name: dict, st: dict, state: dict, live: bool) -> None:
    """账户里已经没有的仓，连续两轮确认后再从本地状态清掉，避免接口闪断误删。"""
    plans = state.setdefault("positions", {})
    held = {position_name(p) for p in (st.get("positions") or []) if position_name(p)}
    for name in list(_missing_hits):
        if name in held or name not in plans:
            _missing_hits.pop(name, None)
    for name in list(plans):
        if name in held:
            continue
        plan = plans.get(name) or {}
        side = plan.get("side") or "—"
        entry = plan.get("entry")
        event = plan.get("event") or "—"
        opened = plan.get("opened_at") or "—"
        hits = _missing_hits.get(name, 0) + 1
        _missing_hits[name] = hits
        if hits == 1:
            log(
                f"账户本轮未见 {name} {side}（本地开仓 {opened}，事件 {event}，入场 {fnum(entry)}），"
                "先记一笔，下一轮仍没有再从本地状态移除。"
            )
            continue
        log(
            f"账户已无 {name} {side}（本地开仓 {opened}，事件 {event}，入场 {fnum(entry)}），"
            "按盘外平仓或交易所止盈止损成交处理：已从本地状态移除。"
        )
        market = markets_by_name.get(name)
        if live and market:
            cancel_exchange_tpsl(keys, market, name)
        plans.pop(name, None)
        _missing_hits.pop(name, None)


def manage_open(keys, markets_by_name: dict, st: dict, state: dict, candles: dict, live: bool) -> None:
    plans = state.setdefault("positions", {})
    for pos in st.get("positions") or []:
        name = position_name(pos)
        if not name:
            continue
        market = markets_by_name.get(name)
        if not market:
            continue
        mark = position_mark(pos)
        if mark is None:
            mark = market_mark(market)
        if mark is None:
            continue
        mark_f = float(mark)
        side = position_side(pos)
        plan = plans.get(name)
        if not plan:
            # 已有仓位：用当前结构挂上止盈止损，不会因为旧仓立刻乱平
            kl = candles.get(name) or {}
            info = None
            if kl.get("d1") and kl.get("h4"):
                bench = bench_rets((candles.get("BTC-USD") or candles.get("SPY-USD") or {}).get("d1") or [])
                cat = (market.get("category") or "").upper()
                if cat in {"EQUITIES", "INDICES", "COMMODITIES"}:
                    bench = bench_rets((candles.get("SPY-USD") or {}).get("d1") or [])
                info = analyze(kl["d1"], kl["h4"], bench, None)
            last = info.get("last") if info else mark_f
            entry = position_entry(pos) or mark_f
            dir_ = "long" if side == "LONG" else "short"
            stop, target = (None, None)
            if info:
                stop, target = structure_levels(info, dir_, last)
            if dir_ == "long":
                if stop is None or stop >= mark_f:
                    stop = min(x["l"] for x in (kl.get("d1") or [{}])[-20:] if x.get("l")) if kl.get("d1") else mark_f * 0.94
                    if stop >= mark_f:
                        stop = mark_f * 0.94
                if target is None or target <= mark_f:
                    target = mark_f * 1.06
            else:
                if stop is None or stop <= mark_f:
                    highs = [x["h"] for x in (kl.get("d1") or [{}])[-20:] if x.get("h")]
                    stop = max(highs) if highs and max(highs) > mark_f else mark_f * 1.06
                if target is None or target >= mark_f:
                    target = mark_f * 0.94
            plan = {
                "side": dir_,
                "entry": entry,
                "stop": stop,
                "init_stop": stop,
                "target": target,
                "event": "已有仓位-挂上结构",
                "opened_at": now_sh(),
                "inherited": True,
            }
            plans[name] = plan
            log(f"为已有仓位 {name} {side} 挂上结构止损 {fnum(stop)}、目标 {fnum(target)}")
            sync_exchange_tpsl(keys, market, pos, plan, live)
            continue

        dir_ = plan.get("side") or ("long" if side == "LONG" else "short")
        if plan.get("init_stop") is None and plan.get("stop") is not None:
            plan["init_stop"] = float(plan["stop"])
        capped = cap_target_to_2r(plan.get("entry"), plan.get("init_stop"), plan.get("target"), dir_)
        if capped is not None and plan.get("target") is not None and float(capped) != float(plan["target"]):
            log(f"{name} 目标从 {fnum(plan['target'])} 收到 {fnum(capped)}（2 倍风险与区间对侧取更近）")
            plan["target"] = capped
        h4 = (candles.get(name) or {}).get("h4") or []
        new_stop = trail_stop(plan, mark_f, h4)
        if new_stop != float(plan["stop"]):
            log(f"{name} 止损上移/下移至 {fnum(new_stop)}（跟踪结构）")
            plan["stop"] = new_stop
        stop, target = float(plan["stop"]), plan.get("target")
        if should_scratch(plan, mark_f):
            if flatten(keys, market, pos, live, f"开仓超过 {SCRATCH_HOURS} 小时仍未走到 {SCRATCH_R} 倍风险，撤出（标记价 {fnum(mark)}）"):
                plan["closed_at"] = now_sh()
                plan["close_reason"] = "scratch"
                plans.pop(name, None)
            continue
        if dir_ == "long":
            if mark_f <= stop:
                if flatten(keys, market, pos, live, f"结构止损（标记价 {fnum(mark)} <= {fnum(stop)}）"):
                    plan["closed_at"] = now_sh()
                    plan["close_reason"] = "stop"
                    plans.pop(name, None)
                continue
            if target and mark_f >= float(target):
                if flatten(keys, market, pos, live, f"结构止盈（标记价 {fnum(mark)} >= 目标 {fnum(target)}）"):
                    plan["closed_at"] = now_sh()
                    plans.pop(name, None)
                continue
        else:
            if mark_f >= stop:
                if flatten(keys, market, pos, live, f"结构止损（标记价 {fnum(mark)} >= {fnum(stop)}）"):
                    plans.pop(name, None)
                continue
            if target and mark_f <= float(target):
                if flatten(keys, market, pos, live, f"结构止盈（标记价 {fnum(mark)} <= 目标 {fnum(target)}）"):
                    plans.pop(name, None)
                continue
        sync_exchange_tpsl(keys, market, pos, plan, live)


def scan_and_enter(keys, markets: list[dict], st: dict, state: dict, cfg: dict, live: bool) -> dict:
    online = [m for m in markets if m.get("status") == "ONLINE"]
    names = [m.get("marketDisplayName") for m in online if m.get("marketDisplayName")]
    log(f"开始扫描 Arcus 全部在线品种，共 {len(names)} 个（加密、美股、指数、商品）。", console=False)
    prev = set(state.get("universe") or [])
    now_set = set(names)
    added = sorted(now_set - prev)
    removed = sorted(prev - now_set)
    if prev and added:
        log("发现新上品种，已自动纳入扫描：" + "、".join(added))
    elif not prev and names:
        log("已记录当前品种名单，之后若 Arcus 上新会自动跟上。")
    if prev and removed:
        log("以下品种已不在线或已下架，本轮不再作为新开仓标的：" + "、".join(removed))
    state["universe"] = names
    candles = fetch_many_arcus_candles(names, keys.proxy)
    log(f"已取得 K 线 {len(candles)} / {len(names)} 个品种。", console=False)
    btc = bench_rets((candles.get("BTC-USD") or {}).get("d1") or [])
    spy = bench_rets((candles.get("SPY-USD") or {}).get("d1") or [])

    held = {position_name(p) for p in (st.get("positions") or [])}
    seen = state.setdefault("signals", {})
    plans = state.setdefault("positions", {})
    open_count = len(st.get("positions") or [])
    acc = st.get("account") or {}
    try:
        free = float(acc.get("freeCollateral") or 0)
    except (TypeError, ValueError):
        free = 0

    signals = []
    for m in online:
        name = m.get("marketDisplayName")
        kl = candles.get(name)
        if not kl or not kl.get("d1") or not kl.get("h4"):
            continue
        cat = (m.get("category") or "CRYPTO").upper()
        bench = spy if cat in {"EQUITIES", "INDICES", "COMMODITIES"} else btc
        deriv = {"available": False}
        if cat == "CRYPTO":
            pair = f"{(m.get('baseAsset') or '').upper()}USDT"
            try:
                deriv = fetch_deriv(pair, keys.proxy)
            except Exception:
                deriv = {"available": False}
            if deriv.get("available") and len(kl["d1"]) >= 2:
                px = (kl["d1"][-1]["c"] / kl["d1"][-2]["c"] - 1) * 100
                deriv["price_vs_oi"] = classify_price_vs_oi(px, deriv.get("oi_chg_pct"))
        info = analyze(kl["d1"], kl["h4"], bench, deriv)
        if not info.get("actionable"):
            continue
        stop, target = structure_levels(info, info["side"], info["last"])
        rr = r_multiple(info["last"], stop, target, info["side"])
        if rr is None or rr < 1.0:
            continue
        key = f"{name}:{info.get('signal_key')}"
        signals.append({
            "name": name,
            "market": m,
            "info": info,
            "stop": stop,
            "target": target,
            "rr": rr,
            "key": key,
            "category": cat,
        })

    signals.sort(key=lambda x: (x["info"].get("score") or 0, x["rr"]), reverse=True)
    # 自动开仓：已确认结构，收盘价先过门槛，再用最新标记价重算盈亏比；普通至少 1.5，弹簧失败至少 2.0，每轮最多新开 1 笔
    closed_ok = [s for s in signals if s["info"].get("actionable") and s["rr"] >= min_auto_rr(s["info"].get("event"))]
    fresh_by_name: dict = {}
    markets_refreshed = False
    if closed_ok:
        try:
            fresh_list = arcus_markets(keys.proxy)
            fresh_by_name = {x.get("marketDisplayName"): x for x in fresh_list if x.get("marketDisplayName")}
            markets_refreshed = True
        except Exception as e:
            log("刷新标记价失败：" + redact(str(e)) + "。本轮先按收盘价筛选自动信号。", console=False)
    auto, live_blocked = filter_auto_by_live_mark(closed_ok, fresh_by_name)
    for s, reason in live_blocked:
        log(
            f"{s['name']} 收盘盈亏比 {s['rr']:.2f}，{reason}，不列入自动开仓。",
            console=False,
        )
    news_items = None
    if auto:
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as ex:
                news_items = ex.submit(fetch_news, keys.proxy).result(timeout=12)
        except Exception as e:
            log("拉取新闻失败或超时：" + redact(str(e)) + "。本轮新闻过滤跳过，开仓规则不变。", console=False)
            news_items = None
        if news_items:
            auto, news_blocked = filter_auto_by_news(auto, news_items)
            for s, reason in news_blocked:
                log(
                    f"{s['name']} 结构已够格，{reason}，暂缓自动开仓。",
                    console=False,
                )
    if not auto:
        log("本轮没有符合自动开仓条件的新信号（需已确认的 SOS/JOC/LPS 或 SOW/破冰/弹簧失败，且按最新标记价盈亏比达标（普通≥1.5，弹簧失败≥2.0）；刚出的重大相关新闻也会暂缓开仓）。", console=False)
        return candles

    log(f"本轮符合自动开仓条件的信号 {len(auto)} 个，本轮最多新开 1 笔。", console=False)
    signals = auto
    opened_this_round = 0
    caps: dict = {}
    min_cap = float(cfg.get("min_market_cap") or 0)
    if min_cap > 0 and any((s.get("category") or "").upper() == "CRYPTO" for s in signals):
        caps, cap_err = get_crypto_market_caps(keys.proxy, min_cap=min_cap)
        if cap_err and caps:
            log("拉取市值失败：" + redact(cap_err))
        elif cap_err:
            log("拉取市值失败：" + redact(cap_err) + "。本轮没有市值数据，加密新仓全部跳过。")
        else:
            log(f"已取得加密市值 {len(caps)} 个，低于 {fmt_market_cap(min_cap)} 的不开新仓。", console=False)
    skipped_same: list[str] = []
    skipped_held: list[str] = []
    for s in signals:
        name, info, m = s["name"], s["info"], s["market"]
        side_zh = "做多" if info["side"] == "long" else "做空"
        same = bool(seen.get(s["key"]))
        held_here = name in held
        log(
            f"信号 {name}（{s['category']}）{side_zh} {info.get('event')} "
            f"评分 {info.get('score')}  盈亏比 {s['rr']:.2f}  止损 {fnum(s['stop'])}  目标 {fnum(s['target'])}",
            console=not (same or held_here),
        )
        if same:
            log(f"  已处理过同一结构，跳过。", console=False)
            skipped_same.append(name)
            continue
        if held_here:
            log(f"  账户已有 {name} 仓位，不加仓。", console=False)
            seen[s["key"]] = now_sh()
            skipped_held.append(name)
            continue
        if open_count >= cfg["max_positions"]:
            log(f"  已达最大持仓数 {cfg['max_positions']}，本轮不再开新仓。")
            continue
        if opened_this_round >= 1:
            log("  本轮已开过 1 笔新仓，其余信号留到下一轮。")
            continue
        if (m.get("category") or "").upper() in {"EQUITIES", "INDICES", "COMMODITIES"} and not in_trading_hours(m):
            log(f"  {name} 目前不在常规交易时段，暂不开仓。")
            continue
        if (m.get("category") or "").upper() == "CRYPTO":
            min_cap = float(cfg.get("min_market_cap") or 0)
            if min_cap > 0:
                base = (m.get("baseAsset") or name or "")
                reason = crypto_skip_reason(lookup_market_cap(base, caps), min_cap)
                if reason:
                    log(f"  {name} {reason}，跳过。")
                    continue
        imf = float(m.get("initialMarginFraction") or 0.05)
        need = cfg["notional"] * imf * 1.8
        if free < max(need, 15):
            log(f"  可用保证金 {fnum(free)} 不足，跳过 {name}。")
            continue
        # 扫盘可能已过去几分钟，开仓前再拉一次最新标记价
        if not markets_refreshed:
            markets_refreshed = True
            try:
                fresh_list = arcus_markets(keys.proxy)
                fresh_by_name = {x.get("marketDisplayName"): x for x in fresh_list if x.get("marketDisplayName")}
            except Exception as e:
                log("  刷新标记价失败：" + redact(str(e)))
                fresh_by_name = {}
        if name in fresh_by_name:
            m = fresh_by_name[name]
            s["market"] = m
        mid = market_mark(m)
        reason = live_mark_blocks_entry(
            info["side"],
            mid,
            s["stop"],
            s.get("target"),
            min_auto_rr(info.get("event")),
            confirm=info.get("stop"),
        )
        if reason:
            log(f"  {reason}，不开仓。")
            if structure_already_gone(info["side"], mid, s["stop"], s.get("target")):
                seen[s["key"]] = now_sh()
            continue
        if news_items:
            news_reason = news_blocks_entry(news_items, name)
            if news_reason:
                log(f"  {news_reason}，暂缓开仓。")
                continue
        try:
            result = open_trade(keys, m, info["side"], cfg["notional"], live)
        except Exception as e:
            log(f"  开仓失败 {name}：{redact(str(e))}")
            continue
        seen[s["key"]] = now_sh()
        if result and not result.get("dry_run"):
            entry = float(result.get("mark") or info["last"])
            plans[name] = {
                "side": info["side"],
                "entry": entry,
                "stop": s["stop"],
                "init_stop": s["stop"],
                "target": s["target"],
                "event": info.get("event"),
                "signal_key": s["key"],
                "opened_at": now_sh(),
                "notional": cfg["notional"],
                "inherited": False,
            }
            open_count += 1
            opened_this_round += 1
            free -= need
            held.add(name)
            log(f"  已记录 {name} 结构止损 {fnum(s['stop'])}、目标 {fnum(s['target'])}。")
            fake_pos = {
                "marketDisplayName": name,
                "side": "LONG" if info["side"] == "long" else "SHORT",
                "size": result.get("size") or 0,
            }
            try:
                sync_exchange_tpsl(keys, m, fake_pos, plans[name], live)
            except Exception as e:
                log(f"  {name} 开仓后挂交易所止盈止损失败：{redact(str(e))}")
        elif result and result.get("dry_run"):
            seen.pop(s["key"], None)  # 干跑不占用信号
    if skipped_same:
        log("本轮因已处理过同一结构跳过：" + "、".join(skipped_same), console=False)
    if skipped_held:
        log("本轮因账户已有仓位不加仓：" + "、".join(skipped_held), console=False)
    return candles


def main() -> int:
    _win_utf8()
    p = argparse.ArgumentParser(description="Arcus 全部品种本地自动盯盘（含美股），默认实盘管理。")
    p.add_argument("--dry-run", action="store_true", help="只扫描和预览，不发送订单")
    p.add_argument("--once", action="store_true", help="只跑一轮后退出")
    args = p.parse_args()
    live = not args.dry_run

    try:
        keys = load_keys()
    except ConfigError as e:
        print(redact(str(e)), file=sys.stderr)
        return 2
    apply_proxy(keys.proxy)
    cfg = settings_from_api()
    state = load_state()

    log("Arcus 威科夫本地自动盯盘启动")
    log(
        f"模式：{'实盘自动开平仓' if live else '干跑预览'}  账户：{short_addr(keys.arcus_wallet_address)}  "
        f"每笔名义 {cfg['notional']} U  最多同时持仓 {cfg['max_positions']}  "
        f"轮询 {cfg['loop_seconds']} 秒  扫描间隔 {cfg['scan_minutes']} 分钟"
        f"（美股常规开盘时段改为每 {SCAN_MINUTES_US_RTH} 分钟）"
    )
    if cfg["min_market_cap"] > 0:
        log(f"加密市值低于 {fmt_market_cap(cfg['min_market_cap'])} 的品种不开新仓；查不到市值也不开。已有仓位照常管理。")
    log("只在确认结构出现时开仓，不止看弹簧或上冲本身；止损按结构失效位；达到 1 倍风险后移至保本并跟踪已收盘的 4 小时结构；目标取 2 倍风险和 20 日区间对侧里更近的那个。弹簧失败需盈亏比至少 2.0，且失败确认不超过 48 小时；开仓 24 小时后若还未走到 0.5 倍风险则撤出。列入自动开仓前会用当时标记价重算盈亏比，不够格的只记日志、不报成自动信号；开仓时标记价仍须站在确认位一侧，且到止损至少 0.5%，避免在冰线缓冲带里按噪声止损开仓。结构已经够格时，若刚出现相关重大新闻或全市场宏观事件，只暂缓自动开仓、不改开仓事件，也不把结构烧掉；没有相关快讯时开法与原来相同。新闻拉取失败则跳过这层过滤。")
    log("每笔开仓会在交易所同时挂上仓位止盈和止损；本地盯盘仍跟踪，止损上移时会撤掉旧单再改挂。")
    if live:
        log("窗口请保持打开。要停止时在窗口里按 Ctrl+C。账户快照只写入日志，窗口只显示开平仓、止盈止损和报错等关键事件。")

    last_scan = 0.0
    last_pos_key = None
    candles: dict = {}
    while True:
        try:
            markets = arcus_markets(keys.proxy)
            markets_by_name = {m.get("marketDisplayName"): m for m in markets if m.get("marketDisplayName")}
            st = arcus_status(keys)
            key = position_key(st)
            show = last_pos_key is None or key != last_pos_key
            log("账户：" + format_status_line(st), console=show)
            last_pos_key = key
            # 管理仓位时，如本轮还没有 K 线，先用缓存；扫描时会刷新
            interval = current_scan_minutes(cfg, markets)
            due = (time.time() - last_scan) >= interval * 60
            if due or not candles:
                candles = scan_and_enter(keys, markets, st, state, cfg, live)
                last_scan = time.time()
                # 扫描后重新取一次账户，便于立刻给新仓挂上管理
                st = arcus_status(keys)
                key = position_key(st)
                show = last_pos_key is None or key != last_pos_key
                log("账户：" + format_status_line(st), console=show)
                last_pos_key = key
                # 扫盘耗时长，管理仓位前再刷新一遍市场标记价，避免用过期价格误平刚开的仓
                try:
                    markets = arcus_markets(keys.proxy)
                    markets_by_name = {m.get("marketDisplayName"): m for m in markets if m.get("marketDisplayName")}
                except Exception as e:
                    log("刷新市场标记价失败：" + redact(str(e)))
            else:
                # 轻量刷新当前持仓品种的 4 小时线，用于跟踪止损
                held_names = [position_name(p) for p in (st.get("positions") or []) if position_name(p)]
                if held_names:
                    fresh = fetch_many_arcus_candles(held_names, keys.proxy, workers=4)
                    candles.update(fresh)
            reconcile_missing_positions(keys, markets_by_name, st, state, live)
            manage_open(keys, markets_by_name, st, state, candles, live)
            save_state(state)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log("本轮出错：" + redact(str(e)))
        if args.once:
            log("已按 --once 结束本轮。")
            return 0
        time.sleep(max(15, cfg["loop_seconds"]))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已停止本地盯盘。", file=sys.stderr)
        raise SystemExit(130)

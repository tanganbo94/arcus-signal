"""按威科夫结构扫描已收盘的日线与 4 小时线，只在结构、确认、失效位齐全时给出可交易信号。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any


H4_MS = 4 * 60 * 60 * 1000
D1_MS = 24 * 60 * 60 * 1000
# 刺破过浅视为噪音，过深且没收回视为破位
PIERCE_MIN, PIERCE_MAX = 0.20, 3.0
MIN_RANGE_PCT = 3.0
# 失败结构若已离开关键位较远，不再追打
CHASE_RANGE_FRAC = 0.5
CHASE_PCT = 3.0
# 弹簧失败确认超过这个时间，不再开仓
FAIL_MAX_AGE_SEC = 48 * 3600
SH = timezone(timedelta(hours=8))

PRICE_VS_OI = {
    "new_longs": "价涨且持仓增加，偏新开多",
    "short_cover": "价涨但持仓下降，偏空头回补",
    "new_shorts": "价跌且持仓增加，偏新开空",
    "long_cover": "价跌且持仓下降，偏多头离场",
    "flat": "价格和持仓变化都很小",
    "mixed": "价格与持仓关系不明确",
    "na": "没有合约持仓对照",
}


def _closed_bars(rows: list[dict], interval_ms: int, now_ms: int | None = None) -> list[dict]:
    """丢掉尚未收盘的最后一根，只在收盘确认上做判断。"""
    if not rows:
        return rows
    now_ms = now_ms or int(time.time() * 1000)
    last_ot = rows[-1].get("ot")
    if last_ot is None:
        return rows
    if int(last_ot) + interval_ms > now_ms:
        return rows[:-1]
    return rows


def _ret(rows: list[dict], n: int) -> float | None:
    if len(rows) < n + 1:
        return None
    a, b = rows[-(n + 1)]["c"], rows[-1]["c"]
    if a == 0:
        return None
    return (b / a - 1) * 100


def _avg_vol(rows: list[dict], n: int = 20) -> float | None:
    chunk = rows[-(n + 1):-1] if len(rows) > n else rows[:-1]
    if not chunk:
        return None
    return sum(x["v"] for x in chunk) / len(chunk)


def _range_pos(rows: list[dict], n: int = 20) -> tuple[float, float, float]:
    window = rows[-n:]
    low = min(x["l"] for x in window)
    high = max(x["h"] for x in window)
    last = rows[-1]["c"]
    pos = 0.5 if high == low else (last - low) / (high - low)
    return low, high, pos


def _close_strength(bar: dict) -> float:
    rng = bar["h"] - bar["l"]
    if rng <= 0:
        return 0.5
    return (bar["c"] - bar["l"]) / rng


def _too_far(last: float, key: float, range_h: float) -> bool:
    if not key:
        return True
    dist = abs(last - key)
    if key and dist / key * 100 >= CHASE_PCT:
        return True
    if range_h > 0 and dist >= CHASE_RANGE_FRAC * range_h:
        return True
    return False

def parse_event_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    raw = str(ts).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=SH)
        except ValueError:
            continue
    return None

def event_age_sec(ts: str | None, now_ts: float | None = None) -> float | None:
    dt = parse_event_ts(ts)
    if dt is None:
        return None
    now_ts = time.time() if now_ts is None else now_ts
    return now_ts - dt.timestamp()

def stale_event(ts: str | None, now_ts: float | None = None, max_age: float = FAIL_MAX_AGE_SEC) -> bool:
    age = event_age_sec(ts, now_ts)
    return age is not None and age > max_age

def chased_after_leave(last: float, key: float, range_h: float, confirm_i: int | None, n_bars: int) -> bool:
    """刚确认离开的那根已收盘K线允许收在关键位3%之外；其后若还离得远，不再追。"""
    if confirm_i is not None and n_bars > 0 and confirm_i >= n_bars - 1:
        return False
    return _too_far(last, key, range_h)
def _prior_range(h4: list[dict], i: int, lookback: int = 30) -> tuple[float, float] | None:
    prior = h4[max(0, i - lookback):i]
    if len(prior) < 8:
        return None
    support = min(x["l"] for x in prior)
    resist = max(x["h"] for x in prior)
    if not support or resist <= support:
        return None
    width = (resist - support) / support * 100
    if width < MIN_RANGE_PCT:
        return None
    return support, resist


def analyze(d1: list[dict], h4: list[dict], btc_rets: dict, deriv: dict | None = None) -> dict[str, Any]:
    d1 = _closed_bars(d1, D1_MS)
    h4 = _closed_bars(h4, H4_MS)
    if len(d1) < 25 or len(h4) < 30:
        return {"score": 0, "actionable": False, "reason": "已收盘K线不足"}

    last = h4[-1]["c"]
    r1, r3, r7 = _ret(d1, 1), _ret(d1, 3), _ret(d1, 7)
    rs7 = None if r7 is None or btc_rets.get("r7") is None else r7 - btc_rets["r7"]
    low20, high20, pos20 = _range_pos(d1, 20)
    av = _avg_vol(d1, 20)
    daily_vol_r = (d1[-1]["v"] / av) if av else None
    av4 = _avg_vol(h4, 20) or 0
    recent_cut = h4[-24]["ts"] if len(h4) >= 24 else h4[0]["ts"]

    springs, utads, climaxes = [], [], []
    for i in range(max(8, len(h4) - 36), len(h4)):
        bar = h4[i]
        rng_pr = _prior_range(h4, i)
        if not rng_pr:
            continue
        support, resist = rng_pr
        vr = (bar["v"] / av4) if av4 else 0
        rng = (bar["h"] - bar["l"]) / bar["l"] * 100 if bar["l"] else 0
        pierce_low = (support - bar["l"]) / support * 100 if support else 0
        pierce_high = (bar["h"] - resist) / resist * 100 if resist else 0
        # 弹簧：明确刺破后，当根收盘收回支撑之上，且收在K线中上部
        if (
            PIERCE_MIN < pierce_low < PIERCE_MAX
            and bar["c"] > support
            and _close_strength(bar) >= 0.5
        ):
            springs.append({
                "i": i, "ts": bar["ts"], "vol_r": round(vr, 2),
                "low": bar["l"], "close": bar["c"],
                "support": support, "resist": resist,
                "pierce_pct": round(pierce_low, 3),
            })
        # 上冲回落：刺破上沿后收盘回到阻力下方，且收在K线中下部
        if (
            PIERCE_MIN < pierce_high < PIERCE_MAX
            and bar["c"] < resist
            and _close_strength(bar) <= 0.5
        ):
            utads.append({
                "i": i, "ts": bar["ts"], "vol_r": round(vr, 2),
                "high": bar["h"], "close": bar["c"],
                "support": support, "resist": resist,
                "pierce_pct": round(pierce_high, 3),
            })
        if vr >= 2.0 and rng >= 2.0:
            climaxes.append({
                "i": i, "ts": bar["ts"], "vol_r": round(vr, 2), "range_pct": round(rng, 2),
                "dir": "up" if bar["c"] > bar["o"] else "down",
            })

    springs = [s for s in springs if s["ts"] >= recent_cut]
    utads = [u for u in utads if u["ts"] >= recent_cut]
    climaxes = [c for c in climaxes if c["ts"] >= recent_cut][-3:]

    last4, evr = h4[-1], 0
    if av4:
        evr = last4["v"] / av4
    last_range = (last4["h"] - last4["l"]) / last4["l"] * 100 if last4["l"] else 0
    effort_no_result = evr >= 1.8 and last_range < 1.2

    def _sos_after(spring: dict) -> dict | None:
        mid = spring["support"] + (spring["resist"] - spring["support"]) * 0.5
        found = None
        for j in range(spring["i"] + 1, len(h4)):
            bar = h4[j]
            vr = (bar["v"] / av4) if av4 else 0
            if bar["c"] > mid and vr >= 1.5 and _close_strength(bar) >= 0.5:
                found = {
                    "i": j, "ts": bar["ts"], "vol_r": round(vr, 2),
                    "close": bar["c"], "level": mid, "low": bar["l"],
                }
        return found

    def _joc(after_i: int, ice: float) -> dict | None:
        """收盘站上冰线后，至少再有一根已收盘K线仍站在冰上。失败后若再出现，只认最新一次确认。"""
        joc_i = None
        confirmed = None
        for j in range(max(after_i, 0), len(h4)):
            if h4[j]["c"] > ice:
                if joc_i is None:
                    joc_i = j
                elif j > joc_i:
                    confirmed = {
                        "i": j, "ts": h4[j]["ts"], "ice": ice,
                        "close": h4[j]["c"], "joc_ts": h4[joc_i]["ts"],
                    }
            else:
                joc_i = None
                confirmed = None
        return confirmed

    def _lps_after(confirm: dict, floor: float) -> dict | None:
        start = confirm["i"] + 1
        sos_vol = confirm.get("vol_r") or 0
        found = None
        for j in range(start, len(h4)):
            bar = h4[j]
            vr = (bar["v"] / av4) if av4 else 0
            if bar["l"] < floor:
                return None
            lighter = (sos_vol and vr < sos_vol) or (av4 and bar["v"] < av4)
            if lighter and bar["c"] > floor and _close_strength(bar) >= 0.5:
                found = {
                    "i": j, "ts": bar["ts"], "vol_r": round(vr, 2),
                    "low": bar["l"], "close": bar["c"], "floor": floor,
                }
        return found

    def _sow_after(utad: dict) -> dict | None:
        mid = utad["support"] + (utad["resist"] - utad["support"]) * 0.5
        found = None
        for j in range(utad["i"] + 1, len(h4)):
            bar = h4[j]
            vr = (bar["v"] / av4) if av4 else 0
            if bar["c"] < mid and vr >= 1.5 and _close_strength(bar) <= 0.5:
                found = {
                    "i": j, "ts": bar["ts"], "vol_r": round(vr, 2),
                    "close": bar["c"], "level": mid, "high": bar["h"],
                }
        return found

    def _ice_break(after_i: int, ice: float) -> dict | None:
        brk_i = None
        confirmed = None
        for j in range(max(after_i, 0), len(h4)):
            if h4[j]["c"] < ice:
                if brk_i is None:
                    brk_i = j
                elif j > brk_i:
                    confirmed = {
                        "i": j, "ts": h4[j]["ts"], "ice": ice,
                        "close": h4[j]["c"], "break_ts": h4[brk_i]["ts"],
                    }
            else:
                brk_i = None
                confirmed = None
        return confirmed

    observations: list[str] = []
    side = None
    event = None
    stop = None
    key_price = None
    event_ts = None
    range_h = 0.0
    score = 0
    reasons: list[str] = []

    # —— 多头：弹簧本身只算观察，必须再有 SOS / JOC / LPS ——
    spring = springs[-1] if springs else None
    spring_failed = False
    if spring:
        range_h = spring["resist"] - spring["support"]
        later_fail = next((h4[j] for j in range(spring["i"] + 1, len(h4)) if h4[j]["c"] < spring["low"]), None)
        if later_fail is not None:
            spring_failed = True
            if _too_far(last, spring["low"], range_h):
                observations.append(f"弹簧已失败且价格离开低点较远，不再追空（{spring['ts']}）")
            elif stale_event(later_fail["ts"]):
                observations.append(f"弹簧失败发生在 {later_fail['ts']}，已超过 48 小时，不再开仓")
            else:
                # 弹簧失败：收盘跌破弹簧低点，且尚未走远，可作空头候选
                side, event, stop = "short", "弹簧失败", later_fail["h"]
                key_price = spring["low"]
                event_ts = later_fail["ts"]
                score += 10
                reasons.append(f"弹簧失败：{later_fail['ts']} 收盘跌破弹簧低点 {spring['low']}")
        elif last <= spring["low"]:
            spring_failed = True
            observations.append("最近收盘已在弹簧低点之下，不能当弹簧做多")
        else:
            sos = _sos_after(spring)
            joc = _joc(spring["i"] + 1, spring["resist"])
            confirm = None
            confirm_floor = spring["support"]
            if sos and (not joc or sos["i"] >= joc["i"]):
                confirm, confirm_floor = sos, spring["support"]
                if last > spring["low"] and last > sos["level"]:
                    side, event, stop = "long", "SOS", spring["low"]
                    key_price = sos["level"]
                    event_ts = sos["ts"]
                    score += 10
                    reasons.append(
                        f"日线定位后，{spring['ts']} 弹簧收回，随后 {sos['ts']} 放量 SOS 收盘站上区间中部 {sos['level']:.6g}"
                    )
                else:
                    observations.append("曾有 SOS，但最近收盘已失守触发位，不作多")
            elif joc:
                confirm, confirm_floor = joc, joc["ice"]
                if last > joc["ice"]:
                    if chased_after_leave(last, joc["ice"], range_h, joc.get("i"), len(h4)):
                        observations.append(
                            f"弹簧后已越冰，但价格离开冰线较远，不再追多（{joc['joc_ts']}）"
                        )
                    else:
                        side, event, stop = "long", "JOC", joc["ice"]
                        key_price = joc["ice"]
                        event_ts = joc["ts"]
                        score += 10
                        reasons.append(
                            f"{spring['ts']} 弹簧后，{joc['joc_ts']} 收盘越过冰线 {joc['ice']:.6g}，且之后仍站在冰上"
                        )
                else:
                    observations.append("曾有 JOC，但最近收盘已回到冰下，视为失败越冰")
            else:
                observations.append(
                    f"弹簧低点仍有效（{spring['ts']}，低点 {spring['low']}），等待 SOS 越过 {(spring['support']+spring['resist'])/2:.6g}，或收盘站稳冰线 {spring['resist']:.6g}"
                )
            if confirm and side == "long":
                lps = _lps_after(confirm, confirm_floor)
                if lps and last > lps["low"]:
                    side, event, stop = "long", "LPS", lps["low"]
                    key_price = lps["low"]
                    event_ts = lps["ts"]
                    score += 2  # 在 SOS/JOC 基础上更优的回踩
                    reasons.append(
                        f"之后 {lps['ts']} 缩量回踩更高支撑并守住，记为 LPS，止损改看回踩低点 {lps['low']}"
                    )

    # —— 空头：上冲回落本身只算观察，必须再有 SOW / 破冰 ——
    utad = utads[-1] if utads else None
    # 若已因弹簧失败做空，只有更新的派发确认才能覆盖
    short_locked = side == "short" and event == "弹簧失败"
    if utad and not (side == "long"):
        range_h = utad["resist"] - utad["support"]
        later_fail = next((h4[j] for j in range(utad["i"] + 1, len(h4)) if h4[j]["c"] > utad["high"]), None)
        if later_fail is not None:
            observations.append(f"上冲回落已失效（收盘站回失败高点之上，{later_fail['ts']}）")
        elif last >= utad["high"]:
            observations.append("最近收盘已在上冲高点之上，不能当 UTAD 做空")
        else:
            sow = _sow_after(utad)
            brk = _ice_break(utad["i"] + 1, utad["support"])
            take_short = None
            if sow and (not brk or sow["i"] >= brk["i"]):
                if last < utad["high"] and last < sow["level"]:
                    if chased_after_leave(last, sow["level"], range_h, sow.get("i"), len(h4)):
                        observations.append(f"上冲后已有 SOW，但价格离开中轴较远，不再追空（{sow['ts']}）")
                    else:
                        take_short = ("SOW", utad["high"], sow["level"], sow["ts"],
                                      f"{utad['ts']} 上冲回落后，{sow['ts']} 放量 SOW 收盘跌破区间中部 {sow['level']:.6g}")
                else:
                    observations.append("曾有 SOW，但最近收盘已失守触发位，不作空")
            elif brk:
                if last < brk["ice"]:
                    if chased_after_leave(last, brk["ice"], range_h, brk.get("i"), len(h4)):
                        observations.append(f"上冲后已破冰，但价格离开冰线较远，不再追空（{brk['break_ts']}）")
                    else:
                        take_short = ("破冰", brk["ice"], brk["ice"], brk["ts"],
                                      f"{utad['ts']} 上冲回落后，{brk['break_ts']} 收盘跌破冰线 {brk['ice']:.6g}，且回抽未站回")
                else:
                    observations.append("曾有破冰，但最近收盘已重新站回冰上")
            else:
                observations.append(
                    f"上冲高点仍有效（{utad['ts']}，高点 {utad['high']}），等待 SOW 跌破 {(utad['support']+utad['resist'])/2:.6g}，或收盘跌破冰线 {utad['support']:.6g}"
                )
            if take_short and (not short_locked or (take_short[3] >= (event_ts or ""))):
                ev, stop, key_price, event_ts, why = take_short
                side, event = "short", ev
                score = 10
                reasons.append(why)

    # 没有弹簧时，不允许只凭一根放量阳线做多；但区间上沿的确认离开仍可作 JOC
    if side is None:
        recent = h4[-30:]
        ice = max(x["h"] for x in recent[:-2]) if len(recent) > 2 else None
        floor = min(x["l"] for x in recent[:-2]) if len(recent) > 2 else None
        if ice and floor and (ice - floor) / floor * 100 >= MIN_RANGE_PCT:
            joc = _joc(len(h4) - 24, ice)
            brk = _ice_break(len(h4) - 24, floor)
            if joc and last > joc["ice"] and not _too_far(last, joc["ice"], ice - floor):
                side, event, stop = "long", "JOC", joc["ice"]
                key_price, event_ts = joc["ice"], joc["ts"]
                score += 10
                reasons.append(f"{joc['joc_ts']} 收盘越过冰线 {joc['ice']:.6g}，且之后仍站在冰上")
            elif brk and last < brk["ice"] and not _too_far(last, brk["ice"], ice - floor):
                side, event, stop = "short", "破冰", brk["ice"]
                key_price, event_ts = brk["ice"], brk["ts"]
                score += 10
                reasons.append(f"{brk['break_ts']} 收盘跌破冰线 {brk['ice']:.6g}，且之后回抽未站回")

    if climaxes:
        score += 2
        reasons.append("近期有放量宽幅高潮")
    if pos20 <= 0.25 and side == "long":
        score += 2
        reasons.append(f"日线靠近 20 日低位（{pos20:.2f}）")
    elif pos20 >= 0.85 and side == "short":
        score += 2
        reasons.append(f"日线靠近 20 日高位（{pos20:.2f}）")
    if rs7 is not None and abs(rs7) >= 5:
        if (side == "long" and rs7 > 0) or (side == "short" and rs7 < 0) or side is None:
            score += 2
        reasons.append(("相对比特币偏强 " if rs7 > 0 else "相对比特币偏弱 ") + f"{rs7:.1f}%")
    if effort_no_result:
        reasons.append("近端已收盘棒放量但推进很小，供需可能在吸收，不算单独信号")

    # 明确下跌趋势里的浅刺，即使看起来像弹簧，没有 SOS/JOC 也不能做多（上面已处理）
    if side == "long" and r7 is not None and r7 <= -12 and event == "SOS" and pos20 < 0.2 and (rs7 or 0) < 0:
        score -= 3
        reasons.append("日线仍在偏弱下跌环境中，即使有 SOS 也降权")

    deriv = deriv or {}
    oi_note = PRICE_VS_OI.get(
        __import__("market").classify_price_vs_oi(r1, deriv.get("oi_chg_pct")),
        "",
    )
    fund = deriv.get("funding_pct")
    crowded_long = fund is not None and fund >= 0.05
    crowded_short = fund is not None and fund <= -0.05
    if side == "long" and crowded_long:
        score -= 2
        reasons.append("资金费偏高，多头较拥挤，信号降权")
    if side == "short" and crowded_short:
        score -= 2
        reasons.append("资金费偏低，空头较拥挤，信号降权")
    if side == "long" and deriv.get("oi_chg_pct") is not None and deriv["oi_chg_pct"] < 0 and r1 and r1 > 0:
        reasons.append("价格上涨时持仓下降，更像空头回补，需谨慎")
    if oi_note:
        reasons.append(oi_note)

    reasons.extend(observations)

    # 可交易：必须有方向、事件、失效位；确认结构本身已是硬条件
    actionable = bool(side and event and stop is not None)
    key = None
    if actionable and event and event_ts:
        key = f"{event}:{event_ts}"
    return {
        "last": last,
        "ret_1d": None if r1 is None else round(r1, 2),
        "ret_7d": None if r7 is None else round(r7, 2),
        "rs_7d": None if rs7 is None else round(rs7, 2),
        "pos20": round(pos20, 3),
        "low20": low20,
        "high20": high20,
        "daily_vol_r": None if daily_vol_r is None else round(daily_vol_r, 2),
        "side": side,
        "event": event,
        "stop": stop,
        "key_price": key_price,
        "score": score,
        "reasons": reasons,
        "actionable": actionable,
        "signal_key": key,
        "deriv": deriv,
        "observations": observations,
    }

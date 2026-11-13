#!/usr/bin/env python3
"""本地威科夫扫描：免费行情 + 主流新闻，只打印可操作信号，默认不下单。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from keys import apply_proxy, load_keys, load_proxy_only, ConfigError, redact
from market import (
    classify_price_vs_oi,
    fetch_binance_fut_usdt,
    fetch_binance_spot_usdt,
    fetch_deriv,
    fetch_fear_greed,
    fetch_klines,
    fetch_many_klines,
    fetch_watchlist,
    map_pair,
)
from news import fetch_news, match_news
from wyckoff import PRICE_VS_OI, analyze

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Asia/Shanghai")
STATE_FILE = ROOT / "signal_state.json"
OUT_FILE = ROOT / "scan_latest.json"


def _win_utf8() -> None:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def now_sh() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def load_state() -> dict:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def btc_rets_from(d1: list[dict]) -> dict:
    def ret(n):
        if len(d1) < n + 1 or d1[-(n + 1)]["c"] == 0:
            return None
        return (d1[-1]["c"] / d1[-(n + 1)]["c"] - 1) * 100
    return {"r1": ret(1), "r3": ret(3), "r7": ret(7)}


def main() -> int:
    _win_utf8()
    p = argparse.ArgumentParser(description="本地扫描可操作威科夫信号，默认不下单。")
    p.add_argument("--coin", help="只看某一个币，例如 BTC")
    p.add_argument("--status", action="store_true", help="同时查询本机 Arcus 账户（需要 API.txt）")
    args = p.parse_args()

    proxy = load_proxy_only()
    apply_proxy(proxy)
    print(f"本地扫描 {now_sh()} 上海时区")
    print("数据来源：CoinGecko 市值榜、币安公开行情、主流媒体 RSS。不使用付费接口，默认不下单。")
    if proxy:
        print("已启用本机代理（不会打印代理地址里的密码）。")

    if args.status:
        try:
            from arcus import TradeError, arcus_status, print_status
            keys = load_keys()
            apply_proxy(keys.proxy or proxy)
            st = arcus_status(keys)
            print()
            print_status(keys, st)
            if st.get("positions"):
                print("注意：账户里已有持仓。若这是正在管理的价差对冲，不要用本脚本再开单向仓。")
            print()
        except (ConfigError, Exception) as e:
            from arcus import TradeError
            if isinstance(e, (ConfigError, TradeError)):
                print(redact(str(e)))
            else:
                print("查询 Arcus 账户失败：", redact(str(e)))
            print()

    try:
        watch = fetch_watchlist(proxy)
        spot = fetch_binance_spot_usdt(proxy)
        futs = fetch_binance_fut_usdt(proxy)
    except Exception as e:
        print("拉取市值榜或币安交易对失败：", redact(str(e)))
        print("若在国内网络超时，可在 API.txt 里加上 PROXY=http://127.0.0.1:10808 后再试。")
        return 1

    arcus_bases = set()
    try:
        from arcus import arcus_markets, listed_bases
        arcus_bases = listed_bases(arcus_markets(proxy))
        print(f"Arcus 在线永续约 {len(arcus_bases)} 个名称（用于过滤可下单标的）。")
    except Exception as e:
        print("未能读取 Arcus 市场列表，将先扫描全部，稍后再对照能否下单：", redact(str(e)))

    fg = fetch_fear_greed(proxy)
    if fg:
        print(f"恐惧贪婪指数：{fg.get('value')}（{fg.get('label') or '—'}）")
    try:
        news = fetch_news(proxy)
        print(f"已读取新闻 {len(news)} 条（CoinDesk / The Block / Cointelegraph）。")
    except Exception:
        news = []
        print("新闻源暂时不可用，本轮只根据行情结构扫描。")

    mapped = []
    for w in watch:
        if args.coin and w["symbol"] != args.coin.upper():
            continue
        pair = map_pair(w["symbol"], spot)
        if not pair:
            continue
        mapped.append({**w, "pair": pair})
    if not mapped:
        print("没有可映射到币安 USDT 现货的币种。")
        return 1

    print(f"观察池：市值前约 100 非稳定币中，已映射 {len(mapped)} 个币安交易对。开始拉 K 线…")
    klines = fetch_many_klines([m["pair"] for m in mapped], proxy)
    btc_kl = klines.get("BTCUSDT") or {}
    if not btc_kl.get("d1"):
        try:
            btc_kl = {"d1": fetch_klines("BTCUSDT", "1d", 80, proxy), "h4": []}
        except Exception:
            btc_kl = {"d1": [], "h4": []}
    btc = btc_rets_from(btc_kl.get("d1") or [])

    state = load_state()
    seen = state.get("signals") or {}
    signals = []
    for w in mapped:
        kl = klines.get(w["pair"])
        if not kl or not kl.get("d1") or not kl.get("h4"):
            continue
        deriv = {"available": False}
        if w["pair"] in futs:
            deriv = fetch_deriv(w["pair"], proxy)
            px = None
            if len(kl["d1"]) >= 2:
                px = (kl["d1"][-1]["c"] / kl["d1"][-2]["c"] - 1) * 100
            deriv["price_vs_oi"] = classify_price_vs_oi(px, deriv.get("oi_chg_pct"))
        info = analyze(kl["d1"], kl["h4"], btc, deriv)
        if not info.get("actionable"):
            continue
        key = f"{w['symbol']}:{info.get('signal_key')}"
        if seen.get(key):
            continue
        on_arcus = (not arcus_bases) or (w["symbol"] in arcus_bases) or (w["pair"].replace("USDT", "") in arcus_bases)
        item = {
            **w,
            **info,
            "on_arcus": on_arcus,
            "news": match_news(news, w["symbol"], w.get("name") or ""),
            "state_key": key,
        }
        signals.append(item)

    signals.sort(key=lambda x: x.get("score") or 0, reverse=True)
    print()
    if not signals:
        print("本轮没有新的可操作信号（旧结构不会重复提示）。")
    else:
        print(f"新的可操作信号：{len(signals)} 个")
        for s in signals:
            side_zh = "做多" if s["side"] == "long" else "做空"
            arcus_txt = "可在 Arcus 下单" if s.get("on_arcus") else "Arcus 上可能没有对应合约，先不要下"
            print()
            print(f"[{s['symbol']}] {side_zh}  {s.get('event') or ''}  评分 {s['score']}  {arcus_txt}")
            print(f"  价格 {s['last']}  1日 {s.get('ret_1d')}%  7日相对BTC {s.get('rs_7d')}%")
            if s.get("stop"):
                print(f"  结构止损参考：{s['stop']}（弹簧低点下方 / 上冲高点上方）")
            for r in s.get("reasons") or []:
                print(f"  - {r}")
            d = s.get("deriv") or {}
            if d.get("available"):
                fund = d.get("funding_pct")
                fund_s = f"{fund:.4f}%" if fund is not None else "—"
                print(
                    f"  合约旁证：资金费 {fund_s}  持仓4小时变化 "
                    f"{d.get('oi_chg_pct') if d.get('oi_chg_pct') is not None else '—'}%  "
                    f"{PRICE_VS_OI.get(d.get('price_vs_oi') or 'na', '')}"
                )
            for n in s.get("news") or []:
                print(f"  新闻（{n['source']} {n.get('time') or ''}）：{n['title']}")
            if s.get("on_arcus"):
                side = "long" if s["side"] == "long" else "short"
                print(f"  若确认下单（先干跑）： py trade_one.py --dry-run --side {side} --coin {s['symbol']} --notional 2000")

    OUT_FILE.write_text(json.dumps({
        "time": now_sh(),
        "signals": [
            {k: v for k, v in s.items() if k not in {"reasons"} or True}
            for s in signals
        ],
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    # 记下已提示的结构，避免下一轮重复
    for s in signals:
        seen[s["state_key"]] = now_sh()
    state["signals"] = seen
    state["updated"] = now_sh()
    save_state(state)
    print()
    print("扫描完成。本轮没有发送任何订单。结果已写入 scan_latest.json。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)

#!/usr/bin/env python3
"""只在 Arcus 下一腿。默认干跑；实盘必须同时加上 --live --i-confirm。"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from zoneinfo import ZoneInfo
from datetime import datetime

from arcus import (
    TradeError,
    arcus_markets,
    arcus_status,
    build_order,
    find_market,
    fnum,
    place_order,
    print_status,
)
from keys import ConfigError, apply_proxy, load_keys, redact, short_addr

TZ = ZoneInfo("Asia/Shanghai")


def _win_utf8() -> None:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def main() -> int:
    _win_utf8()
    p = argparse.ArgumentParser(description="Arcus 单向开仓。默认干跑，不下单。")
    p.add_argument("--status", action="store_true", help="只查账户，不下单")
    p.add_argument("--dry-run", action="store_true", help="打印将要发送的订单，不真正下单")
    p.add_argument("--live", action="store_true", help="真正下单；必须同时加上 --i-confirm")
    p.add_argument("--i-confirm", action="store_true", dest="i_confirm", help="确认发送实盘订单")
    p.add_argument("--side", choices=("long", "short"), help="做多或做空")
    p.add_argument("--coin", help="例如 BTC 或 BTC-USD")
    p.add_argument("--size", help="标的数量；与 --notional 二选一")
    p.add_argument("--notional", help="名义金额（按标记价折算数量），例如 2000")
    p.add_argument("--price", help="可选限价；省略则按标记价 ±5% 保护限价 IOC")
    args = p.parse_args()

    if args.live and args.dry_run:
        print("不能同时使用 --live 和 --dry-run。")
        return 2
    if args.live and not args.i_confirm:
        print("拒绝：--live 必须同时加上 --i-confirm。本次未发送任何订单。")
        return 2

    try:
        keys = load_keys()
    except ConfigError as e:
        print(redact(str(e)), file=sys.stderr)
        return 2
    apply_proxy(keys.proxy)

    print(f"Arcus 单向  {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} 上海时区")
    print(f"配置来源：{keys.source_name}（只在本机读取，密钥不会打印）  账户：{short_addr(keys.arcus_wallet_address)}")

    try:
        st = arcus_status(keys)
        print()
        print_status(keys, st)
        if st.get("positions") and (args.live or args.dry_run):
            print("注意：账户里已有持仓。若其中包含正在管理的价差对冲，继续开单向仓会把保证金和仓位搅在一起。")
    except TradeError as e:
        print(redact(str(e)))
        if args.status and not (args.live or args.dry_run or args.coin):
            return 1

    if args.status and not (args.live or args.dry_run or args.coin):
        return 0
    if not (args.live or args.dry_run):
        print("\n未指定 --dry-run 或 --live，只查询了账户。预览订单请加 --dry-run。")
        return 0
    if not args.side or not args.coin or (not args.size and not args.notional):
        print("下单需要 --side、--coin，以及 --size 或 --notional。")
        return 2

    try:
        markets = arcus_markets(keys.proxy)
        market = find_market(markets, args.coin)
        mid = market.get("markPrice") or market.get("oraclePrice") or market.get("lastTradePrice")
        if args.size:
            size = Decimal(str(args.size))
        else:
            if mid is None:
                raise TradeError("没有标记价，无法按名义金额折算数量，请改用 --size。")
            size = Decimal(str(args.notional)) / Decimal(str(mid))
        if size <= 0:
            raise TradeError("数量必须为正数。")
        side = "BUY" if args.side == "long" else "SELL"
        planned = build_order(keys, market, side, size, args.price)
    except TradeError as e:
        print(redact(str(e)))
        return 1

    print("\n—— 将发送的订单 ——")
    print(f"  场所：Arcus  市场：{planned['market']}  方向：{planned['side']}")
    print(f"  类型：{planned['type']} / {planned['tif']}  数量：{planned['size']}  价格：{planned['price']}  标记：{planned['mark']}")
    if not args.live:
        print("\n这是干跑，订单没有发送。确认无误后再使用：")
        cmd = f" py trade_one.py --live --i-confirm --side {args.side} --coin {args.coin}"
        if args.size:
            cmd += f" --size {args.size}"
        else:
            cmd += f" --notional {args.notional}"
        if args.price:
            cmd += f" --price {args.price}"
        print(cmd)
        return 0

    print("\n开始发送订单（已带 --live --i-confirm）…")
    try:
        result = place_order(keys, planned)
    except Exception as e:
        print("下单失败：", redact(str(e)))
        return 1
    print("Arcus 已受理：", redact(json.dumps(result, ensure_ascii=False, default=str)[:800]))
    print("最终成交请以网页或再次运行 py trade_one.py --status 为准。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)

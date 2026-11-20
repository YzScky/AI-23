"""binance_order.py - 币安合约下单命令行（默认 dry-run，不发真单）。

用法（在平台目录里，用平台自己的 venv 跑）：
  .venv/bin/python binance_order.py --symbol BTCUSDT --side BUY
      dry-run：只校验 + 风控上限 + 查公开价算单，不发任何东西
  .venv/bin/python binance_order.py --symbol BTCUSDT --side BUY --live --confirm
      实盘：还要 config.py 里 binance.live_trading=true 才真发；缺任一条件都不发
  .venv/bin/python binance_order.py --symbol BTCUSDT --side SELL --close
      平多（dry-run）：只平现有的多头，不开空

硬护栏（缺一不可才会发真单）：
  1. 命令行同时带 --live 和 --confirm
  2. config.py 里 binance.live_trading=true（默认 False）
  3. 风控上限通过（只做多；保证金 ≤ 10U；杠杆 ≤ 10x；名义 ≤ 100U；
     当日亏损 < 日亏上限 50U）
  4. 币安已链接（.env 凭证齐全）
纸面默认：保证金 10U，杠杆 10x，止损 5%，止盈 60%，只做多。
输出开头会用大字标明 [DRY-RUN 模拟] 或 [LIVE 实盘]。每一次尝试都记进
binance_orders.log。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from quant_platform.binance_client import (
    BinanceError, BinanceLink, LiveTradingDisabledError, RiskRefusedError,
    binance_cfg)


def main():
    ap = argparse.ArgumentParser(description="币安合约下单（默认 dry-run）")
    ap.add_argument("--symbol", default="BTCUSDT", help="合约币种，默认 BTCUSDT")
    ap.add_argument("--side", required=True, choices=["BUY", "SELL"])
    ap.add_argument("--margin", type=float, default=None,
                    help="保证金 U，默认 config 里的 10U")
    ap.add_argument("--leverage", type=float, default=None,
                    help="杠杆，默认 config 里的 10x")
    ap.add_argument("--close", action="store_true",
                    help="平多：SELL 时必须带，不开空")
    ap.add_argument("--price", type=float, default=None,
                    help="指定价（不填就拉一次公开价）")
    ap.add_argument("--live", action="store_true", help="发真单（还要 --confirm 和 config 开关）")
    ap.add_argument("--confirm", action="store_true", help="确认发真单")
    args = ap.parse_args()

    live = bool(args.live and args.confirm)
    if args.live and not args.confirm:
        print("[拒绝] --live 必须和 --confirm 一起用。未发送。")
        return 1
    cfg = binance_cfg()
    if live and not cfg.get("live_trading", False):
        print("[拒绝] config.py binance.live_trading=false，实盘开关没开。未发送。")
        print("开实盘要先定策略、接好币安 API、亲口确认实盘参数。")
        return 1
    if args.side == "SELL" and not args.close:
        print("[拒绝] 只做多：SELL 必须带 --close（平多），不开空。未发送。")
        return 1

    print("[LIVE 实盘] 发真单" if live else "[DRY-RUN 模拟] 未发送真单")
    link = BinanceLink()
    try:
        res = link.place_order(args.symbol, args.side, margin=args.margin,
                               leverage=args.leverage, dry_run=not live,
                               close=args.close, price=args.price)
    except LiveTradingDisabledError as e:
        print("[拒绝] " + str(e))
        return 1
    except RiskRefusedError as e:
        print("[拒绝] " + str(e))
        return 1
    except (BinanceError, Exception) as e:
        print("[失败] " + str(e)[:200])
        return 1

    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("dry_run"):
        print("结果：模拟通过" if res.get("would_send") else "结果：模拟未通过（见 reason）")
        if res.get("filters_from_defaults"):
            print("注意：精度用的默认值（exchangeInfo 拉不到），实盘前先在 Mac 上跑"
                  " binance_link.py --test 确认 fapi 通不通。")
    else:
        print(f"结果：已发送，主单号 {res.get('order_id')}，状态 {res.get('status')}")
        print(f"止损单号：{res.get('stop_order_id')}，止盈单号：{res.get('take_profit_order_id')}")
        if res.get("stop_order_error") or res.get("take_profit_order_error"):
            print("警告：止损/止盈有挂不上的——主单已成交，立即人工检查挂单！")
            print("止损错误：" + str(res.get("stop_order_error") or "无"))
            print("止盈错误：" + str(res.get("take_profit_order_error") or "无"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

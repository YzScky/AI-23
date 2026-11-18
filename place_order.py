"""place_order.py - 下单命令行（默认 dry-run，不发真单）。

用法（在平台目录里，用平台自己的 venv 跑）：
  .venv/bin/python place_order.py --token TOKEN_ID --side BUY --price 0.40 --size 100
      dry-run：只校验 + 风控上限 + 查公开盘口，不发任何东西
  .venv/bin/python place_order.py --token TOKEN_ID --side BUY --price 0.40 --size 100 --live --confirm
      实盘：还要 config.py live_trading=true 才真发；缺任一条件都不发

硬护栏（缺一不可才会发真单）：
  1. 命令行同时带 --live 和 --confirm
  2. config.py 里 live_trading=true（默认 False）
  3. 风控上限通过（单笔名义金额 <= max_pair_size；当日亏损 < 日亏上限）
  4. 钱包已链接（.env 凭证齐全）
输出开头会用大字标明 [DRY-RUN 模拟] 或 [LIVE 实盘]。每一次尝试都记进 orders.log。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from quant_platform.clob_client import (
    ClobLink, ClobLinkError, LiveTradingDisabledError, RiskRefusedError)
from config import CONFIG


def main():
    ap = argparse.ArgumentParser(description="Polymarket 下单（默认 dry-run）")
    ap.add_argument("--token", required=True, help="outcome token_id")
    ap.add_argument("--side", required=True, choices=["BUY", "SELL"])
    ap.add_argument("--price", required=True, type=float, help="限价，0~1")
    ap.add_argument("--size", required=True, type=float, help="数量（份数）")
    ap.add_argument("--live", action="store_true", help="发真单（还要 --confirm 和 config 开关）")
    ap.add_argument("--confirm", action="store_true", help="确认发真单")
    args = ap.parse_args()

    live = bool(args.live and args.confirm)
    if args.live and not args.confirm:
        print("[拒绝] --live 必须和 --confirm 一起用。未发送。")
        return 1
    if live and not CONFIG.get("live_trading", False):
        print("[拒绝] config.py live_trading=false，实盘开关没开。未发送。")
        print("开实盘要先定策略、接好钱包、亲口确认实盘参数。")
        return 1

    print("[LIVE 实盘] 发真单" if live else "[DRY-RUN 模拟] 未发送真单")
    link = ClobLink()
    try:
        res = link.place_order(args.token, args.side, args.price, args.size,
                               dry_run=not live)
    except LiveTradingDisabledError as e:
        print("[拒绝] " + str(e))
        return 1
    except RiskRefusedError as e:
        print("[拒绝] " + str(e))
        return 1
    except ClobLinkError as e:
        print("[失败] " + str(e))
        return 1
    except Exception as e:
        print("[失败] " + str(e)[:200])
        return 1

    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("dry_run"):
        print("结果：模拟通过" if res.get("would_send") else "结果：模拟未通过（见 reason）")
    else:
        print("结果：已发送，订单号 %s，状态 %s" % (res.get("order_id"), res.get("status")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

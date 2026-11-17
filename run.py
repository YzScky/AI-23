"""Polymarket 量化平台入口。纸面模式默认，不下真单。"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CONFIG
from quant_platform.engine import Engine

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=600, help="运行秒数，默认 600")
    ap.add_argument("--markets", type=int, default=0, help="订阅市场数，默认用 config 里的")
    ap.add_argument("--poll", type=float, default=0, help="REST 轮询秒数，0=走 WebSocket；WS 被拦时用 --poll 5")
    ap.add_argument("--no-history", action="store_true", help="不写 history.db（默认记录）")
    args = ap.parse_args()
    cfg = dict(CONFIG)
    if args.markets:
        cfg["markets"] = dict(cfg["markets"], max_markets=args.markets)
    if args.poll:
        cfg["poll_interval"] = args.poll
    if args.no_history:
        cfg["history"] = False
    eng = Engine(cfg)
    out = eng.run(args.duration)
    print(json.dumps(out, ensure_ascii=False, indent=2))

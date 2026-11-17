"""历史数据查询：读本地 history.db（SQLite）。
用法：
  python history.py --stats                        # 各表行数、DB 大小、各市场最新盘口
  python history.py --market <market_id> --limit 5 # 该市场最近 5 行盘口
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quant_platform.storage import HistoryStore


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true", help="各表行数、DB 大小、各市场最新盘口")
    ap.add_argument("--market", default=None, help="市场 id（见 --stats 输出）")
    ap.add_argument("--limit", type=int, default=5, help="--market 时返回行数，默认 5")
    args = ap.parse_args()
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")
    if not os.path.exists(db):
        print("history.db 不存在：先跑一次平台（./start.command 或 run.py --poll 5）")
        return
    store = HistoryStore(db)
    try:
        if args.stats:
            c = store.counts()
            print(f"books: {c['books']} 行")
            print(f"trades: {c['trades']} 行")
            print(f"DB 大小: {os.path.getsize(db) / 1024:.1f} KB")
            print("各市场最新盘口（ts, market_id, 问题, a_bid/a_ask, b_bid/b_ask）：")
            for row in store.latest_books():
                print(f"  {row[0]} | {row[1]} | {row[2]} | a {row[3]}/{row[4]} b {row[5]}/{row[6]}")
        elif args.market:
            rows = store.recent_books(args.market, args.limit)
            if not rows:
                print(f"市场 {args.market} 无记录（id 见 history.py --stats）")
            for row in rows:
                print(row)
        else:
            ap.print_help()
    finally:
        store.close()


if __name__ == "__main__":
    main()

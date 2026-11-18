"""统一数据流（数据接入步）：行情 + 账户，拼成一份快照给未来的策略消费。

只读，不下单，不碰私钥，不读 .env 内容。
- 行情：默认读 history.db 里每个市场最新一行盘口（引擎在跑时由它写入）；
  也可以传 market_source 回调（返回引擎 feed 那种 state dict 列表），供策略
  在进程内直连实时行情。
- 账户：调 quant_platform.clob_client.link_status()（只读），节流到最多 30 秒
  一次；两次之间用缓存值。没配 .env / 没装 v2 客户端时返回未链接，不崩。
- 账户快照：挂了 HistoryStore 时才落盘，最多 60 秒一次，或余额/挂单数变化时
  立刻记一行。--once 是只读探针，不写盘；--watch 会写盘。

用法：
  python -m quant_platform.data_feed --once            # 打印一份快照（JSON）
  python -m quant_platform.data_feed --watch 10        # 每 10 秒打印一份快照
  python -m quant_platform.data_feed --once --db /path/to/history.db
"""
import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ACCOUNT_MIN_INTERVAL = 30.0            # 账户实读最小间隔（秒）
ACCOUNT_SNAPSHOT_MIN_INTERVAL = 60.0   # 账户快照落盘最小间隔（秒）
ACCOUNT_LINKED_FRESH_SECONDS = 600     # 看板判“已链接”：10 分钟内有账户快照


def market_row_from_state(state, ts=None):
    """引擎 feed 的 state dict -> 快照行。
    state: market_id/question/a_bid/a_ask/b_bid/b_ask/fee_rate（a=YES，b=NO）。
    buy_both = 两边都买的成本（a_ask + b_ask）；sell_both = 两边都卖的收入（a_bid + b_bid）。
    spread = sell_both - buy_both（通常为负，绝对值是双边往返成本；>0 表示盘口交叉）。
    缺 bid/ask 时相关字段为 None，不编数。"""
    ts = int(ts or time.time())
    a_bid, a_ask = state.get("a_bid"), state.get("a_ask")
    b_bid, b_ask = state.get("b_bid"), state.get("b_ask")
    buy_both = round(a_ask + b_ask, 4) if (a_ask is not None and b_ask is not None) else None
    sell_both = round(a_bid + b_bid, 4) if (a_bid is not None and b_bid is not None) else None
    spread = round(sell_both - buy_both, 4) if (buy_both is not None and sell_both is not None) else None
    return {
        "market_id": state.get("market_id"),
        "question": state.get("question"),
        "yes_bid": a_bid, "yes_ask": a_ask,
        "no_bid": b_bid, "no_ask": b_ask,
        "buy_both": buy_both, "sell_both": sell_both, "spread": spread,
        "fee_rate": state.get("fee_rate"),
        "as_of": ts,
    }


def latest_market_rows(db_path):
    """只读读 history.db：每个市场最新一行盘口 -> 快照行列表。
    文件不存在 / 表不存在 → 空列表，绝不建文件、绝不抛异常。"""
    if not db_path or not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT b.ts, b.market_id, b.question, b.a_bid, b.a_ask, "
                "b.b_bid, b.b_ask, b.fee_rate FROM books b JOIN "
                "(SELECT market_id, MAX(ts) m FROM books GROUP BY market_id) x "
                "ON b.market_id = x.market_id AND b.ts = x.m "
                "ORDER BY b.market_id").fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    out = []
    for r in rows:
        out.append(market_row_from_state(
            {"market_id": r[1], "question": r[2], "a_bid": r[3], "a_ask": r[4],
             "b_bid": r[5], "b_ask": r[6], "fee_rate": r[7]}, ts=r[0]))
    return out


def read_account(account_fn=None):
    """调一次 link_status()（或注入的 account_fn），绝不抛异常。
    只透传它返回的字段（已掩码地址、余额、挂单数、报错），不读 .env、不打印凭证。"""
    fn = account_fn
    if fn is None:
        try:
            from quant_platform.clob_client import link_status
            fn = link_status
        except Exception as e:
            return {"linked": False, "wallet_address": None, "pusd_balance": None,
                    "open_orders": None, "error": f"钱包模块导入失败：{str(e)[:120]}",
                    "as_of": int(time.time())}
    try:
        st = fn() or {}
    except Exception as e:
        return {"linked": False, "wallet_address": None, "pusd_balance": None,
                "open_orders": None, "error": f"账户读取失败：{str(e)[:120]}",
                "as_of": int(time.time())}
    if not isinstance(st, dict):
        st = {}
    return {
        "linked": bool(st.get("linked")),
        "wallet_address": st.get("wallet_address"),
        "pusd_balance": st.get("balance"),
        "open_orders": st.get("open_orders"),
        "error": st.get("error"),
        "as_of": int(time.time()),
    }


class DataFeed:
    """统一数据流。
    market_source：可调用对象，返回 state dict 列表（引擎 feed 格式）；不传则读
      history.db 最新盘口。
    account_fn：可调用对象，返回 link_status() 格式的 dict；不传则用真实 link_status()。
    store：HistoryStore 或 None；有 store 时才落盘账户快照。"""

    def __init__(self, db_path=None, market_source=None, account_fn=None, store=None,
                 account_min_interval=ACCOUNT_MIN_INTERVAL,
                 account_snapshot_min_interval=ACCOUNT_SNAPSHOT_MIN_INTERVAL):
        self.db_path = db_path or os.path.join(ROOT, "history.db")
        self.market_source = market_source
        self.account_fn = account_fn
        self.store = store
        self.account_min_interval = account_min_interval
        self.account_snapshot_min_interval = account_snapshot_min_interval
        self._account_cache = None
        self._account_cache_ts = 0.0
        self._last_snapshot_ts = 0.0
        self._last_snapshot_values = (None, None)  # (pusd_balance, open_orders)

    # ---- 行情 ----
    def market_rows(self):
        if self.market_source is not None:
            try:
                states = self.market_source() or []
            except Exception:
                return []
            now = int(time.time())
            return [market_row_from_state(s, ts=now)
                    for s in states if isinstance(s, dict)]
        return latest_market_rows(self.db_path)

    # ---- 账户（节流，最多 30 秒实读一次）----
    def account(self, force=False):
        now = time.time()
        if (not force and self._account_cache is not None
                and now - self._account_cache_ts < self.account_min_interval):
            return self._account_cache
        acc = read_account(self.account_fn)
        self._account_cache = acc
        self._account_cache_ts = now
        return acc

    @property
    def last_account_refresh_ts(self):
        return int(self._account_cache_ts) if self._account_cache is not None else None

    # ---- 账户快照落盘 ----
    def _maybe_record_account(self, acc):
        """落盘规则：有 store 且账户已联通时才记；最多 60 秒一次，或余额/挂单数
        变了立刻记。首次调用时先用库里最新一行做基准，重启后不重复记。"""
        if self.store is None or not acc.get("linked"):
            return False
        if self._last_snapshot_ts == 0:
            try:
                row = self.store.latest_account_snapshot()
            except Exception:
                row = None
            if row:
                self._last_snapshot_ts = row[0]
                self._last_snapshot_values = (row[1], row[2])
        now = int(time.time())
        vals = (acc.get("pusd_balance"), acc.get("open_orders"))
        changed = vals != self._last_snapshot_values
        due = now - self._last_snapshot_ts >= self.account_snapshot_min_interval
        if not changed and not due:
            return False
        try:
            self.store.write_account_snapshot(
                now, acc.get("pusd_balance"), acc.get("open_orders"))
        except Exception:
            return False
        self._last_snapshot_ts = now
        self._last_snapshot_values = vals
        return True

    def build_snapshot(self, record=True):
        """拼一份快照：行情 + 账户 + 健康信息。绝不抛异常。"""
        now = int(time.time())
        rows = self.market_rows()
        acc = self.account()
        if record:
            self._maybe_record_account(acc)
        last_market_ts = max((r["as_of"] for r in rows), default=None)
        return {
            "ts": now,
            "markets": rows,
            "account": acc,
            "health": {
                "market_count": len(rows),
                "last_market_ts": last_market_ts,
                "last_account_refresh_ts": self.last_account_refresh_ts,
                "account_linked": bool(acc.get("linked")),
            },
        }


def main():
    ap = argparse.ArgumentParser(description="统一数据流：行情 + 账户快照（只读，不下单）")
    ap.add_argument("--once", action="store_true", help="打印一份快照（只读探针，不写盘）")
    ap.add_argument("--watch", type=float, default=0, help="每 N 秒打印一份快照（会按规则落盘账户快照）")
    ap.add_argument("--db", default=None, help="history.db 路径（默认平台根目录）")
    args = ap.parse_args()
    if not args.once and not args.watch:
        ap.error("--once 或 --watch N 二选一")
    db_path = args.db or os.path.join(ROOT, "history.db")
    if args.once:
        feed = DataFeed(db_path=db_path)  # store=None → 不写盘
        print(json.dumps(feed.build_snapshot(record=False), ensure_ascii=False))
        return 0
    # --watch：挂一个 HistoryStore 落盘账户快照；DB 不存在时新建（和引擎一致）
    from quant_platform.storage import HistoryStore
    store = HistoryStore(db_path)
    feed = DataFeed(db_path=db_path, store=store)
    try:
        while True:
            print(json.dumps(feed.build_snapshot(record=True), ensure_ascii=False), flush=True)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            store.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""历史数据存储：SQLite（标准库 sqlite3），只存本地 history.db。
只存两样：盘口快照（books）+ 公开成交（trades）。
Polymarket 没有资金费率（那是永续合约的概念），不建资金费率表；
价格历史可由 books 表的中价（(bid+ask)/2）随时间推导。
"""
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    ts INTEGER, market_id TEXT, question TEXT,
    a_bid REAL, a_ask REAL, b_bid REAL, b_ask REAL, fee_rate REAL
);
CREATE INDEX IF NOT EXISTS idx_books_market_ts ON books (market_id, ts);
CREATE TABLE IF NOT EXISTS trades (
    ts INTEGER, market_id TEXT, price REAL, size REAL, side TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_market_ts ON trades (market_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_dedupe ON trades (market_id, ts, price, size);
CREATE TABLE IF NOT EXISTS account_snapshots (
    ts INTEGER, pusd_balance REAL, open_orders INTEGER
);
CREATE INDEX IF NOT EXISTS idx_account_snapshots_ts ON account_snapshots (ts);
"""


class HistoryStore:
    def __init__(self, path=None):
        if path is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "history.db")
        self.path = os.path.abspath(path)
        # check_same_thread=False：盘口写来自轮询/WS 线程，成交写来自主线程，
        # 写操作统一由 self._lock 串行化。
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.Lock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._writes_since_commit = 0
        self._last_commit = time.time()

    def write_book(self, state, ts=None):
        """state: 引擎的盘口 state dict（market_id/question/a_bid/a_ask/b_bid/b_ask/fee_rate）。
        缺 bid/ask（None）存 NULL，不崩。"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO books (ts, market_id, question, a_bid, a_ask, b_bid, b_ask, fee_rate)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (int(ts or time.time()),
                 state.get("market_id"), state.get("question"),
                 state.get("a_bid"), state.get("a_ask"),
                 state.get("b_bid"), state.get("b_ask"), state.get("fee_rate")))
            self._maybe_commit()

    def write_trade(self, market_id, ts, price, size, side):
        """公开成交一行；(market_id, ts, price, size) 重复时忽略（轮询重叠去重）。"""
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO trades (ts, market_id, price, size, side)"
                " VALUES (?,?,?,?,?)",
                (int(ts), market_id, float(price), float(size), side))
            self._maybe_commit()

    def _maybe_commit(self):
        self._writes_since_commit += 1
        now = time.time()
        if self._writes_since_commit >= 50 or now - self._last_commit >= 10:
            self.conn.commit()
            self._writes_since_commit = 0
            self._last_commit = now

    def flush(self):
        with self._lock:
            self.conn.commit()
            self._writes_since_commit = 0
            self._last_commit = time.time()

    def write_account_snapshot(self, ts, pusd_balance, open_orders):
        """记一行账户快照（ts, pUSD 余额, 未成交挂单数）。调用方负责节流
        （≤60 秒一次，或余额/挂单数变化时）。余额/挂单未知时存 NULL。"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO account_snapshots (ts, pusd_balance, open_orders)"
                " VALUES (?,?,?)",
                (int(ts),
                 None if pusd_balance is None else float(pusd_balance),
                 None if open_orders is None else int(open_orders)))
            self._maybe_commit()

    def latest_account_snapshot(self):
        """最新一行账户快照 (ts, pusd_balance, open_orders)，没有返回 None。"""
        row = self.conn.execute(
            "SELECT ts, pusd_balance, open_orders FROM account_snapshots"
            " ORDER BY ts DESC LIMIT 1").fetchone()
        return row

    def counts(self):
        books = self.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        trades = self.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        return {"books": books, "trades": trades}

    def latest_books(self):
        """每个市场最新一行盘口。列顺序：ts, market_id, question, a_bid, a_ask, b_bid, b_ask, fee_rate"""
        return self.conn.execute(
            "SELECT b.* FROM books b JOIN"
            " (SELECT market_id, MAX(ts) m FROM books GROUP BY market_id) x"
            " ON b.market_id = x.market_id AND b.ts = x.m"
            " ORDER BY b.market_id").fetchall()

    def recent_books(self, market_id, limit=5):
        return self.conn.execute(
            "SELECT * FROM books WHERE market_id = ? ORDER BY ts DESC LIMIT ?",
            (market_id, limit)).fetchall()

    def close(self):
        with self._lock:
            self.conn.commit()
            self.conn.close()
        self._writes_since_commit = 0

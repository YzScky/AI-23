"""引擎：把行情、策略、账本、风控串起来。纸面模式默认；实盘未接好，强行开会直接报错停住。"""
import os
import time

from .feed import MarketFeed, discover_markets
from .ledger import PaperLedger
from .risk import RiskManager
from .strategy import STRATEGIES


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.live = bool(cfg.get("live_trading")) and cfg.get("mode") == "live"
        if self.live:
            raise RuntimeError("实盘执行未接好：live_trading 只能在钱包和 API 凭证配好、参数确认后手动开启")
        self.ledger = PaperLedger(cfg["risk"]["total_capital"])
        self.risk = RiskManager(cfg)
        strat_name = cfg["strategy"]["name"]
        # 策略参数从 config 的 strategy.<name> 节透传（scalp_dip 的时点表全在这里）
        self.strategy = STRATEGIES[strat_name](cfg["strategy"].get(strat_name))
        self.best_buy = (None, None)
        self.best_sell = (None, None)
        self.log_path = os.path.join(os.path.dirname(__file__), "..", "platform.log")
        self.start_ts = time.time()
        self.store = None
        self._last_book_write = {}
        self._trade_poll_warned = False

    def _log(self, msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(self.log_path, "a") as f:
            f.write(line + "\n")

    def on_book(self, market_id, state, ts):
        a_ask, b_ask = state["a_ask"], state["b_ask"]
        a_bid, b_bid = state["a_bid"], state["b_bid"]
        if a_ask is not None and b_ask is not None:
            s = a_ask + b_ask
            if self.best_buy[0] is None or s < self.best_buy[0]:
                self.best_buy = (round(s, 4), state["question"])
        if a_bid is not None and b_bid is not None:
            s = a_bid + b_bid
            if self.best_sell[0] is None or s > self.best_sell[0]:
                self.best_sell = (round(s, 4), state["question"])
        # 策略自管执行（如 scalp_dip）用 ctx["log"] 写 [SCALP] 日志进 platform.log：
        # 看板启动的 run.py stdout 是 DEVNULL，只 print 会丢日志。
        orders = self.strategy.on_book(state, {"ledger": self.ledger, "risk": self.risk,
                                               "log": self._log, "ts": ts})
        for o in orders:
            ok, reason = self.risk.check(self.ledger, o.size)
            if not ok:
                self._log(f"[BLOCKED] {o.market_id} {o.direction} {reason}")
                continue
            if self.live:
                raise RuntimeError("live path not wired")
            pnl = self.ledger.execute_pair(
                o.market_id, o.direction, o.pair_sum, o.size)
            self._log(f"[PAPER] {o.direction} {o.market_id} sum={o.pair_sum} "
                      f"size={o.size} pnl={pnl:.2f}")
        # 历史存盘：每个市场最多每 poll 间隔写一行（WS 模式下也节流，不写爆磁盘）
        if self.store is not None:
            gap = self.cfg.get("poll_interval") or 5
            last = self._last_book_write.get(market_id, 0)
            if ts - last >= gap:
                self._last_book_write[market_id] = ts
                try:
                    self.store.write_book(state, ts)
                except Exception as e:
                    self._log(f"[HIST] write_book failed: {e}")

    def _poll_trades(self, markets):
        """每 60 秒拉一次各市场的公开成交，写 trades 表。
        注意：CLOB /trades 要鉴权，公开成交走 data-api v2（按 condition_id 查，无需鉴权）。
        任何异常都静默跳过（只记一次日志），不拖慢主循环。"""
        import requests
        for m in markets:
            cond = m.get("condition_id")
            if not cond:
                continue
            try:
                r = requests.get("https://data-api.polymarket.com/v2/trades",
                                 params={"condition": cond, "limit": 20}, timeout=10)
                r.raise_for_status()
                for t in (r.json().get("data") or []):
                    self.store.write_trade(m["market_id"], t["timestamp"],
                                           t["price"], t["size"], t.get("side"))
            except Exception as e:
                if not self._trade_poll_warned:
                    self._trade_poll_warned = True
                    self._log(f"[HIST] trades poll failed (will keep trying silently): {e}")
                continue

    def run(self, duration):
        mk = self.cfg["markets"]
        # 策略自带 tags（如 scalp_dip 只做 nba）时用策略的，否则用 config 的
        tags = getattr(self.strategy, "tags", None) or mk["tags"]
        markets = discover_markets(tags, mk["min_liquidity"], mk["max_markets"])
        self._log(f"[BOOT] mode={self.cfg['mode']} live={self.live} "
                  f"strategy={self.strategy.name} markets={len(markets)}")
        if self.cfg.get("history", True):
            from .storage import HistoryStore
            self.store = HistoryStore()
            self._log(f"[BOOT] history on: {self.store.path}")
        feed = MarketFeed(markets, self.on_book)
        feed.seed()
        poll = self.cfg.get("poll_interval") or 0
        if poll:
            self._log(f"[BOOT] books seeded, REST poll mode every {poll}s (WS blocked on this network) ...")
            feed.start_poll(poll)
        else:
            self._log("[BOOT] books seeded, subscribing WS ...")
            feed.start()
        end = time.time() + duration
        last_hb = 0
        last_trade_poll = 0
        try:
            while time.time() < end:
                time.sleep(1)
                if self.store is not None and time.time() - last_trade_poll >= 60:
                    last_trade_poll = time.time()
                    self._poll_trades(markets)
                if time.time() - last_hb >= 10:
                    last_hb = time.time()
                    snap = self.ledger.snapshot()
                    ups = feed.updates / max(1, time.time() - self.start_ts)
                    self._log(f"[HB] updates={feed.updates} ({ups:.0f}/s) "
                              f"best_buy={self.best_buy} best_sell={self.best_sell} "
                              f"cash={snap['cash']} daily_loss={snap['daily_loss']} "
                              f"trades={snap['trades']}")
        finally:
            feed.stop()
            if self.store is not None:
                self.store.close()
                self.store = None
        snap = self.ledger.snapshot()
        self._log(f"[DONE] ran {duration}s updates={feed.updates} "
                  f"best_buy={self.best_buy} best_sell={self.best_sell} "
                  f"cash={snap['cash']} trades={snap['trades']}")
        return {"updates": feed.updates, "best_buy": self.best_buy,
                "best_sell": self.best_sell, "ledger": snap}

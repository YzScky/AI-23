"""纸面账本：虚拟资金、虚拟成交、日亏统计。实盘账本以后接同一接口。"""
import time


class PaperLedger:
    def __init__(self, capital):
        self.start_cash = float(capital)
        self.cash = float(capital)
        self.trades = []
        self.daily_loss = 0.0

    def execute_pair(self, market_id, direction, pair_sum, size, ts=None):
        """纸面成交一对双边单，立即记锁定利润（真实资金要等结算才到账，纸面会高估资金周转）。
        BUY_BOTH: 付 size，结算收 size/pair_sum；SELL_BOTH: 收 size，结算付 size/pair_sum。"""
        ts = ts or time.time()
        if direction == "BUY_BOTH":
            locked = size / pair_sum - size
        else:
            locked = size - size / pair_sum
        self.cash += locked
        self.trades.append({"ts": ts, "market": market_id, "direction": direction,
                            "sum": pair_sum, "size": size, "locked_pnl": round(locked, 4)})
        if locked < 0:
            self.daily_loss += -locked
        return locked

    def record_loss(self, amount, market_id, note=""):
        self.daily_loss += amount
        self.cash -= amount
        self.trades.append({"ts": time.time(), "market": market_id, "direction": "LOSS",
                            "sum": None, "size": amount, "locked_pnl": round(-amount, 4), "note": note})

    def snapshot(self):
        return {"cash": round(self.cash, 2), "daily_loss": round(self.daily_loss, 2),
                "trades": len(self.trades)}

"""纸面账本：虚拟资金、虚拟成交、日亏统计。实盘账本以后接同一接口。"""
import time


class PaperLedger:
    def __init__(self, capital):
        self.start_cash = float(capital)
        self.cash = float(capital)
        self.trades = []
        self.daily_loss = 0.0
        self.legs = {}        # leg_id -> 单腿持仓（策略自管执行用）
        self._next_leg = 1

    def open_leg(self, market_id, side, price, size, ts=None):
        """单腿开仓：付 price*size。返回 leg_id。"""
        ts = ts or time.time()
        cost = round(float(price) * float(size), 4)
        leg_id = self._next_leg
        self._next_leg += 1
        self.cash -= cost
        self.legs[leg_id] = {"id": leg_id, "market": market_id, "side": side,
                             "price": float(price), "size": float(size),
                             "cost": cost, "ts": ts}
        self.trades.append({"ts": ts, "market": market_id, "direction": "OPEN_LEG",
                            "side": side, "sum": float(price), "size": float(size),
                            "locked_pnl": round(-cost, 4), "leg_id": leg_id})
        return leg_id

    def _close_leg_common(self, leg_id, receipt, fee, ts, direction):
        leg = self.legs.pop(leg_id, None)
        if leg is None:
            return None
        pnl = round(receipt - fee - leg["cost"], 4)
        self.cash += round(receipt - fee, 4)
        self.trades.append({"ts": ts, "market": leg["market"], "direction": direction,
                            "side": leg["side"], "sum": leg["price"],
                            "size": leg["size"], "locked_pnl": pnl, "leg_id": leg_id})
        if pnl < 0:
            self.daily_loss += -pnl
        return pnl

    def close_leg(self, leg_id, price, fee, ts=None):
        """单腿平仓：按 price 卖出，付 fee。返回已实现盈亏（None 表示腿不存在）。"""
        ts = ts or time.time()
        leg = self.legs.get(leg_id)
        if leg is None:
            return None
        return self._close_leg_common(leg_id, round(float(price) * leg["size"], 4),
                                      round(float(fee), 4), ts, "CLOSE_LEG")

    def lock_pair(self, market_id, price, size, ts=None):
        """双边同时成交锁利：两边各付 price*size，结算必收 size（赢的那边 1.00）。
        盈亏 = size - 2*price*size = size*(1-2*price)，立即记账。"""
        ts = ts or time.time()
        pnl = round(float(size) * (1.0 - 2.0 * float(price)), 4)
        self.cash += pnl
        self.trades.append({"ts": ts, "market": market_id, "direction": "LOCK_PAIR",
                            "sum": float(price), "size": float(size),
                            "locked_pnl": pnl})
        if pnl < 0:
            self.daily_loss += -pnl
        return pnl

    def settle_leg(self, leg_id, won, ts=None):
        """终场结算：赢收 size*1.00，输收 0。返回已实现盈亏。"""
        ts = ts or time.time()
        leg = self.legs.get(leg_id)
        if leg is None:
            return None
        return self._close_leg_common(leg_id, leg["size"] if won else 0.0,
                                      0.0, ts, "SETTLE_LEG")

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
                "trades": len(self.trades), "open_legs": len(self.legs)}

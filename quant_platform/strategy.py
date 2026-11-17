"""策略接口：所有策略都按这个写 —— 输入盘口状态，输出订单列表。
新策略：继承 Strategy，实现 on_book，名字注册进 STRATEGIES。"""
from dataclasses import dataclass


@dataclass
class Order:
    market_id: str
    direction: str  # BUY_BOTH / SELL_BOTH
    pair_sum: float
    size: float


class Strategy:
    name = "base"

    def on_book(self, state, ctx):
        """state: {market_id, question, a_bid, a_ask, b_bid, b_ask, fee_rate}
        ctx: {"ledger": PaperLedger, "risk": RiskManager}
        返回 [Order, ...]，无信号返回 []。"""
        return []


class ObserveOnlyStrategy(Strategy):
    """占位策略：只观察，不下单。策略以后再定，接上面这个接口就行。"""
    name = "observe_only"

    def on_book(self, state, ctx):
        return []


STRATEGIES = {"observe_only": ObserveOnlyStrategy}

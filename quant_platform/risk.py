"""风控层：独立于策略。单笔上限、日亏熔断、手动 kill switch。"""


class RiskManager:
    def __init__(self, cfg):
        r = cfg["risk"]
        self.total_capital = r["total_capital"]
        self.max_pair_size = r["max_pair_size"]
        self.daily_loss_cap = r["daily_loss_cap"]
        self.stopped = False

    def check(self, ledger, size):
        if self.stopped:
            return False, "killed"
        if size > self.max_pair_size:
            return False, f"size {size} > max {self.max_pair_size}"
        if ledger.daily_loss >= self.daily_loss_cap:
            self.stopped = True
            return False, "daily loss cap hit"
        return True, "ok"

    def kill(self, reason):
        self.stopped = True
        print(f"[RISK] KILL SWITCH: {reason}")

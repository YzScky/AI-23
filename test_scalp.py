"""scalp_dip（篮球低吸）离线单测：假时钟 + 合成盘口，不碰网络。
跑：python3 test_scalp.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quant_platform.ledger import PaperLedger
from quant_platform.risk import RiskManager
from quant_platform.strategy import DEFAULT_SCALP_DIP, ScalpDipStrategy

T0 = 1793000000  # 固定开球时间（epoch 秒），测试里时间全由 ctx["ts"] 给，不用真时间
Q = "Suns vs. Rockets"


class FakeClock:
    """假 ESPN 时钟：snap 为快照 dict 或 None（None = 时钟缺失）。"""

    def __init__(self, snap=None):
        self.snap = snap
        self.calls = []

    def get_game(self, a, b, date_str):
        self.calls.append((a, b, date_str))
        return dict(self.snap) if self.snap else None


def pre_game():
    return {"state": "pre", "period": 0, "clock_s": None,
            "home_abbr": "HOU", "away_abbr": "PHX",
            "home_score": 0, "away_score": 0, "winner": None}


def in_game(period, clock_s):
    return {"state": "in", "period": period, "clock_s": clock_s,
            "home_abbr": "HOU", "away_abbr": "PHX",
            "home_score": 50, "away_score": 48, "winner": None}


def final(winner):
    return {"state": "final", "period": 4, "clock_s": 0.0,
            "home_abbr": "HOU", "away_abbr": "PHX",
            "home_score": 110 if winner == "HOU" else 100,
            "away_score": 100 if winner == "HOU" else 110,
            "winner": winner}


def make(clock=None, capital=1000.0, daily_loss_cap=100.0):
    strat = ScalpDipStrategy(params=dict(DEFAULT_SCALP_DIP),
                             clock=clock if clock is not None else FakeClock(pre_game()))
    ledger = PaperLedger(capital)
    risk = RiskManager({"risk": {"total_capital": capital, "max_pair_size": 200.0,
                                 "daily_loss_cap": daily_loss_cap}})
    logs = []
    return strat, ledger, risk, logs


def book(strat, ledger, risk, logs, ts, a_bid=None, a_ask=None, b_bid=None,
         b_ask=None, start_ts=T0, question=Q, fee_rate=0.05, tag="nba"):
    state = {"market_id": "m1", "question": question, "a_bid": a_bid, "a_ask": a_ask,
             "b_bid": b_bid, "b_ask": b_ask, "fee_rate": fee_rate, "tag": tag,
             "start_ts": start_ts}
    ctx = {"ledger": ledger, "risk": risk, "log": lambda m: logs.append(m), "ts": ts}
    out = strat.on_book(state, ctx)
    assert out == [], f"on_book 必须永远返回 []，得到 {out}"
    return out


def has(logs, *words):
    return any(all(w in line for w in words) for line in logs)


def test_pregame_fill_then_target():
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 30, a_bid=0.27, a_ask=0.28, b_bid=0.64, b_ask=0.65)
    assert len(ledger.legs) == 1, "赛前应成交一腿"
    assert ledger.cash == 991.0, f"开仓扣 9.00，cash 应为 991.0，实际 {ledger.cash}"
    assert has(logs, "赛前", "成交")
    # 赛中摸到 0.40 落袋：付 9.00 收 12.00，盈亏 +3.00，cash 1000-9+12=1003
    strat.clock.snap = in_game(2, 600)
    book(strat, ledger, risk, logs, T0 + 3600, a_bid=0.42, a_ask=0.43, b_bid=0.58, b_ask=0.59)
    assert len(ledger.legs) == 0
    assert ledger.cash == 1003.0, f"0.30 买 0.40 卖 +3.00，cash 应为 1003.0，实际 {ledger.cash}"
    assert ledger.trades[-1]["locked_pnl"] == 3.0
    assert has(logs, "落袋")


def test_pregame_no_fill_cancel():
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 30, a_bid=0.34, a_ask=0.35, b_bid=0.69, b_ask=0.70)
    assert len(ledger.legs) == 0
    strat.clock.snap = in_game(1, 700)
    book(strat, ledger, risk, logs, T0 + 61, a_bid=0.34, a_ask=0.35, b_bid=0.69, b_ask=0.70)
    assert len(ledger.legs) == 0, "T0+60s 无成交应全撤，不开仓"
    assert has(logs, "全撤")
    book(strat, ledger, risk, logs, T0 + 1200, a_bid=0.34, a_ask=0.35, b_bid=0.69, b_ask=0.70)
    assert len(ledger.legs) == 0


def test_ingame_dip_then_q4_exit():
    strat, ledger, risk, logs = make(clock=FakeClock(in_game(2, 600)))
    # 赛中初见：A 队卖价 0.29，低吸成交（弱队价 0.29 在筛选区间内）
    book(strat, ledger, risk, logs, T0 + 3600, a_bid=0.28, a_ask=0.29, b_bid=0.70, b_ask=0.71)
    assert len(ledger.legs) == 1 and ledger.cash == 991.0
    assert has(logs, "赛中低吸", "成交")
    # Q4 剩 119 秒仍未到 0.40：市价 0.25 平掉，taker 费 0.05*30*0.25*0.75=0.28
    strat.clock.snap = in_game(4, 119)
    book(strat, ledger, risk, logs, T0 + 6600, a_bid=0.25, a_ask=0.26, b_bid=0.74, b_ask=0.75)
    assert len(ledger.legs) == 0
    # 7.50 - 0.28 - 9.00 = -1.78；cash 1000 - 9 + 7.50 - 0.28 = 998.22
    assert abs(ledger.cash - 998.22) < 0.01, f"cash 应约 998.22，实际 {ledger.cash}"
    assert abs(ledger.trades[-1]["locked_pnl"] + 1.78) < 0.01
    assert abs(ledger.daily_loss - 1.78) < 0.01
    assert has(logs, "Q4", "市价")


def test_price_stop():
    strat, ledger, risk, logs = make(clock=FakeClock(in_game(2, 600)))
    # 赛中低吸成交：A 队卖价 0.29 买入，成本 8.70
    book(strat, ledger, risk, logs, T0 + 3600, a_bid=0.28, a_ask=0.29, b_bid=0.70, b_ask=0.71)
    assert len(ledger.legs) == 1 and ledger.cash == 991.0
    # 跌到 0.10：触发止损，市价平掉；收 3.00，taker 费 0.05*30*0.10*0.90=0.14
    book(strat, ledger, risk, logs, T0 + 3900, a_bid=0.10, a_ask=0.11, b_bid=0.89, b_ask=0.90)
    assert len(ledger.legs) == 0
    # 盈亏 3.00 - 0.14 - 9.00 = -6.14（进场按策略价 0.30 记，成本 9.00）；
    # cash 991.0 + 3.00 - 0.14 = 993.86
    assert abs(ledger.trades[-1]["locked_pnl"] + 6.135) < 0.01
    assert abs(ledger.cash - 993.865) < 0.01
    assert has(logs, "止损")
    # 另一场：跌到 0.11 不触发止损，之后涨回 0.40 正常落袋
    strat, ledger, risk, logs = make(clock=FakeClock(in_game(2, 600)))
    book(strat, ledger, risk, logs, T0 + 3600, a_bid=0.28, a_ask=0.29, b_bid=0.70, b_ask=0.71)
    book(strat, ledger, risk, logs, T0 + 3900, a_bid=0.11, a_ask=0.12, b_bid=0.88, b_ask=0.89)
    assert len(ledger.legs) == 1, "0.11 高于止损价 0.10，不应平掉"
    book(strat, ledger, risk, logs, T0 + 4200, a_bid=0.40, a_ask=0.41, b_bid=0.59, b_ask=0.60)
    assert len(ledger.legs) == 0
    # 落袋收 12.00，成本 9.00，盈亏 +3.00
    assert abs(ledger.trades[-1]["locked_pnl"] - 3.00) < 0.01
    assert has(logs, "落袋")


def test_lock_pair():
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 30, a_bid=0.27, a_ask=0.28, b_bid=0.28, b_ask=0.29)
    assert len(ledger.legs) == 0
    assert ledger.cash == 1012.0, f"双边锁利 +12.00，cash 应为 1012.0，实际 {ledger.cash}"
    assert ledger.trades[-1]["direction"] == "LOCK_PAIR"
    assert ledger.trades[-1]["locked_pnl"] == 12.0
    assert has(logs, "锁利")


def test_circuit_breaker():
    strat, ledger, risk, logs = make(daily_loss_cap=100.0)
    ledger.daily_loss = 100.0  # 已触日亏上限
    book(strat, ledger, risk, logs, T0 - 30, a_bid=0.27, a_ask=0.28, b_bid=0.64, b_ask=0.65)
    assert len(ledger.legs) == 0, "熔断后不应开仓"
    assert ledger.cash == 1000.0
    assert has(logs, "熔断拒单")


def test_clock_missing_no_open():
    strat, ledger, risk, logs = make(clock=FakeClock(None))
    book(strat, ledger, risk, logs, T0 - 30, a_bid=0.27, a_ask=0.28, b_bid=0.64, b_ask=0.65)
    assert len(ledger.legs) == 0, "时钟缺失不应新开仓"
    assert has(logs, "时钟缺失")


def test_final_settlement():
    # 赢：终场结算收 30，盈亏 +21
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 30, a_bid=0.27, a_ask=0.28, b_bid=0.64, b_ask=0.65)
    strat.clock.snap = final("PHX")
    book(strat, ledger, risk, logs, T0 + 9000, a_bid=0.99, a_ask=1.0, b_bid=0.0, b_ask=0.01)
    assert len(ledger.legs) == 0
    assert ledger.cash == 1021.0, f"赢的腿结算 +21.00，cash 应为 1021.0，实际 {ledger.cash}"
    assert has(logs, "终场结算", "赢")
    # 输：终场结算收 0，盈亏 -9
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 30, a_bid=0.27, a_ask=0.28, b_bid=0.64, b_ask=0.65)
    strat.clock.snap = final("HOU")
    book(strat, ledger, risk, logs, T0 + 9000, a_bid=0.0, a_ask=0.01, b_bid=0.99, b_ask=1.0)
    assert ledger.cash == 991.0, f"输的腿结算 -9.00，cash 应为 991.0，实际 {ledger.cash}"
    assert ledger.daily_loss == 9.0
    assert has(logs, "终场结算", "输")


def test_underdog_filter():
    # 弱队 0.20：低于下限，跳过
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 30, a_bid=0.19, a_ask=0.20, b_bid=0.79, b_ask=0.80)
    assert len(ledger.legs) == 0 and has(logs, "筛选跳过")
    # 弱队 0.45：高于上限，跳过
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 30, a_bid=0.44, a_ask=0.45, b_bid=0.54, b_ask=0.55)
    assert len(ledger.legs) == 0 and has(logs, "筛选跳过")


def test_non_nba_ignored_and_t0_missing():
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 30, a_bid=0.27, a_ask=0.28, b_bid=0.64,
         b_ask=0.65, tag="crypto")
    assert len(ledger.legs) == 0, "非 nba 市场必须忽略"
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 30, a_bid=0.27, a_ask=0.28, b_bid=0.64,
         b_ask=0.65, start_ts=None)
    assert len(ledger.legs) == 0, "没有开球时间不能开仓"
    assert has(logs, "开球时间")


def test_ledger_math_exact():
    led = PaperLedger(1000.0)
    leg = led.open_leg("m", "a", 0.30, 30)
    assert led.cash == 991.0
    pnl = led.close_leg(leg, 0.40, 0.0)
    assert pnl == 3.0 and led.cash == 1003.0  # 付 9 收 12
    leg = led.open_leg("m", "b", 0.30, 30)
    pnl = led.close_leg(leg, 0.25, 0.28)
    assert abs(pnl + 1.78) < 0.01 and abs(led.cash - 1001.22) < 0.01
    assert abs(led.daily_loss - 1.78) < 0.01
    assert led.lock_pair("m", 0.30, 30) == 12.0
    assert abs(led.cash - 1013.22) < 0.01
    leg = led.open_leg("m", "a", 0.30, 30)
    assert led.settle_leg(leg, True) == 21.0
    assert abs(led.cash - 1034.22) < 0.01
    leg = led.open_leg("m", "a", 0.30, 30)
    assert led.settle_leg(leg, False) == -9.0
    assert abs(led.cash - 1025.22) < 0.01
    assert led.snapshot()["open_legs"] == 0


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed, failed = 0, []
    for t in tests:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed.append(t.__name__)
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failed.append(t.__name__)
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{passed + len(failed)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

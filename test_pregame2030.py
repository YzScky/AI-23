"""pregame_2030（赛前低吸 0.10-0.30，30U）离线单测：假时钟 + 合成盘口，不碰网络。
跑：python3 test_pregame2030.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quant_platform.ledger import PaperLedger
from quant_platform.risk import RiskManager
from quant_platform.strategy import DEFAULT_PREGAME_2030, Pregame2030Strategy

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
    strat = Pregame2030Strategy(params=dict(DEFAULT_PREGAME_2030),
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


def buy_a_025(strat, ledger, risk, logs):
    """T0-12h，A 队卖价 0.25 买入：120 份，成本 30，买入 taker 费 1.125。"""
    book(strat, ledger, risk, logs, T0 - 12 * 3600,
         a_bid=0.24, a_ask=0.25, b_bid=0.74, b_ask=0.75)
    assert len(ledger.legs) == 1, "0.25 在区间内应买入"
    assert ledger.cash == 970.0, f"开仓扣 30，cash 应为 970.0，实际 {ledger.cash}"
    assert has(logs, "赛前买入")


def test_entry_in_range_then_tp():
    strat, ledger, risk, logs = make()
    buy_a_025(strat, ledger, risk, logs)
    # 赛中摸到 0.40 落袋：收 120x0.40=48，买入费 1.125，成本 30，盈亏 +16.875
    strat.clock.snap = in_game(2, 600)
    book(strat, ledger, risk, logs, T0 + 3600, a_bid=0.42, a_ask=0.43, b_bid=0.58, b_ask=0.59)
    assert len(ledger.legs) == 0
    assert abs(ledger.cash - 1016.875) < 0.01, f"cash 应为 1016.875，实际 {ledger.cash}"
    assert abs(ledger.trades[-1]["locked_pnl"] - 16.875) < 0.01
    assert has(logs, "落袋")


def test_entry_range_edges():
    # 0.10 下限（含）：买入，300 份
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 3600, a_bid=0.09, a_ask=0.10, b_bid=0.89, b_ask=0.90)
    assert len(ledger.legs) == 1, "0.10 是下限（含），应买入"
    # 0.20 区间内：买入，150 份
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 3600, a_bid=0.19, a_ask=0.20, b_bid=0.79, b_ask=0.80)
    assert len(ledger.legs) == 1, "0.20 在区间内，应买入"
    # 0.30 上限（含）：买入，100 份
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 3600, a_bid=0.29, a_ask=0.30, b_bid=0.69, b_ask=0.70)
    assert len(ledger.legs) == 1, "0.30 是上限（含），应买入"
    # 0.09 / 0.31：不买
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 3600, a_bid=0.08, a_ask=0.09, b_bid=0.90, b_ask=0.91)
    assert len(ledger.legs) == 0, "0.09 低于下限，不应买入"
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 3600, a_bid=0.30, a_ask=0.31, b_bid=0.68, b_ask=0.69)
    assert len(ledger.legs) == 0, "0.31 高于上限，不应买入"


def test_entry_uses_ask_not_bid():
    # 买价 0.25 在区间但卖价 0.32 不在：买入按卖价付，不应买
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 3600, a_bid=0.25, a_ask=0.32, b_bid=0.67, b_ask=0.68)
    assert len(ledger.legs) == 0, "买入按卖价，卖价 0.32 不在区间不应买入"


def test_window_edges():
    # T0-24h 整：窗口起点（含），买入
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 86400, a_bid=0.24, a_ask=0.25, b_bid=0.74, b_ask=0.75)
    assert len(ledger.legs) == 1, "T0-24h 是窗口起点（含），应买入"
    # T0-24h-1s：还没到窗口，不买
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 86401, a_bid=0.24, a_ask=0.25, b_bid=0.74, b_ask=0.75)
    assert len(ledger.legs) == 0, "窗口还没开，不应买入"
    # T0 整：窗口已过，不买且标记 DONE
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0, a_bid=0.24, a_ask=0.25, b_bid=0.74, b_ask=0.75)
    assert len(ledger.legs) == 0, "开球后不买"
    assert has(logs, "已过赛前窗口")


def test_no_entry_after_tipoff():
    strat, ledger, risk, logs = make(clock=FakeClock(in_game(1, 700)))
    book(strat, ledger, risk, logs, T0 + 100, a_bid=0.24, a_ask=0.25, b_bid=0.74, b_ask=0.75)
    assert len(ledger.legs) == 0, "只在开赛前买，赛中价格再好也不买"
    assert has(logs, "已过赛前窗口")


def test_q3_start_exit():
    strat, ledger, risk, logs = make()
    buy_a_025(strat, ledger, risk, logs)
    # Q3 开始（period 3，不管时钟剩多少）仍未到 0.40：市价 0.05 平掉
    # 收 120x0.05=6.00，卖出 taker 费 0.05x120x0.05x0.95=0.285，买入费 1.125
    strat.clock.snap = in_game(3, 720)
    book(strat, ledger, risk, logs, T0 + 5400, a_bid=0.05, a_ask=0.06, b_bid=0.94, b_ask=0.95)
    assert len(ledger.legs) == 0
    # 盈亏 6.00 - 0.285 - 1.125 - 30 = -25.41；cash 970 + 6 - 1.41 = 974.59
    assert abs(ledger.trades[-1]["locked_pnl"] + 25.41) < 0.01
    assert abs(ledger.cash - 974.59) < 0.01, f"cash 应为 974.59，实际 {ledger.cash}"
    assert has(logs, "Q3 开始")


def test_q2_does_not_exit():
    strat, ledger, risk, logs = make()
    buy_a_025(strat, ledger, risk, logs)
    # Q2 剩 100 秒：还没到 Q3，不出场
    strat.clock.snap = in_game(2, 100)
    book(strat, ledger, risk, logs, T0 + 3600, a_bid=0.05, a_ask=0.06, b_bid=0.94, b_ask=0.95)
    assert len(ledger.legs) == 1, "Q2 未到 Q3，不应出场"
    # Q3 开始：出场
    strat.clock.snap = in_game(3, 720)
    book(strat, ledger, risk, logs, T0 + 5400, a_bid=0.05, a_ask=0.06, b_bid=0.94, b_ask=0.95)
    assert len(ledger.legs) == 0
    assert has(logs, "Q3 开始")


def test_q4_fallback_exit():
    strat, ledger, risk, logs = make()
    buy_a_025(strat, ledger, risk, logs)
    # 时钟跳过 Q3 直接到 Q4（漏轮询）：Q4 开始兜底出场
    strat.clock.snap = in_game(4, 700)
    book(strat, ledger, risk, logs, T0 + 6600, a_bid=0.05, a_ask=0.06, b_bid=0.94, b_ask=0.95)
    assert len(ledger.legs) == 0
    assert abs(ledger.trades[-1]["locked_pnl"] + 25.41) < 0.01
    assert has(logs, "Q4 开始", "漏过")


def test_q3_start_exit_waits_for_bid():
    strat, ledger, risk, logs = make()
    buy_a_025(strat, ledger, risk, logs)
    strat.clock.snap = in_game(3, 720)
    book(strat, ledger, risk, logs, T0 + 5400, a_bid=None, a_ask=0.06, b_bid=0.94, b_ask=0.95)
    assert len(ledger.legs) == 1, "Q3 开始但无买价，应等下一 tick，不应丢腿"
    assert has(logs, "无买价")
    book(strat, ledger, risk, logs, T0 + 5405, a_bid=0.05, a_ask=0.06, b_bid=0.94, b_ask=0.95)
    assert len(ledger.legs) == 0


def test_clock_missing_blocks_entry_but_not_tp():
    # 时钟缺失：这一下不买，继续等窗口
    strat, ledger, risk, logs = make(clock=FakeClock(None))
    book(strat, ledger, risk, logs, T0 - 12 * 3600,
         a_bid=0.24, a_ask=0.25, b_bid=0.74, b_ask=0.75)
    assert len(ledger.legs) == 0, "时钟缺失不应新开仓"
    assert has(logs, "时钟缺失")
    # 时钟恢复：同一窗口内再看到就买
    strat.clock.snap = pre_game()
    book(strat, ledger, risk, logs, T0 - 11 * 3600,
         a_bid=0.24, a_ask=0.25, b_bid=0.74, b_ask=0.75)
    assert len(ledger.legs) == 1
    # 持仓后时钟又丢：0.40 落袋只看价格，照样走
    strat.clock.snap = None
    book(strat, ledger, risk, logs, T0 + 3600, a_bid=0.42, a_ask=0.43, b_bid=0.58, b_ask=0.59)
    assert len(ledger.legs) == 0
    assert abs(ledger.trades[-1]["locked_pnl"] - 16.875) < 0.01
    assert has(logs, "落袋")


def test_final_settlement():
    # 赢：终场结算收 120x1.00，买入费 1.125，盈亏 +88.875
    strat, ledger, risk, logs = make()
    buy_a_025(strat, ledger, risk, logs)
    strat.clock.snap = final("PHX")
    book(strat, ledger, risk, logs, T0 + 9000, a_bid=0.99, a_ask=1.0, b_bid=0.0, b_ask=0.01)
    assert len(ledger.legs) == 0
    assert abs(ledger.cash - 1088.875) < 0.01, f"cash 应为 1088.875，实际 {ledger.cash}"
    assert abs(ledger.trades[-1]["locked_pnl"] - 88.875) < 0.01
    assert has(logs, "终场结算", "赢")
    # 输：终场结算收 0，盈亏 -31.125（含买入费）
    strat, ledger, risk, logs = make()
    buy_a_025(strat, ledger, risk, logs)
    strat.clock.snap = final("HOU")
    book(strat, ledger, risk, logs, T0 + 9000, a_bid=0.0, a_ask=0.01, b_bid=0.99, b_ask=1.0)
    assert abs(ledger.cash - 968.875) < 0.01, f"cash 应为 968.875，实际 {ledger.cash}"
    assert abs(ledger.trades[-1]["locked_pnl"] + 31.125) < 0.01
    assert abs(ledger.daily_loss - 31.125) < 0.01
    assert has(logs, "终场结算", "输")


def test_both_sides_in_range_buys_a_first():
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 3600, a_bid=0.24, a_ask=0.25, b_bid=0.27, b_ask=0.28)
    assert len(ledger.legs) == 1
    leg = next(iter(ledger.legs.values()))
    assert leg["side"] == "a", "两边都在区间时先买 A"
    assert has(logs, "两边都在区间")


def test_circuit_breaker():
    strat, ledger, risk, logs = make(daily_loss_cap=100.0)
    ledger.daily_loss = 100.0  # 已触日亏上限
    book(strat, ledger, risk, logs, T0 - 3600, a_bid=0.24, a_ask=0.25, b_bid=0.74, b_ask=0.75)
    assert len(ledger.legs) == 0, "熔断后不应开仓"
    assert ledger.cash == 1000.0
    assert has(logs, "熔断拒单")


def test_non_nba_ignored_and_t0_missing():
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 3600, a_bid=0.24, a_ask=0.25, b_bid=0.74,
         b_ask=0.75, tag="crypto")
    assert len(ledger.legs) == 0, "非 nba 市场必须忽略"
    strat, ledger, risk, logs = make()
    book(strat, ledger, risk, logs, T0 - 3600, a_bid=0.24, a_ask=0.25, b_bid=0.74,
         b_ask=0.75, start_ts=None)
    assert len(ledger.legs) == 0, "没有开球时间不能开仓"
    assert has(logs, "开球时间")


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

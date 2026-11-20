"""回测器离线单测：合成两场假比赛，不碰网络。
跑真实的 ScalpDipStrategy + ReplayClock + PaperLedger，验证重放链路本身。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CONFIG
from quant_platform.ledger import PaperLedger
from quant_platform.risk import RiskManager
from quant_platform.strategy import ScalpDipStrategy
from backtest_nba import ReplayClock, replay_game

T0 = 1795136400  # 2026-11-20T01:00:00Z（固定值，测试用；美东 11-19 20:00，et_date "20261119"）


def _plays(t0):
    """合成时钟线：墙钟 2 小时打完 48 分钟比赛时钟（线性映射，仅供测试）。
    每 60 秒一个 play；最后一条 End of Game。"""
    plays = []
    for off in range(0, 7201, 60):
        elapsed = off * 0.4
        if off == 7200:
            per, clk, text = 4, 0.0, "End of Game"
        else:
            per = min(4, 1 + int(elapsed // 720))
            clk = 720.0 - (elapsed % 720)
            text = ""
        plays.append({"t": t0 + off, "period": per, "clock_s": clk,
                      "home_score": 0, "away_score": 0, "text": text})
    return plays


def _game(title, away, home, winner, price_a_fn, market_id):
    prices_a, prices_b = [], []
    for off in range(-3600, 7201, 60):
        pa = price_a_fn(off)
        prices_a.append({"t": T0 + off, "p": pa})
        prices_b.append({"t": T0 + off, "p": round(1.0 - pa, 4)})
    return {"et_date": "20261119", "away": away, "home": home, "title": title,
            "market_id": market_id, "start_ts": T0, "winner": winner,
            "token_a": "tok_a", "token_b": "tok_b",
            "outcomes": [title.split(" vs. ")[0], title.split(" vs. ")[1]],
            "fee_rate": 0.05, "granularity": "1d/1",
            "prices_a": prices_a, "prices_b": prices_b, "plays": _plays(T0),
            "coverage": {"pregame_window_covered": True}}


def _price_a_take_profit(off):
    # 赛前 0.35 -> Q2 跌到 0.30 成交 -> Q3 涨回 0.40 落袋
    if off < 0:
        return 0.35
    if off < 2400:
        return 0.34
    if off == 2400:
        return 0.30
    if off < 4200:
        return 0.31
    return 0.40


def _price_a_q4_exit(off):
    # 赛前 0.35 -> Q2 跌到 0.30 成交 -> 一路跌到 0.10，Q4 剩 2:00 市价出
    if off < 0:
        return 0.35
    if off < 2400:
        return 0.34
    if off == 2400:
        return 0.30
    if off < 6000:
        return 0.20
    return 0.10


def _run(game):
    params = (CONFIG.get("strategy") or {}).get("scalp_dip") or {}
    clock = ReplayClock([game])
    strategy = ScalpDipStrategy(params=params, clock=clock)
    ledger = PaperLedger(CONFIG["risk"]["total_capital"])
    risk = RiskManager(CONFIG)
    logs = []
    st = replay_game(game, strategy, clock, ledger, risk, logs)
    return st, ledger, logs


def test_take_profit():
    g = _game("Warriors vs. Grizzlies", "GSW", "MEM", "GSW",
              _price_a_take_profit, "m-tp")
    st, ledger, _logs = _run(g)
    assert st["take_profits"] == 1, st
    assert st["legs_opened"] == 1, st
    assert abs(st["pnl"] - 3.00) < 1e-9, st
    assert abs(ledger.snapshot()["cash"] - 1003.00) < 1e-9, ledger.snapshot()
    print("PASS test_take_profit")


def test_q4_exit():
    g = _game("Warriors vs. Grizzlies", "GSW", "MEM", "MEM",
              _price_a_q4_exit, "m-q4")
    st, ledger, _logs = _run(g)
    assert st["q4_exits"] == 1, st
    assert st["q4_exit_prices"] == [0.10], st
    # 0.10*30 - taker费 0.05*30*0.10*0.90=0.135 - 成本 9.00 = -6.135
    assert abs(st["pnl"] - (-6.135)) < 1e-9, st
    assert abs(ledger.snapshot()["cash"] - 993.865) < 0.01, ledger.snapshot()
    print("PASS test_q4_exit")


def test_replay_clock():
    g = _game("Warriors vs. Grizzlies", "GSW", "MEM", "GSW",
              _price_a_take_profit, "m-clock")
    clock = ReplayClock([g])
    clock.set_now(T0 - 60)
    snap = clock.get_game("GSW", "MEM", "20261119")
    assert snap["state"] == "pre" and snap["clock_s"] is None, snap
    clock.set_now(T0 + 3600)
    snap = clock.get_game("GSW", "MEM", "20261119")
    assert snap["state"] == "in" and snap["period"] == 3, snap
    clock.set_now(T0 + 7201)
    snap = clock.get_game("GSW", "MEM", "20261119")
    assert snap["state"] == "final" and snap["winner"] == "GSW", snap
    assert clock.get_game("GSW", "XXX", "20261119") is None
    assert clock.get_game("GSW", "MEM", "20261118") is None
    print("PASS test_replay_clock")


if __name__ == "__main__":
    test_take_profit()
    test_q4_exit()
    test_replay_clock()
    print("3/3 通过")

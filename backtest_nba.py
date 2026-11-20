"""NBA 回测器：把真实的 ScalpDipStrategy 在过去打完的 NBA 比赛上重放。

数据：ESPN 记分牌（找已打完的比赛）-> Gamma slug（moneyline 市场）->
CLOB /prices-history（两边 token 价格历史）-> ESPN summary plays（时钟线）。
原始数据缓存到 nba_backtest_data/，重跑不再重复拉（见 quant_platform/nba_history.py）。

回放口径（诚实说明，见报告）：
- 价格序列每时间点只有一个价，回测里把它同时当 bid 和 ask 用（忽略点差）。
  这会轻微高估成交率；关键数“摸到 0.40 的概率”看的是价格触及，序列直接覆盖。
- 价格粒度随比赛新旧变：开球 <24h 前 1 分钟，<7 天 5 分钟，更老 10 分钟。
  粗粒度会漏掉短暂触及（尤其 120 秒的赛前窗口和 0.40 的瞬间摸到），
  所以 5/10 分钟粒度的比赛算出的触及概率是下限。
- 时钟线来自 ESPN summary plays；终场按 ESPN 比分结算（策略自己调 settle_leg）。

用法：
    python3 backtest_nba.py                  # 过去 7 天全量（先拉数据，再重放）
    python3 backtest_nba.py --games 5        # 最近 5 场小样本验证链路
    python3 backtest_nba.py --fetch-only     # 只拉数据不重放
    python3 backtest_nba.py --no-fetch       # 只用缓存重放，不碰网络
    python3 backtest_nba.py --days 3 --refresh
"""
import argparse
import bisect
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CONFIG
from quant_platform.ledger import PaperLedger
from quant_platform.risk import RiskManager
from quant_platform.strategy import ScalpDipStrategy
from quant_platform import nba_history


class ReplayClock:
    """重放时钟：set_now(ts) 设定重放时间；get_game 按时间线返回快照。
    state 由时间线位置定：第一条 play 之前 pre；最后一条 play（End of Game）
    之后 final；其余 in。接口与 quant_platform.nba_clock.NbaClock 一致。"""

    def __init__(self, games):
        self.index = {}
        self._times = {}
        for g in games:
            key = (frozenset((g["away"], g["home"])), g["et_date"])
            self.index[key] = g
            self._times[key] = [p["t"] for p in g["plays"]]
        self.now = None

    def set_now(self, ts):
        self.now = ts

    def get_game(self, abbr_a, abbr_b, date_str):
        key = (frozenset((abbr_a, abbr_b)), date_str)
        g = self.index.get(key)
        if g is None or self.now is None:
            return None
        plays = g["plays"]
        if not plays:
            return None
        idx = bisect.bisect_right(self._times[key], self.now) - 1
        base = {"home_abbr": g["home"], "away_abbr": g["away"],
                "winner": g["winner"], "date": date_str}
        if idx < 0:
            return dict(base, state="pre", period=0, clock_s=None,
                        home_score=0, away_score=0)
        pl = plays[idx]
        final = idx == len(plays) - 1
        return dict(base, state="final" if final else "in",
                    period=pl["period"] or 0, clock_s=pl["clock_s"],
                    home_score=pl["home_score"], away_score=pl["away_score"])


def _price_at(pts, times, ts):
    """forward-fill：ts 时刻最后已知价；ts 早于第一点返回 None。"""
    i = bisect.bisect_right(times, ts) - 1
    if i < 0:
        return None
    return pts[i]["p"]


_TAKE_RE = re.compile(r"触及 0\.40 落袋.*盈亏 ([+-][0-9.]+)")
_Q4_RE = re.compile(r"Q4 剩 [0-9.]+s 未到 0\.40，市价 ([0-9.]+) 平掉.*盈亏 ([+-][0-9.]+)")
_SETTLE_RE = re.compile(r"终场结算：[AB]队(赢|输)，盈亏 ([+-][0-9.]+)")
_LOCK_RE = re.compile(r"双边同时成交锁利.*锁定 ([+-][0-9.]+)")


def replay_game(game, strategy, clock, ledger, risk, log_list):
    """重放一场比赛。返回本场统计（盈亏以账本 trades 为准，结局分类以 [SCALP] 日志为准）。
    log_list 是日志行列表（调用方传入，函数内 append）。"""
    start_ts = game["start_ts"]
    plays = game["plays"]
    end_t = plays[-1]["t"] if plays else start_ts
    pa_list, pb_list = game["prices_a"], game["prices_b"]
    ta = [p["t"] for p in pa_list]
    tb = [p["t"] for p in pb_list]
    times = sorted({t for t in ta + tb if t <= end_t + 1})
    mid = game["market_id"]
    before = len(log_list)

    def tick(ts):
        pa = _price_at(pa_list, ta, ts)
        pb = _price_at(pb_list, tb, ts)
        if pa is None or pb is None:
            return
        clock.set_now(ts)
        state = {"market_id": mid, "question": game["title"],
                 "a_bid": pa, "a_ask": pa, "b_bid": pb, "b_ask": pb,
                 "fee_rate": game.get("fee_rate", 0.05),
                 "tag": "nba", "start_ts": start_ts}
        strategy.on_book(state, {"ledger": ledger, "risk": risk,
                                 "log": log_list.append, "ts": ts})

    for ts in times:
        tick(ts)
    # 终场 tick：比赛结束 +1 秒，用最后已知价，触发终场结算（策略自己调 settle_leg）
    last_pa = _price_at(pa_list, ta, end_t)
    last_pb = _price_at(pb_list, tb, end_t)
    if last_pa is not None and last_pb is not None:
        clock.set_now(end_t + 1)
        state = {"market_id": mid, "question": game["title"],
                 "a_bid": last_pa, "a_ask": last_pa,
                 "b_bid": last_pb, "b_ask": last_pb,
                 "fee_rate": game.get("fee_rate", 0.05),
                 "tag": "nba", "start_ts": start_ts}
        strategy.on_book(state, {"ledger": ledger, "risk": risk,
                                 "log": log_list.append, "ts": end_t + 1})

    title = game["title"]
    lines = [l for l in log_list[before:] if title in l]
    st = {"game": f"{game['et_date']} {game['away']}-{game['home']}",
          "title": title, "granularity": game.get("granularity"),
          "pregame_window_covered": game.get("coverage", {}).get("pregame_window_covered"),
          "legs_opened": 0, "pregame_fills": 0, "ingame_fills": 0,
          "take_profits": 0, "take_profit_pnl": 0.0,
          "q4_exits": 0, "q4_exit_prices": [], "q4_exit_pnl": 0.0,
          "settlements_win": 0, "settlements_loss": 0, "settlement_pnl": 0.0,
          "locks": 0, "lock_pnl": 0.0, "risk_blocked": 0,
          "pnl": 0.0, "trades": 0}
    for l in lines:
        if "赛前成交" in l:
            st["pregame_fills"] += 1
        if "赛中低吸成交" in l:
            st["ingame_fills"] += 1
        m = _TAKE_RE.search(l)
        if m:
            st["take_profits"] += 1
            st["take_profit_pnl"] += float(m.group(1))
            continue
        m = _Q4_RE.search(l)
        if m:
            st["q4_exits"] += 1
            st["q4_exit_prices"].append(float(m.group(1)))
            st["q4_exit_pnl"] += float(m.group(2))
            continue
        m = _SETTLE_RE.search(l)
        if m:
            if m.group(1) == "赢":
                st["settlements_win"] += 1
            else:
                st["settlements_loss"] += 1
            st["settlement_pnl"] += float(m.group(2))
            continue
        m = _LOCK_RE.search(l)
        if m:
            st["locks"] += 1
            st["lock_pnl"] += float(m.group(1))
            continue
        if "熔断拒单" in l:
            st["risk_blocked"] += 1
    # 盈亏以账本为准：CLOSE/SETTLE/LOCK 的 locked_pnl 之和（OPEN_LEG 那行是现金流出，不是盈亏）
    pnl = 0.0
    n_trades = 0
    for tr in ledger.trades:
        if tr.get("market") != mid:
            continue
        n_trades += 1
        if tr.get("direction") == "OPEN_LEG":
            st["legs_opened"] += 1
        else:
            pnl += tr.get("locked_pnl") or 0.0
    st["pnl"] = round(pnl, 4)
    st["trades"] = n_trades
    return st


def load_cached_range(days, data_dir, games_limit=0):
    """只读缓存：过去 days 天的比赛记录（按开球时间升序）。"""
    dates = set(nba_history.past_et_dates(days))
    records = []
    if not os.path.isdir(data_dir):
        return records
    for fn in sorted(os.listdir(data_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            rec = nba_history.load_game(os.path.join(data_dir, fn))
        except Exception:
            continue
        if rec.get("et_date") in dates:
            records.append(rec)
    records.sort(key=lambda r: r["start_ts"])
    if games_limit:
        records = records[-games_limit:]
    return records


def run_backtest(records):
    """重放全部比赛（按开球时间升序；每天一个新账本，日亏熔断按天算）。"""
    params = (CONFIG.get("strategy") or {}).get("scalp_dip") or {}
    clock = ReplayClock(records)
    strategy = ScalpDipStrategy(params=params, clock=clock)
    logs = []
    per_game = []
    by_date = {}
    for g in records:
        by_date.setdefault(g["et_date"], []).append(g)
    for et_date in sorted(by_date):
        ledger = PaperLedger(CONFIG["risk"]["total_capital"])
        risk = RiskManager(CONFIG)
        for g in sorted(by_date[et_date], key=lambda x: x["start_ts"]):
            per_game.append(replay_game(g, strategy, clock, ledger, risk, logs))
    return per_game, logs


def summarize(per_game, skipped):
    s = {"games": len(per_game), "skipped": skipped,
         "legs_opened": sum(g["legs_opened"] for g in per_game),
         "pregame_fills": sum(g["pregame_fills"] for g in per_game),
         "ingame_fills": sum(g["ingame_fills"] for g in per_game),
         "take_profits": sum(g["take_profits"] for g in per_game),
         "take_profit_pnl": round(sum(g["take_profit_pnl"] for g in per_game), 2),
         "q4_exits": sum(g["q4_exits"] for g in per_game),
         "q4_exit_prices": sorted(p for g in per_game for p in g["q4_exit_prices"]),
         "q4_exit_pnl": round(sum(g["q4_exit_pnl"] for g in per_game), 2),
         "settlements_win": sum(g["settlements_win"] for g in per_game),
         "settlements_loss": sum(g["settlements_loss"] for g in per_game),
         "settlement_pnl": round(sum(g["settlement_pnl"] for g in per_game), 2),
         "locks": sum(g["locks"] for g in per_game),
         "lock_pnl": round(sum(g["lock_pnl"] for g in per_game), 2),
         "risk_blocked": sum(g["risk_blocked"] for g in per_game),
         "total_pnl": round(sum(g["pnl"] for g in per_game), 2),
         "per_game": per_game}
    filled = s["take_profits"] + s["q4_exits"] + s["settlements_win"] + s["settlements_loss"]
    s["filled_legs"] = filled
    s["key_prob"] = (round(s["take_profits"] / filled, 4) if filled else None)
    s["avg_pnl_per_game"] = (round(s["total_pnl"] / len(per_game), 2) if per_game else 0.0)
    s["avg_pnl_per_filled_leg"] = (round(s["total_pnl"] / filled, 2) if filled else None)
    pnls = [g["pnl"] for g in per_game]
    s["max_single_game_loss"] = round(min(pnls), 2) if pnls else None
    s["max_single_game_win"] = round(max(pnls), 2) if pnls else None
    s["games_with_fill"] = sum(1 for g in per_game if g["legs_opened"] or g["locks"])
    gran = {}
    for g in per_game:
        gran[g.get("granularity")] = gran.get(g.get("granularity"), 0) + 1
    s["granularity_mix"] = gran
    s["pregame_window_covered"] = sum(1 for g in per_game if g.get("pregame_window_covered"))
    s["notes"] = ("价格序列每时间点只有一个价，回测里同时当 bid 和 ask 用（忽略点差），"
                  "会轻微高估成交率；粗粒度（5/10 分钟）会漏掉短暂触及，"
                  "所以触及概率是下限；120 秒的赛前窗口在 5/10 分钟粒度下经常没有价格点，"
                  "赛前成交会被低估；5 分钟粒度下 Q4 剩 2:00 的出场窗口也可能被跳过，"
                  "导致个别腿直接终场结算（实盘 5 秒轮询时一般能赶上出场）。"
                  "分类小计（落袋/Q4出/结算）来自 [SCALP] 日志的 2 位小数盈亏，"
                  "与账本总盈亏可能有 ±0.05 以内的 rounding 差异，总盈亏以账本为准。")
    return s


def print_report(s):
    print(f"\n=== NBA 回测：scalp_dip（{s['games']} 场）===")
    print(f"入场腿数：{s['legs_opened']}（赛前 {s['pregame_fills']} / 赛中低吸 {s['ingame_fills']}；"
          f"双边锁利 {s['locks']} 次，+{s['lock_pnl']:.2f}）")
    print(f"摸到 0.40 落袋：{s['take_profits']} 次，+{s['take_profit_pnl']:.2f}")
    print(f"Q4 剩 2:00 市价出：{s['q4_exits']} 次，{s['q4_exit_pnl']:+.2f}，"
          f"出场价 {s['q4_exit_prices']}")
    print(f"终场结算：赢 {s['settlements_win']} / 输 {s['settlements_loss']}，"
          f"{s['settlement_pnl']:+.2f}")
    if s['risk_blocked']:
        print(f"日亏熔断拒单：{s['risk_blocked']} 次")
    print(f"关键数 P(0.30 成交 -> Q4 剩 2:00 前摸到 0.40) = {s['key_prob']} "
          f"（{s['take_profits']}/{s['filled_legs']}）")
    print(f"总盈亏：{s['total_pnl']:+.2f} ｜ 每场平均：{s['avg_pnl_per_game']:+.2f} ｜ "
          f"每成交腿平均：{s['avg_pnl_per_filled_leg']} ｜ "
          f"最大单场亏损：{s['max_single_game_loss']} ｜ 最大单场盈利：{s['max_single_game_win']}")
    print(f"有成交的场次：{s['games_with_fill']}/{s['games']} ｜ "
          f"赛前窗口有价格点的场次：{s['pregame_window_covered']}/{s['games']} ｜ "
          f"粒度分布：{s['granularity_mix']}")
    if s['skipped']:
        print(f"跳过的场次（{len(s['skipped'])}）：")
        for sk in s['skipped']:
            print(f"  {sk['game']}：{sk['reason']}")
    print(f"说明：{s['notes']}")


def main():
    ap = argparse.ArgumentParser(description="NBA 回测器：scalp_dip 在过去打完的比赛上重放")
    ap.add_argument("--days", type=int, default=7, help="过去 N 天（默认 7）")
    ap.add_argument("--games", type=int, default=0, help="只跑最近 N 场（0=全部）")
    ap.add_argument("--data-dir", default="nba_backtest_data", help="缓存目录")
    ap.add_argument("--refresh", action="store_true", help="缓存也重拉")
    ap.add_argument("--fetch-only", action="store_true", help="只拉数据不重放")
    ap.add_argument("--no-fetch", action="store_true", help="只用缓存重放，不碰网络")
    ap.add_argument("--output", default="nba_backtest_results.json", help="结果 JSON 路径")
    args = ap.parse_args()
    root = os.path.dirname(os.path.abspath(__file__))
    data_dir = args.data_dir if os.path.isabs(args.data_dir) else os.path.join(root, args.data_dir)

    if args.no_fetch:
        records = load_cached_range(args.days, data_dir, args.games)
        skipped = []
        print(f"缓存重放：{len(records)} 场（{data_dir}）")
    else:
        records, skipped = nba_history.fetch_range(
            days=args.days, data_dir=data_dir, games_limit=args.games,
            refresh=args.refresh)
        print(f"数据就绪：{len(records)} 场，跳过 {len(skipped)} 场")
        for sk in skipped:
            print(f"  跳过 {sk['game']}：{sk['reason']}")
        if args.fetch_only:
            return 0
    if not records:
        print("没有可重放的比赛数据。")
        return 1
    t0 = time.time()
    per_game, _logs = run_backtest(records)
    s = summarize(per_game, skipped)
    print_report(s)
    out_path = args.output if os.path.isabs(args.output) else os.path.join(root, args.output)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    print(f"\n结果已写 {out_path}（用时 {time.time() - t0:.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

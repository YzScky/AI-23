"""NBA 新策略回测：赛前 0.20-0.30 低吸（30U），盘中触 0.50 全卖，
第三节中段（Q3 比赛时钟剩 6:00）前未触及则当时市价全卖。无止损。

规则（用户 2026-11-23 确认，止盈 0.50 + Q3 中段出场）：
- 每天扫当天所有比赛，只买赛前价格在 [0.20, 0.30]（含）那一边，其他不买。
- 买入窗口：开赛前 1 天都可以（T0-24h ~ T0）；窗口内价格第一次进 [0.20, 0.30]
  就买入，30U 一腿，份数 = 30 / 买入价。每场最多一腿。
- 买入后等开赛；盘中（T0 之后）第一次触及 0.50 就全部卖出（挂 0.50 限价，
  maker，费 0）；到第三节中段（Q3 比赛时钟剩 6:00）还没触及，就在当时按市价
  全部卖出（taker，体育费率 0.05）。
- 无止损。Q3 中段时钟线缺失的极端情况用 Q4 开始兜底，再缺失才 fallback 到终场结算。

费用（已核实口径）：taker 费 = 0.05 x 份数 x 价 x (1 - 价)，体育；
maker（挂单）费 0；结算/赎回费 0。买入按市价买算 taker。

回放口径（诚实说明）：
- 价格序列每时间点只有一个价，同时当 bid 和 ask 用（忽略点差），成交略高估。
- 粒度：1d/1 分钟、1w/5 分钟；粗粒度会漏掉短暂触及，0.40 触及概率是下限。
- 赛前 24h 窗口覆盖不全：39 场全覆盖，28 场只有部分（最少 17.4h，
  因“1d”区间只回望拉取时刻前 24h）。覆盖外的价格进区间是看不到的，
  所以入场腿数是下限。
- Q4 开始取 ESPN plays 里第一条 period==4 的时间（首次记录到的 Q4 事件，
  略晚于真实 Q4 开始）；出场价按该时刻的 forward-fill 价。

用法：
    python3 backtest_nba_v2.py                  # 缓存里全部 67 场
    python3 backtest_nba_v2.py --output x.json   # 换结果文件
"""
import argparse
import bisect
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quant_platform import nba_history

SIZE_U = 30.0
LO, HI = 0.20, 0.30
TARGET = 0.50
FEE_RATE = 0.05
WINDOW_S = 24 * 3600
EXIT_Q3_CLOCK_S = 360  # 出场：第三节中段 = Q3 比赛时钟剩 6:00


def _price_at(pts, times, ts):
    i = bisect.bisect_right(times, ts) - 1
    if i < 0:
        return None
    return pts[i]["p"]


def _taker_fee(shares, price):
    return FEE_RATE * shares * price * (1.0 - price)


def replay_game(g, target=TARGET):
    """重放一场。返回 per-game dict（含 0 或 1 腿）。"""
    t0 = g["start_ts"]
    plays = g["plays"] or []
    pa, pb = g["prices_a"], g["prices_b"]
    ta = [p["t"] for p in pa]
    tb = [p["t"] for p in pb]
    st = {"game": f"{g['et_date']} {g['away']}-{g['home']}",
          "title": g["title"], "granularity": g.get("granularity"),
          "legs": [], "pnl": 0.0, "entry": None}
    # 覆盖：两侧最早价到 T0 的小时数（取较晚的那边）
    cov_h = (t0 - max(pa[0]["t"], pb[0]["t"])) / 3600.0 if pa and pb else 0.0
    st["pregame_coverage_h"] = round(cov_h, 1)
    st["full_24h"] = cov_h >= 24.0

    # --- 入场：窗口内第一次进 [0.20, 0.30] 的点（A 优先于 B，同时间）---
    pre = []
    for side, pts in (("A", pa), ("B", pb)):
        for p in pts:
            if t0 - WINDOW_S <= p["t"] < t0:
                pre.append((p["t"], side, p["p"]))
    pre.sort(key=lambda x: (x[0], x[1]))
    entry = None
    for t, side, p in pre:
        if LO <= p <= HI:
            entry = {"t": t, "side": side, "price": p,
                     "shares": SIZE_U / p, "lead_h": round((t0 - t) / 3600.0, 2)}
            break
    if entry is None:
        st["no_entry_reason"] = ("no_point_in_range" if pre
                                 else "no_pregame_points_in_window")
        return st
    entry["fee_buy"] = round(_taker_fee(entry["shares"], entry["price"]), 4)
    st["entry"] = entry

    # --- 出场 ---
    held_pts = pa if entry["side"] == "A" else pb
    held_times = [p["t"] for p in held_pts]
    # 出场时刻：第三节中段（plays 里第一条 period==3 且 clock_s<=360 的记录），
    # 缺失时用 Q4 开始（第一条 period>=4 的记录）兜底
    exit_t, exit_kind = None, None
    for pl in plays:
        per, cs = pl.get("period"), pl.get("clock_s")
        if per == 3 and cs is not None and cs <= EXIT_Q3_CLOCK_S:
            exit_t, exit_kind = pl["t"], "Q3_MID"
            break
        if per is not None and per >= 4:
            exit_t, exit_kind = pl["t"], "Q4_START"
            break
    leg = {"side": entry["side"], "entry_price": entry["price"],
           "shares": round(entry["shares"], 2), "entry_t": entry["t"],
           "lead_h": entry["lead_h"], "fee_buy": entry["fee_buy"]}
    # TP：T0 之后、出场时刻之前第一次 >= target
    tp_t = None
    for p in held_pts:
        if p["t"] < t0:
            continue
        if exit_t is not None and p["t"] >= exit_t:
            break
        if p["p"] >= target:
            tp_t = p["t"]
            break
    if tp_t is not None:
        leg.update({"exit": "TP", "exit_price": target, "exit_t": tp_t,
                    "fee_sell": 0.0,
                    "pnl": round(entry["shares"] * target - SIZE_U
                                 - entry["fee_buy"], 4)})
    elif exit_t is not None:
        exit_p = _price_at(held_pts, held_times, exit_t)
        if exit_p is None:
            exit_p = entry["price"]
        fee_sell = round(_taker_fee(entry["shares"], exit_p), 4)
        leg.update({"exit": exit_kind, "exit_price": exit_p, "exit_t": exit_t,
                    "fee_sell": fee_sell,
                    "pnl": round(entry["shares"] * exit_p - fee_sell - SIZE_U
                                 - entry["fee_buy"], 4)})
    else:
        # 无 Q3 中段也无 Q4 时钟线：终场结算
        won = (g.get("winner") == g["away"] if entry["side"] == "A"
               else g.get("winner") == g["home"])
        receipt = entry["shares"] if won else 0.0
        leg.update({"exit": "SETTLE", "exit_price": 1.0 if won else 0.0,
                    "exit_t": plays[-1]["t"] if plays else t0, "fee_sell": 0.0,
                    "pnl": round(receipt - SIZE_U - entry["fee_buy"], 4)})
    st["legs"].append(leg)
    st["pnl"] = leg["pnl"]
    return st


def load_all_cached(data_dir):
    records = []
    if not os.path.isdir(data_dir):
        return records
    for fn in sorted(os.listdir(data_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            records.append(nba_history.load_game(os.path.join(data_dir, fn)))
        except Exception:
            continue
    records.sort(key=lambda r: r["start_ts"])
    return records


def summarize(per_game, target=TARGET):
    legs = [l for g in per_game for l in g["legs"]]
    tp = [l for l in legs if l["exit"] == "TP"]
    q3 = [l for l in legs if l["exit"] == "Q3_MID"]
    q4 = [l for l in legs if l["exit"] == "Q4_START"]
    stl = [l for l in legs if l["exit"] == "SETTLE"]
    wins = [l for l in legs if l["pnl"] > 0]
    total = round(sum(l["pnl"] for l in legs), 2)
    s = {"target": target, "exit_q3_clock_s": EXIT_Q3_CLOCK_S,
         "games": len(per_game), "legs": len(legs),
         "games_with_entry": sum(1 for g in per_game if g["legs"]),
         "games_no_entry": sum(1 for g in per_game if not g["legs"]),
         "no_entry_reasons": dict(Counter(g.get("no_entry_reason", "?")
                                           for g in per_game if not g["legs"])),
         "full_24h_games": sum(1 for g in per_game if g.get("full_24h")),
         "tp_count": len(tp), "tp_pnl": round(sum(l["pnl"] for l in tp), 2),
         "q3_count": len(q3), "q3_pnl": round(sum(l["pnl"] for l in q3), 2),
         "q3_exit_prices": sorted(l["exit_price"] for l in q3),
         "q4_count": len(q4), "q4_pnl": round(sum(l["pnl"] for l in q4), 2),
         "q4_exit_prices": sorted(l["exit_price"] for l in q4),
         "settle_count": len(stl), "settle_pnl": round(sum(l["pnl"] for l in stl), 2),
         "win_legs": len(wins),
         "win_rate": round(len(wins) / len(legs), 4) if legs else None,
         "total_pnl": total,
         "avg_pnl_per_game": round(total / len(per_game), 2) if per_game else 0.0,
         "avg_pnl_per_leg": round(total / len(legs), 2) if legs else None,
         "max_win_leg": round(max((l["pnl"] for l in legs), default=0.0), 2),
         "max_loss_leg": round(min((l["pnl"] for l in legs), default=0.0), 2),
         "entry_prices": sorted(l["entry_price"] for l in legs),
         "entry_lead_h": sorted(l["lead_h"] for l in legs),
         "total_fee_buy": round(sum(l["fee_buy"] for l in legs), 2),
         "total_fee_sell": round(sum(l["fee_sell"] for l in legs), 2),
         "granularity_mix": dict(Counter(g.get("granularity") for g in per_game)),
         "per_game": per_game,
         "notes": (f"30U/腿，买入价 0.20-0.30，买入按市价算 taker 费 0.05；"
                   f"触 {target} 按挂单卖费 0；Q3 中段（比赛时钟剩 6:00）市价卖算 taker 费；无止损。"
                   "价格序列单价同时当 bid/ask，成交略高估；5 分钟粒度漏短暂触及，"
                   f"{target} 触及是下限。"
                   f"{sum(1 for g in per_game if g.get('full_24h'))} 场赛前 24h 全覆盖、"
                   f"{sum(1 for g in per_game if not g.get('full_24h'))} 场只有部分"
                   f"（最少 {min(g.get('pregame_coverage_h', 0) for g in per_game):.1f}h），"
                   "入场腿数是下限。Q3 中段取 plays 第一条 period==3 且 clock<=360 的时间；"
                   "缺失时用 Q4 开始兜底。")}
    return s


def print_report(s):
    print(f"\n=== NBA 回测：赛前 0.20-0.30 低吸 30U，止盈 {s['target']}（{s['games']} 场）===")
    print(f"入场：{s['legs']} 腿（{s['games_with_entry']} 场有入场 / {s['games_no_entry']} 场无入场 "
          f"{s['no_entry_reasons']}）；赛前 24h 全覆盖 {s['full_24h_games']} 场")
    print(f"触 {s['target']} 落袋：{s['tp_count']} 次，{s['tp_pnl']:+.2f}")
    print(f"Q3 中段市价出：{s['q3_count']} 次，{s['q3_pnl']:+.2f}，"
          f"出场价 {s['q3_exit_prices']}")
    if s['q4_count']:
        print(f"Q4 开始市价出（Q3 中段时钟线缺失兜底）：{s['q4_count']} 次，"
              f"{s['q4_pnl']:+.2f}，出场价 {s['q4_exit_prices']}")
    print(f"终场结算（无 Q3 中段也无 Q4 时钟线）：{s['settle_count']} 次，{s['settle_pnl']:+.2f}")
    print(f"胜率：{s['win_legs']}/{s['legs']} = {s['win_rate']}｜"
          f"总盈亏：{s['total_pnl']:+.2f}｜每场平均：{s['avg_pnl_per_game']:+.2f}｜"
          f"每腿平均：{s['avg_pnl_per_leg']}｜最大单腿赢：{s['max_win_leg']:+.2f}｜"
          f"最大单腿亏：{s['max_loss_leg']:+.2f}")
    print(f"买入价分布：{s['entry_prices']}")
    print(f"买入提前（小时）：{s['entry_lead_h']}")
    print(f"费用合计：买入 taker {s['total_fee_buy']:.2f} + 卖出 taker {s['total_fee_sell']:.2f}")
    print(f"粒度分布：{s['granularity_mix']}")
    print(f"说明：{s['notes']}")


def main():
    ap = argparse.ArgumentParser(description="NBA 新策略回测：赛前 0.20-0.30 低吸 30U")
    ap.add_argument("--data-dir", default="nba_backtest_data", help="缓存目录")
    ap.add_argument("--output", default="nba_backtest_v2_results.json", help="结果 JSON 路径")
    ap.add_argument("--target", type=float, default=TARGET, help="止盈价（默认 0.40）")
    args = ap.parse_args()
    root = os.path.dirname(os.path.abspath(__file__))
    data_dir = args.data_dir if os.path.isabs(args.data_dir) else os.path.join(root, args.data_dir)
    records = load_all_cached(data_dir)
    print(f"缓存重放：{len(records)} 场（{data_dir}），止盈 {args.target}")
    if not records:
        print("没有可重放的比赛数据。")
        return 1
    per_game = [replay_game(g, target=args.target) for g in records]
    s = summarize(per_game, target=args.target)
    print_report(s)
    out_path = args.output if os.path.isabs(args.output) else os.path.join(root, args.output)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    print(f"\n结果已写 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""参数扫荡：赛前低吸在 74 场缓存上找更好的解法。

网格：lo {0.05,0.10,0.15,0.20} x hi {0.25,0.30,0.35}
      x target {0.35,0.40,0.45,0.50,0.60}
      x exit {q3_start, q3_mid, q4_start} = 180 种组合。
再对前 3 名试止损 {0.05, 0.10, 0.15}。
只读缓存，不碰网络。跑：python3 /tmp/sweep_pregame.py"""
import itertools
import json
import os
import sys

sys.path.insert(0, "/home/hatch/workspace/polymarket-platform")
os.chdir("/home/hatch/workspace/polymarket-platform")

from backtest_nba_v2 import load_all_cached, replay_game, summarize

records = load_all_cached("nba_backtest_data")
print(f"缓存：{len(records)} 场")
assert len(records) == 74, f"缓存应为 74 场，实际 {len(records)}"

LOS = [0.05, 0.10, 0.15, 0.20]
HIS = [0.25, 0.30, 0.35]
TARGETS = [0.35, 0.40, 0.45, 0.50, 0.60]
EXITS = ["q3_start", "q3_mid", "q4_start"]

results = []
for lo, hi, target, exit_mode in itertools.product(LOS, HIS, TARGETS, EXITS):
    if lo >= hi:
        continue
    per_game = [replay_game(g, target=target, lo=lo, hi=hi, exit_mode=exit_mode)
                for g in records]
    s = summarize(per_game, target=target, lo=lo, hi=hi, exit_mode=exit_mode)
    results.append({"lo": lo, "hi": hi, "target": target, "exit": exit_mode,
                    "legs": s["legs"], "tp": s["tp_count"],
                    "tp_pnl": s["tp_pnl"], "exit_pnl": round(s["q3_pnl"] + s["q4_pnl"], 2),
                    "win_rate": s["win_rate"], "total": s["total_pnl"],
                    "per_leg": s["avg_pnl_per_leg"],
                    "max_win": s["max_win_leg"], "max_loss": s["max_loss_leg"]})
results.sort(key=lambda r: r["total"], reverse=True)

print(f"\n共 {len(results)} 种组合，前 15 名（按总盈亏）：")
print(f"{'买入区间':>12} {'止盈':>6} {'出场':>9} {'腿':>3} {'落袋':>4} "
      f"{'胜率':>6} {'总盈亏':>8} {'每腿':>6} {'最大赢':>7} {'最大亏':>7}")
for r in results[:15]:
    print(f"{r['lo']}-{r['hi']:>9} {r['target']:>6} {r['exit']:>9} {r['legs']:>3} "
          f"{r['tp']:>4} {r['win_rate']:>6.2f} {r['total']:>8.2f} "
          f"{r['per_leg']:>6.2f} {r['max_win']:>7.2f} {r['max_loss']:>7.2f}")

# 命名对照：当前三版 + 几个关键点
named = {
    "当前 v3（0.10-0.30/0.40/Q3开始）": (0.10, 0.30, 0.40, "q3_start"),
    "v1（0.20-0.30/0.40/Q4开始）": (0.20, 0.30, 0.40, "q4_start"),
    "0.20-0.30/0.40/Q3中段": (0.20, 0.30, 0.40, "q3_mid"),
    "0.20-0.30/0.50/Q3中段": (0.20, 0.30, 0.50, "q3_mid"),
    "0.10-0.30/0.40/Q4开始": (0.10, 0.30, 0.40, "q4_start"),
    "0.10-0.30/0.50/Q3开始": (0.10, 0.30, 0.50, "q3_start"),
}
print("\n命名对照：")
for name, key in named.items():
    r = next(x for x in results if (x["lo"], x["hi"], x["target"], x["exit"]) == key)
    rank = results.index(r) + 1
    print(f"  {name}：{r['legs']} 腿，{r['tp']} 落袋，胜率 {r['win_rate']:.2f}，"
          f"总 {r['total']:+.2f}，排名 {rank}/{len(results)}")

# 稳健性：最好组合的邻居（每个维度 ± 一档）
best = results[0]
print(f"\n最好组合 {best['lo']}-{best['hi']} / {best['target']} / {best['exit']} "
      f"（总 {best['total']:+.2f}）的邻居：")
neighbors = []
for lo in LOS:
    if abs(LOS.index(lo) - LOS.index(best["lo"])) == 1:
        neighbors.append((lo, best["hi"], best["target"], best["exit"]))
for hi in HIS:
    if abs(HIS.index(hi) - HIS.index(best["hi"])) == 1:
        neighbors.append((best["lo"], hi, best["target"], best["exit"]))
for t in TARGETS:
    if abs(TARGETS.index(t) - TARGETS.index(best["target"])) == 1:
        neighbors.append((best["lo"], best["hi"], t, best["exit"]))
for e in EXITS:
    if e != best["exit"]:
        neighbors.append((best["lo"], best["hi"], best["target"], e))
for key in neighbors:
    r = next(x for x in results if (x["lo"], x["hi"], x["target"], x["exit"]) == key)
    print(f"  {key[0]}-{key[1]} / {key[2]} / {key[3]}：{r['legs']} 腿，"
          f"总 {r['total']:+.2f}（差 {r['total'] - best['total']:+.2f}）")

# 前 3 名试止损
print("\n前 3 名试止损：")
for r in results[:3]:
    for stop in (0.05, 0.10, 0.15):
        per_game = [replay_game(g, target=r["target"], lo=r["lo"], hi=r["hi"],
                                exit_mode=r["exit"], stop=stop) for g in records]
        s = summarize(per_game, target=r["target"], lo=r["lo"], hi=r["hi"],
                      exit_mode=r["exit"], stop=stop)
        print(f"  {r['lo']}-{r['hi']} / {r['target']} / {r['exit']} "
              f"止损 {stop}：{s['legs']} 腿，{s['tp_count']} 落袋，"
              f"{s['stop_count']} 止损出，胜率 {s['win_rate']:.2f}，"
              f"总 {s['total_pnl']:+.2f}（无止损 {r['total']:+.2f}，"
              f"差 {s['total_pnl'] - r['total']:+.2f}）")

json.dump(results, open("/tmp/sweep_results.json", "w"), ensure_ascii=False, indent=1)
print("\n全部结果已写 /tmp/sweep_results.json")

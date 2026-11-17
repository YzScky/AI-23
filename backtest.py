"""双边 MM 回测引擎：拿 history.db 里攒的盘口快照重放策略。
纸面回测，不下真单，不连钱包。

用法：
  python backtest.py                                  # 全部历史，默认阈值 0.99/1.01
  python backtest.py --buy-threshold 1.02             # 放宽买触发，看引擎是否真会下单
  python backtest.py --from 2026-11-16 --to 2026-11-17 --market 663583
  python backtest.py --db /path/to/history.db --split 0.7

成交模型（诚实版，写在代码里，README 也有）：
- 每条 books 行 = 一个市场在某时刻的盘口快照（Mac 上 5 秒一轮询）。
- 触发：某快照 buy_both_sum（a_ask + b_ask）<= 买阈值 → 开 BUY 对；
  sell_both_sum（a_bid + b_bid）>= 卖阈值 → 开 SELL 对。每市场同时最多一对。
- 下单量按价格比例分，保证两边 payoff 相等（不是 50/50）：
  BUY 对每边买 size/buy_sum 份；SELL 对每边卖 size/sell_sum 份。
  完成对的锁定利润：BUY = size/buy_sum - size；SELL = size - size/sell_sum。
- 成交确认：一条腿只在**下一个快照**仍显示该边价格不差于触发价时才算成交
  （BUY 腿：下一快照 ask <= 触发 ask；SELL 腿：下一快照 bid >= 触发 bid）。
  这是对真实成交的保守近似——真实世界里挂单可能根本轮不到你。
- 单边：只有一条腿成交、另一条在 timeout（默认 30 秒，按快照时间算）内
  仍未成交 → 撤未成交腿，已成交腿按当前快照市价平掉（taker），扣该市场
  自己的 fee_rate：fee = 份数 * fee_rate * 平仓价 * (1 - 平仓价)。
  BUY 腿平仓：按当前 bid 卖出；SELL 腿平仓：按当前 ask 买回。
  若当前快照该边 bid/ask 缺失，退而用另一边价格；两边都缺则按触发价平（记 note）。
- 两条腿都在 timeout 内没成交 → 全撤，无盈亏，记 no_fill_cancel。
- 单边平仓出过亏损的市场，当天（北京时间日）不再进；日已实现亏损达到
  daily_loss_cap 后，当天不再开新对（已开的对照常结算）。
- 回放按全局时间顺序走（所有市场的快照按 ts 排序后单次遍历），日亏熔断
  和当日拉黑按真实时间先后生效；若按市场逐个重放，熔断会错判先后。
- 完成对是 maker，手续费 0。

样本内外划分（防过拟合）：
- 按时间把整个区间切两段：前 split（默认 0.7）为 in-sample，后 30% 为
  out-of-sample。两段用同一套参数各跑一遍，分别报告，再汇总。
  如果一段的快照太少（< 20 行），该段结果会明确标“数据太薄”。

诚实限制：
- Mac 上是 5 秒轮询快照，看不到 5 秒内的价格跳动；本回测是 5 秒分辨率
  近似，不是 tick 级回测。真实高频场景下结果会更差，不会更好。
- 完成对的锁定利润在真实世界要等市场结算才到账，回测里立即记入，
  会高估资金周转。
- 快照少于 50 行或 0 触发时，输出会明说数据太薄 / 无触发，不编结果。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CST = timezone(timedelta(hours=8))
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")
TRADE_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_trades.jsonl")


def parse_time(s):
    """接受 epoch 秒、YYYY-MM-DD、YYYY-MM-DD HH:MM[:SS]、ISO 格式；无时区视为北京时间。"""
    if s is None:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=CST)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise ValueError(f"时间格式无法解析：{s}（用 epoch 秒或 YYYY-MM-DD[ HH:MM]）")


def cst_day(ts):
    return datetime.fromtimestamp(ts, tz=CST).strftime("%Y-%m-%d")


def load_books(db_path, t_from, t_to, markets):
    if not os.path.exists(db_path):
        print(f"history.db 不存在：{db_path}\n先跑平台攒数据（./start.command），再跑回测。")
        sys.exit(1)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    q = ("SELECT ts, market_id, question, a_bid, a_ask, b_bid, b_ask, fee_rate "
         "FROM books WHERE 1=1")
    params = []
    if t_from is not None:
        q += " AND ts >= ?"
        params.append(t_from)
    if t_to is not None:
        q += " AND ts <= ?"
        params.append(t_to)
    if markets:
        q += " AND market_id IN (%s)" % ",".join("?" * len(markets))
        params.extend(markets)
    q += " ORDER BY market_id, ts"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    by_market = {}
    for r in rows:
        by_market.setdefault(r[1], []).append({
            "ts": r[0], "market_id": r[1], "question": r[2],
            "a_bid": r[3], "a_ask": r[4], "b_bid": r[5], "b_ask": r[6],
            "fee_rate": r[7] if r[7] is not None else 0.05,
        })
    return by_market


def leg_filled(direction, side, trig_price, snap):
    """下一快照该边价格是否仍不差于触发价。side: 'A' 或 'B'。"""
    if trig_price is None:
        return False
    if direction == "BUY_BOTH":
        p = snap["a_ask"] if side == "A" else snap["b_ask"]
        return p is not None and p <= trig_price
    p = snap["a_bid"] if side == "A" else snap["b_bid"]
    return p is not None and p >= trig_price


def close_price_for(direction, side, snap, trig_price):
    """单边平仓价：BUY 腿按当前 bid 卖，SELL 腿按当前 ask 买回；缺失时退化。"""
    if direction == "BUY_BOTH":
        p = snap["a_bid"] if side == "A" else snap["b_bid"]
        if p is not None:
            return p, "bid"
        p = snap["a_ask"] if side == "A" else snap["b_ask"]
        if p is not None:
            return p, "ask_fallback"
        return trig_price, "trigger_fallback"
    p = snap["a_ask"] if side == "A" else snap["b_ask"]
    if p is not None:
        return p, "ask"
    p = snap["a_bid"] if side == "A" else snap["b_bid"]
    if p is not None:
        return p, "bid_fallback"
    return trig_price, "trigger_fallback"


def run_segment(by_market, t_start, t_end, p):
    """在 [t_start, t_end) 区间跑一遍策略。p 为参数 dict。返回 stats + trades + equity 点。"""
    stats = {
        "snapshots": 0, "triggers": 0, "placed": 0, "completed": 0,
        "completed_win": 0, "single_side_closes": 0, "single_side_loss": 0.0,
        "no_fill_cancels": 0, "open_at_end": 0, "gross_pnl": 0.0,
        "fees": 0.0, "net_pnl": 0.0, "cap_stop_days": 0, "cap_stopped": False,
    }
    trades = []
    cash = p["bankroll"]
    equity = [(t_start, cash)]
    daily_loss = {}          # CST 日期 -> 当天已实现亏损
    blocked = set()          # (CST 日期, market_id)：单边亏损后当天不再进
    cap_days = set()         # 触发过日亏熔断的日期
    pending = {}             # market_id -> 未结算的对

    def log_trade(rec):
        rec["segment"] = p["segment"]
        trades.append(rec)

    def record_pnl(amount, ts):
        nonlocal cash
        cash += amount
        equity.append((ts, cash))

    # 全局时间顺序重放：所有市场的快照按 ts 排序后单次遍历（同 ts 按 market_id），
    # 这样日亏熔断和当日拉黑按真实时间先后生效。
    events = sorted((s for v in by_market.values() for s in v
                     if t_start <= s["ts"] < t_end),
                    key=lambda s: (s["ts"], s["market_id"]))
    for snap in events:
        stats["snapshots"] += 1
        ts = snap["ts"]
        day = cst_day(ts)
        market_id = snap["market_id"]
        pair = pending.get(market_id)

        if pair is not None:
            # 先结算未完成的对（用当前快照判断成交/超时）
            filled_a = pair["filled"]["A"] or leg_filled(pair["direction"], "A", pair["trig"]["A"], snap)
            filled_b = pair["filled"]["B"] or leg_filled(pair["direction"], "B", pair["trig"]["B"], snap)
            pair["filled"] = {"A": filled_a, "B": filled_b}
            timed_out = ts - pair["open_ts"] >= p["timeout"]
            if filled_a and filled_b:
                pnl = pair["locked_pnl"]
                record_pnl(pnl, ts)
                stats["completed"] += 1
                if pnl > 0:
                    stats["completed_win"] += 1
                stats["gross_pnl"] += pnl
                stats["net_pnl"] += pnl
                log_trade({"market_id": market_id, "question": pair["question"],
                           "direction": pair["direction"], "open_ts": pair["open_ts"],
                           "close_ts": ts, "trigger_sum": pair["trigger_sum"],
                           "size": pair["size"], "outcome": "completed",
                           "pnl_gross": round(pnl, 4), "fee": 0.0,
                           "pnl_net": round(pnl, 4)})
                del pending[market_id]
                continue
            if timed_out:
                if not filled_a and not filled_b:
                    stats["no_fill_cancels"] += 1
                    log_trade({"market_id": market_id, "question": pair["question"],
                               "direction": pair["direction"], "open_ts": pair["open_ts"],
                               "close_ts": ts, "trigger_sum": pair["trigger_sum"],
                               "size": pair["size"], "outcome": "no_fill_cancel",
                               "pnl_gross": 0.0, "fee": 0.0, "pnl_net": 0.0})
                else:
                    side = "A" if filled_a else "B"
                    trig = pair["trig"][side]
                    shares = pair["shares"]
                    fee_rate = pair["fee_rate"]
                    cp, how = close_price_for(pair["direction"], side, snap, trig)
                    fee = shares * fee_rate * cp * (1 - cp)
                    if pair["direction"] == "BUY_BOTH":
                        pnl_gross = shares * (cp - trig)
                    else:
                        pnl_gross = shares * (trig - cp)
                    pnl_net = pnl_gross - fee
                    record_pnl(pnl_net, ts)
                    stats["single_side_closes"] += 1
                    stats["gross_pnl"] += pnl_gross
                    stats["fees"] += fee
                    stats["net_pnl"] += pnl_net
                    if pnl_net < 0:
                        stats["single_side_loss"] += -pnl_net
                        daily_loss[day] = daily_loss.get(day, 0.0) + (-pnl_net)
                        blocked.add((day, market_id))
                    log_trade({"market_id": market_id, "question": pair["question"],
                               "direction": pair["direction"], "open_ts": pair["open_ts"],
                               "close_ts": ts, "trigger_sum": pair["trigger_sum"],
                               "size": pair["size"], "outcome": "single_side_close",
                               "closed_leg": side, "close_price": cp, "close_via": how,
                               "pnl_gross": round(pnl_gross, 4), "fee": round(fee, 4),
                               "pnl_net": round(pnl_net, 4)})
                del pending[market_id]
                continue
            # 未超时且未全成交：继续等，不开新对
            continue

        # 无未完成对：看触发（先计触发数，再看熔断/拉黑是否允许下单）
        a_ask, b_ask = snap["a_ask"], snap["b_ask"]
        a_bid, b_bid = snap["a_bid"], snap["b_bid"]
        buy_sum = (a_ask + b_ask) if (a_ask is not None and b_ask is not None) else None
        sell_sum = (a_bid + b_bid) if (a_bid is not None and b_bid is not None) else None
        direction = None
        trig_sum = None
        if buy_sum is not None and buy_sum <= p["buy_threshold"]:
            direction, trig_sum = "BUY_BOTH", buy_sum
        elif sell_sum is not None and sell_sum >= p["sell_threshold"]:
            direction, trig_sum = "SELL_BOTH", sell_sum
        if direction is None:
            continue
        stats["triggers"] += 1
        if day in cap_days or daily_loss.get(day, 0.0) >= p["daily_loss_cap"]:
            if day not in cap_days:
                cap_days.add(day)
                stats["cap_stop_days"] += 1
            continue
        if (day, market_id) in blocked:
            continue
        size = p["size"]
        if direction == "BUY_BOTH":
            shares = size / trig_sum
            locked = size / trig_sum - size
            trig = {"A": a_ask, "B": b_ask}
        else:
            shares = size / trig_sum
            locked = size - size / trig_sum
            trig = {"A": a_bid, "B": b_bid}
        pending[market_id] = {
            "direction": direction, "question": snap["question"],
            "open_ts": ts, "trigger_sum": trig_sum, "size": size,
            "shares": shares, "locked_pnl": locked, "trig": trig,
            "filled": {"A": False, "B": False}, "fee_rate": snap["fee_rate"],
        }
        stats["placed"] += 1

    for market_id, pair in pending.items():
        stats["open_at_end"] += 1
        log_trade({"market_id": market_id, "question": pair["question"],
                   "direction": pair["direction"], "open_ts": pair["open_ts"],
                   "close_ts": None, "trigger_sum": pair["trigger_sum"],
                   "size": pair["size"], "outcome": "open_at_end",
                   "pnl_gross": 0.0, "fee": 0.0, "pnl_net": 0.0})

    stats["cap_stopped"] = len(cap_days) > 0
    stats["win_rate"] = (stats["completed_win"] / stats["completed"]
                         if stats["completed"] else None)
    # 最大回撤：权益曲线峰值到谷底的最大跌幅
    mdd = 0.0
    running_peak = p["bankroll"]
    for _, c in equity:
        running_peak = max(running_peak, c)
        mdd = max(mdd, running_peak - c)
    stats["max_drawdown"] = round(mdd, 4)
    stats["final_cash"] = round(cash, 4)
    for k in ("gross_pnl", "fees", "net_pnl", "single_side_loss"):
        stats[k] = round(stats[k], 4)
    return stats, trades


def fmt_seg(name, s, thin):
    lines = [f"--- {name} ---"]
    if thin:
        lines.append("  数据太薄（快照 < 20 行）：以下数字仅供参考，不可信。")
    lines.append(f"  快照: {s['snapshots']}  触发: {s['triggers']}  下单: {s['placed']}  "
                 f"完成: {s['completed']}  胜率: {s['win_rate'] if s['win_rate'] is not None else '—'}")
    lines.append(f"  单边平仓: {s['single_side_closes']}（累计亏损 {s['single_side_loss']} U）  "
                 f"全撤: {s['no_fill_cancels']}  区间末未结: {s['open_at_end']}")
    lines.append(f"  毛利润: {s['gross_pnl']} U  手续费: {s['fees']} U  净利润: {s['net_pnl']} U")
    lines.append(f"  最大回撤: {s['max_drawdown']} U  日亏熔断天数: {s['cap_stop_days']}  "
                 f"期末虚拟资金: {s['final_cash']} U")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="双边 MM 回测：重放 history.db 的盘口快照")
    ap.add_argument("--db", default=DEFAULT_DB, help="history.db 路径，默认平台根目录")
    ap.add_argument("--from", dest="t_from", default=None, help="起始时间：epoch 或 YYYY-MM-DD[ HH:MM]（北京时间）")
    ap.add_argument("--to", dest="t_to", default=None, help="结束时间：同上")
    ap.add_argument("--market", action="append", default=[], help="只回测该市场 id，可重复")
    ap.add_argument("--buy-threshold", type=float, default=0.99)
    ap.add_argument("--sell-threshold", type=float, default=1.01)
    ap.add_argument("--size", type=float, default=200, help="每对下单 U 数")
    ap.add_argument("--bankroll", type=float, default=1000, help="每段起始虚拟资金")
    ap.add_argument("--daily-loss-cap", type=float, default=100, help="当日已实现亏损上限")
    ap.add_argument("--single-side-timeout", type=float, default=30, help="单边等待秒数")
    ap.add_argument("--split", type=float, default=0.7, help="样本内外切分比例，默认 0.7")
    args = ap.parse_args()
    if not (0 < args.split < 1):
        print("--split 必须在 0 和 1 之间")
        sys.exit(2)

    t_from = parse_time(args.t_from)
    t_to = parse_time(args.t_to)
    by_market = load_books(args.db, t_from, t_to, args.market)
    total_snaps = sum(len(v) for v in by_market.values())
    if total_snaps == 0:
        print("没有符合条件的盘口快照：检查 --from/--to/--market，或先跑平台攒数据。")
        sys.exit(1)
    all_ts = [s["ts"] for v in by_market.values() for s in v]
    t_min, t_max = min(all_ts), max(all_ts)
    span = t_max - t_min
    cut = t_min + args.split * span if span > 0 else t_max + 1

    base = {"buy_threshold": args.buy_threshold, "sell_threshold": args.sell_threshold,
            "size": args.size, "bankroll": args.bankroll,
            "daily_loss_cap": args.daily_loss_cap, "timeout": args.single_side_timeout}
    in_p = dict(base, segment="in-sample")
    out_p = dict(base, segment="out-of-sample")
    in_stats, in_trades = run_segment(by_market, t_min, cut, in_p)
    out_stats, out_trades = run_segment(by_market, cut, t_max + 1, out_p)

    total = {}
    for k in ("snapshots", "triggers", "placed", "completed", "completed_win",
              "single_side_closes", "no_fill_cancels", "open_at_end", "cap_stop_days"):
        total[k] = in_stats[k] + out_stats[k]
    for k in ("gross_pnl", "fees", "net_pnl", "single_side_loss"):
        total[k] = round(in_stats[k] + out_stats[k], 4)
    total["win_rate"] = (total["completed_win"] / total["completed"]
                         if total["completed"] else None)
    total["max_drawdown"] = round(max(in_stats["max_drawdown"], out_stats["max_drawdown"]), 4)
    total["final_cash"] = round(args.bankroll + total["net_pnl"], 4)
    total["cap_stopped"] = in_stats["cap_stopped"] or out_stats["cap_stopped"]

    # 逐笔日志（追加写，跑多次不覆盖之前的结果）
    with open(TRADE_LOG, "a") as f:
        for rec in in_trades + out_trades:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 人类可读摘要
    print("双边 MM 回测（纸面，5 秒快照分辨率）")
    print(f"区间: {datetime.fromtimestamp(t_min, tz=CST)} ~ {datetime.fromtimestamp(t_max, tz=CST)}"
          f"（北京时间），市场 {len(by_market)} 个，快照 {total_snaps} 行")
    print(f"切分: in-sample 前 {args.split:.0%}（至 {datetime.fromtimestamp(cut, tz=CST)}），"
          f"其余 out-of-sample")
    print(f"参数: 买 <= {args.buy_threshold} / 卖 >= {args.sell_threshold}，"
          f"每对 {args.size} U，总资金 {args.bankroll} U，日亏上限 {args.daily_loss_cap} U，"
          f"单边超时 {args.single_side_timeout} 秒")
    if total_snaps < 50:
        print("⚠ 数据太薄（快照 < 50 行）：以下结果不可信，先多跑平台攒数据。")
    if total["triggers"] == 0:
        print("⚠ 全程 0 触发：阈值内没有出现套利窗口，结果为空——这是数据本身的结论，不是引擎故障。")
    print(fmt_seg("in-sample", in_stats, in_stats["snapshots"] < 20))
    print(fmt_seg("out-of-sample", out_stats, out_stats["snapshots"] < 20))
    print(fmt_seg("合计", total, False))
    print(f"逐笔记录已写 {TRADE_LOG}（{len(in_trades) + len(out_trades)} 条）")
    print("注意：5 秒轮询看不到 5 秒内的价格跳动，本回测是 5 秒分辨率近似，"
          "不是 tick 级回测；完成对的锁定利润按立即到账记，会高估资金周转。")

    # 机器可读 JSON
    print(json.dumps({
        "range": {"from": t_min, "to": t_max, "cut": cut, "split": args.split},
        "markets": len(by_market), "snapshots": total_snaps,
        "params": {"buy_threshold": args.buy_threshold, "sell_threshold": args.sell_threshold,
                   "size": args.size, "bankroll": args.bankroll,
                   "daily_loss_cap": args.daily_loss_cap,
                   "single_side_timeout": args.single_side_timeout},
        "in_sample": in_stats, "out_of_sample": out_stats, "total": total,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

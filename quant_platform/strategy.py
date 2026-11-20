"""策略接口：所有策略都按这个写 —— 输入盘口状态，输出订单列表。
新策略：继承 Strategy，实现 on_book，名字注册进 STRATEGIES。"""
import time
from dataclasses import dataclass

from .nba_clock import NbaClock, et_date_str, parse_teams


@dataclass
class Order:
    market_id: str
    direction: str  # BUY_BOTH / SELL_BOTH
    pair_sum: float
    size: float


class Strategy:
    name = "base"
    tags = None  # 引擎用：有则只发现这些 tag 的市场，没有则用 config 的 tags

    def __init__(self, params=None):
        self.params = dict(params or {})

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


DEFAULT_SCALP_DIP = {
    "bid_price": 0.30,
    "target": 0.40,
    "price_stop": 0.10,          # 跌破止损价：市价平掉（taker 费），不等 Q4 出场
    "size": 30.0,
    "pregame_window_s": 60,
    "entry_cutoff_q4_clock_s": 300,   # 赛中开仓截止：Q4 比赛时钟剩 5:00
    "exit_q4_clock_s": 120,           # 出场：Q4（及加时）比赛时钟剩 2:00
    "underdog_min": 0.25,
    "underdog_max": 0.40,
    "game_filter": True,
}


class ScalpDipStrategy(Strategy):
    """篮球低吸（NBA Polymarket 比赛盘，纸面模式）。

    时点表（用户原表按篮球重设，全部参数化）：
    - T0 = 开球时间（市场 start_ts，即 Gamma 事件 startTime；注意 startDate 是
      上架时间，绝不能当 T0 用）。结算 = 终场哨响。
    - T0-60s：两队各挂 0.30 买单 30 份；任一侧成交 -> 撤另一侧，该侧挂 0.40 卖单；
      T0+60s 无成交 -> 两侧全撤，等下一场。
    - 赛中（Q1 至 Q4 比赛时钟剩 5:00）：任一队卖价 <= 0.30 就买入 30 份，
      撤另一侧挂单，该侧挂 0.40 卖单。
    - 价格触及 0.40：卖单成交，落袋 +$3.00（maker，手续费 0；成交按策略自己的
      价 0.30/0.40 记，保守口径）。
    - 跌破止损价 0.10：市价平掉（taker 费），不等 Q4 出场。回测 52 场：0.10 止损
      不杀死任何赢腿（赢腿触及 0.40 前从未跌到 0.10），救下 10 条输腿
      （否则亏整腿约 -9.00）；0.20 止损会杀死 3 条赢腿，反而更差。
    - Q4 比赛时钟剩 2:00 仍未到 0.40：撤卖单，市价平掉，不持有到终场
      （体育 taker 费 = fee_rate x 份数 x 价 x (1-价)，一般 0.05）。
    - 双边同时成交（两边卖价同时 <= 0.30）：锁利，付 2x0.30x30=18，结算收 30，+12.00。
    - 比赛筛选（默认开）：只做弱队赛前价在 0.25-0.40 的局。

    接法：Order 模型只支持双边对瞬间成交，装不下挂单/撤单/单腿，所以本策略
    采用策略自管执行 —— 自己管状态机、自己记账（ledger.open_leg/close_leg/
    lock_pair/settle_leg），on_book 永远返回 []，引擎只透传参数。
    比赛时钟走 ESPN（quant_platform/nba_clock.py），Polymarket 数据里没有。
    时钟缺失时：不新开仓，记一次 [SCALP] 时钟缺失；已持仓只按价格管理（0.40 落袋）。
    """
    name = "scalp_dip"
    tags = ["nba"]

    def __init__(self, params=None, clock=None):
        super().__init__(params)
        p = dict(DEFAULT_SCALP_DIP)
        p.update(self.params)
        self.p = p
        self.clock = clock if clock is not None else NbaClock()
        self.state = {}  # market_id -> 每场状态机

    # ---------- 小工具 ----------

    def _log(self, ctx, msg):
        log = (ctx or {}).get("log")
        line = f"[SCALP] {msg}"
        if callable(log):
            try:
                log(line)
                return
            except Exception:
                pass
        print(line, flush=True)

    def _st(self, market_id):
        st = self.state.get(market_id)
        if st is None:
            st = {"phase": "IDLE", "side": None, "leg_id": None,
                  "filter_done": False, "filter_pass": False,
                  "seen": False, "last_clock_log": 0.0}
            self.state[market_id] = st
        return st

    def _clock(self, ctx, st, teams, start_ts, now):
        """取比赛时钟。失败返回 None（调用方按时钟缺失处理，节流记日志）。"""
        if teams is None or start_ts is None:
            return None
        try:
            snap = self.clock.get_game(teams[0], teams[1], et_date_str(start_ts))
        except Exception:
            snap = None
        if snap is None and now - st["last_clock_log"] >= 300:
            st["last_clock_log"] = now
            self._log(ctx, "时钟缺失：ESPN 记分牌拉不到这场比赛，不新开仓")
        return snap

    def _try_fill(self, state, ctx, st, now, where):
        """尝试成交一边（或双边锁利）。返回 True 表示已成交/锁利。"""
        p = self.p
        a_ask, b_ask = state.get("a_ask"), state.get("b_ask")
        if a_ask is None or b_ask is None:
            return False
        ledger = ctx["ledger"]
        q = state.get("question") or state["market_id"]
        if a_ask <= p["bid_price"] and b_ask <= p["bid_price"]:
            ok, reason = ctx["risk"].check(ledger, 2 * p["size"])
            if not ok:
                self._log(ctx, f"{q} 熔断拒单（双边锁利）：{reason}")
                st["phase"] = "DONE"
                return True
            pnl = ledger.lock_pair(state["market_id"], p["bid_price"], p["size"], ts=now)
            self._log(ctx, f"{q} 双边同时成交锁利：两边各 0.30x{p['size']:.0f}，"
                           f"付 {2 * p['bid_price'] * p['size']:.2f} 结算收 {p['size']:.2f}，"
                           f"锁定 +{pnl:.2f}")
            st["phase"] = "DONE"
            return True
        side = None
        if a_ask <= p["bid_price"]:
            side = "a"
        elif b_ask <= p["bid_price"]:
            side = "b"
        if side is None:
            return False
        ok, reason = ctx["risk"].check(ledger, p["size"])
        if not ok:
            self._log(ctx, f"{q} 熔断拒单（{where}）：{reason}")
            st["phase"] = "DONE"
            return True
        leg_id = ledger.open_leg(state["market_id"], side, p["bid_price"],
                                 p["size"], ts=now)
        st["phase"] = "HOLDING"
        st["side"] = side
        st["leg_id"] = leg_id
        self._log(ctx, f"{q} {where}成交：{'A' if side == 'a' else 'B'}队 "
                       f"0.30x{p['size']:.0f}，撤另一侧，挂 0.40 卖单")
        return True

    def _apply_filter(self, state, ctx, st, now):
        """比赛筛选：弱队赛前价（两边卖价较低者）在 [underdog_min, underdog_max] 才做。
        首次在赛中才看到市场时，用当前价当代理（日志注明）。只评一次。"""
        p = self.p
        if st["filter_done"]:
            return st["filter_pass"]
        a_ask, b_ask = state.get("a_ask"), state.get("b_ask")
        if a_ask is None or b_ask is None:
            return None  # 盘口没齐，等下一 tick
        q = state.get("question") or state["market_id"]
        underdog = min(a_ask, b_ask)
        start_ts = state.get("start_ts")
        proxy = start_ts is not None and now > start_ts + p["pregame_window_s"]
        if not p["game_filter"]:
            st["filter_done"] = True
            st["filter_pass"] = True
            return True
        ok = p["underdog_min"] <= underdog <= p["underdog_max"]
        st["filter_done"] = True
        st["filter_pass"] = ok
        if ok:
            self._log(ctx, f"{q} 比赛筛选通过：弱队赛前价 {underdog:.2f}"
                           f"{'（赛中初见，用当前价代理）' if proxy else ''}")
        else:
            self._log(ctx, f"{q} 比赛筛选跳过：弱队赛前价 {underdog:.2f} 不在 "
                           f"{p['underdog_min']}-{p['underdog_max']}")
            st["phase"] = "DONE"
        return ok

    # ---------- 主入口 ----------

    def on_book(self, state, ctx):
        if state.get("tag") != "nba":
            return []
        ctx = ctx or {}
        now = ctx.get("ts") or time.time()
        mid = state["market_id"]
        st = self._st(mid)
        q = state.get("question") or mid
        p = self.p

        if st["phase"] == "DONE":
            return []

        # 首次见到这场：记一行（冒烟跑也能看到 [SCALP]），解析两队
        if not st["seen"]:
            st["seen"] = True
            teams = parse_teams(q)
            st["teams"] = teams
            start_ts = state.get("start_ts")
            if teams is None:
                self._log(ctx, f"{q} question 解析不出两队，跳过这场")
                st["phase"] = "DONE"
                return []
            if start_ts is None:
                self._log(ctx, f"{q} 没有开球时间（start_ts 缺失），跳过这场")
                st["phase"] = "DONE"
                return []
            t0 = time.strftime("%m-%d %H:%M", time.localtime(start_ts))
            self._log(ctx, f"{q} 开球 {t0}（UTC 时间戳 {int(start_ts)}），"
                           f"等待 T0-60s 挂单")
        teams = st.get("teams")
        start_ts = state.get("start_ts")
        if teams is None or start_ts is None:
            return []

        # ---- 持仓管理（先于开仓判断）----
        if st["phase"] == "HOLDING":
            snap = self._clock(ctx, st, teams, start_ts, now)
            leg_id, side = st["leg_id"], st["side"]
            # 终场：按胜负结算
            if snap is not None and snap["state"] == "final":
                won = snap.get("winner") == (teams[0] if side == "a" else teams[1])
                pnl = ctx["ledger"].settle_leg(leg_id, won, ts=now)
                self._log(ctx, f"{q} 终场结算：{'A' if side == 'a' else 'B'}队"
                               f"{'赢' if won else '输'}，盈亏 {pnl:+.2f}")
                st["phase"] = "DONE"
                return []
            # 0.40 落袋（maker，手续费 0）
            bid = state.get("a_bid") if side == "a" else state.get("b_bid")
            if bid is not None and bid >= p["target"]:
                pnl = ctx["ledger"].close_leg(leg_id, p["target"], 0.0, ts=now)
                self._log(ctx, f"{q} 触及 0.40 落袋：{'A' if side == 'a' else 'B'}队 "
                               f"0.30 买入 0.40 卖出，盈亏 {pnl:+.2f}")
                st["phase"] = "DONE"
                return []
            # 跌破止损价：市价平掉（taker 费），不等 Q4 出场
            if bid is not None and p.get("price_stop") is not None \
                    and bid <= p["price_stop"]:
                fee_rate = state.get("fee_rate") or 0.05
                fee = fee_rate * p["size"] * bid * (1 - bid)
                pnl = ctx["ledger"].close_leg(leg_id, bid, fee, ts=now)
                self._log(ctx, f"{q} 跌破止损 {p['price_stop']:.2f}，市价 "
                               f"{bid:.2f} 平掉（taker 费 {fee:.2f}），盈亏 {pnl:+.2f}")
                st["phase"] = "DONE"
                return []
            # Q4（及加时）比赛时钟剩 2:00：市价平掉
            if snap is not None and snap["state"] == "in" \
                    and snap["period"] >= 4 and snap["clock_s"] is not None \
                    and snap["clock_s"] <= p["exit_q4_clock_s"]:
                if bid is None:
                    self._log(ctx, f"{q} Q4 剩 {snap['clock_s']:.0f}s 该出场但无买价，"
                                   "等下一 tick（终场结算兜底）")
                    return []
                fee_rate = state.get("fee_rate") or 0.05
                fee = fee_rate * p["size"] * bid * (1 - bid)
                pnl = ctx["ledger"].close_leg(leg_id, bid, fee, ts=now)
                self._log(ctx, f"{q} Q4 剩 {snap['clock_s']:.0f}s 未到 0.40，市价 "
                               f"{bid:.2f} 平掉（taker 费 {fee:.2f}），盈亏 {pnl:+.2f}")
                st["phase"] = "DONE"
                return []
            return []

        # ---- 开仓 ----
        # 赛前窗口 [T0-60s, T0+60s]
        if start_ts - p["pregame_window_s"] <= now <= start_ts + p["pregame_window_s"]:
            if st["phase"] == "IDLE":
                st["phase"] = "ARMED"
            if self._apply_filter(state, ctx, st, now) is not True:
                return []
            # 赛前开仓也要先确认时钟可达：时钟缺失时不新开仓（否则 Q4 出场没法管）
            if self._clock(ctx, st, teams, start_ts, now) is None:
                return []
            if self._try_fill(state, ctx, st, now, "赛前"):
                return []
            return []  # 仍在窗口内等成交；窗口过后下个 tick 走赛中逻辑

        # 赛前窗口已过（无成交）：全撤，等下一场——但赛中低吸入口还开着，
        # 所以只在比赛还没开打时标记 DONE，开打后走赛中逻辑
        if now < start_ts - p["pregame_window_s"]:
            return []  # 还没到挂单时间
        if st["phase"] == "ARMED":
            self._log(ctx, f"{q} T0+{p['pregame_window_s']:.0f}s 无成交，两侧全撤，"
                           "等赛中低吸机会")
            st["phase"] = "IDLE"

        # 赛中低吸：Q1 至 Q4 比赛时钟剩 5:00
        snap = self._clock(ctx, st, teams, start_ts, now)
        if snap is None:
            return []  # 时钟缺失：不新开仓
        if snap["state"] == "final":
            st["phase"] = "DONE"
            return []
        if snap["state"] != "in":
            return []  # 还没开打（开球推迟）：等
        if snap["period"] > 4:
            return []  # 加时不开新仓
        if snap["period"] == 4 and snap["clock_s"] is not None \
                and snap["clock_s"] <= p["entry_cutoff_q4_clock_s"]:
            return []  # Q4 剩 5:00 内不开新仓
        if self._apply_filter(state, ctx, st, now) is not True:
            return []
        self._try_fill(state, ctx, st, now, "赛中低吸")
        return []


STRATEGIES = {"observe_only": ObserveOnlyStrategy, "scalp_dip": ScalpDipStrategy}

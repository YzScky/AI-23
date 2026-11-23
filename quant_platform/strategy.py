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


DEFAULT_PREGAME_2030 = {
    "size_u": 30.0,            # 每腿 30U（份数 = 30 / 买入价）
    "lo": 0.20,                # 买入价下限（含）
    "hi": 0.30,                # 买入价上限（含）
    "target": 0.50,            # 落袋价（挂 0.50 卖单，maker，费 0）
    "entry_window_s": 86400,   # 买入窗口：T0-24h 至 T0（只在开赛前买）
    "exit_q3_clock_s": 360,    # 出场：第三节中段 = Q3 比赛时钟剩 6:00
}


class Pregame2030Strategy(Strategy):
    """赛前低吸（NBA Polymarket 比赛盘，纸面模式）。

    规则（用户 2026-11-23 确认）：
    - 每天扫当天所有比赛，只买赛前价格在 [0.20, 0.30]（含）那一边，其他不买。
    - 买入窗口：开赛前 1 天都可以（T0-24h ~ T0）；窗口内卖价第一次进区间就
      市价买入 30U（份数 = 30 / 买入价，taker 费 = fee_rate x 份数 x 价 x (1-价)）。
      每场最多一腿。
    - 买入后等开赛；盘中第一次触及 0.50 就全部卖出（挂 0.50 卖单，maker，费 0）。
    - 到第三节中段（Q3 比赛时钟剩 6:00）还没触及 0.50，就按当时买价市价全部
      卖出（taker 费）。无止损。Q3 中段时钟线缺失时用 Q4 开始兜底。
    - 终场仍有持仓（时钟全程缺失的极端情况）才按胜负结算；买入费计入结算盈亏。

    与 scalp_dip 的区别：只做赛前、不做赛中低吸；买入价是区间不是固定 0.30；
    份数按 30U 算不是固定 30 份；出场在 Q3 中段（不是 Q4 剩 2:00）；
    没有止损。买入是市价买（taker 费），不是挂单（maker）。

    回测（backtest_nba_v2.py，2026-11-23，74 场，止盈 0.50 + Q3 中段出场）：
    入场 29 腿，触 0.50 落袋 12 次（+281.97），Q3 中段市价出 17 次（-192.31），
    Q4 兜底 0 次，胜率 51.7%，总盈亏 +89.66，每腿平均 +3.09。42 场赛前价格没进
    区间所以不买；29 腿里 21 腿是开赛 12 小时前买的——1 天窗口是入场的前提。
    （67 场时是 +122.55；今天 7 场新增 4 腿，0 落袋，-32.89，总 +89.66。
    同止盈 0.50 但 Q4 开始出场、67 场时是 +145.79：Q3 中段出场少落袋 2 腿
    ——PHX-DEN +37.01→+22.41、DET-SAS +25.50→-12.11，Q4 的后半段反弹被放弃；
    但提前出场的腿平均少亏。仍为正。）

    接法与 scalp_dip 一样：策略自管执行，on_book 永远返回 []。
    时钟走 ESPN（quant_platform/nba_clock.py）；买入那一下要求时钟可达
    （否则 Q3 中段出场没法管），时钟缺失时这一下先不买、继续等窗口，
    不像 scalp_dip 那样直接放弃；持仓后只按价格管理（触及目标落袋）。
    """
    name = "pregame_2030"
    tags = ["nba"]

    def __init__(self, params=None, clock=None):
        super().__init__(params)
        p = dict(DEFAULT_PREGAME_2030)
        p.update(self.params)
        self.p = p
        self.clock = clock if clock is not None else NbaClock()
        self.state = {}  # market_id -> 每场状态机

    # ---------- 小工具 ----------

    def _log(self, ctx, msg):
        log = (ctx or {}).get("log")
        line = f"[PG2030] {msg}"
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
                  "entry_price": None, "shares": None, "fee_buy": 0.0,
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
            self._log(ctx, "时钟缺失：ESPN 记分牌拉不到这场比赛，暂不买入")
        return snap

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

        # 首次见到这场：记一行，解析两队
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
            self._log(ctx, f"{q} 开球 {t0}，等待 T0-24h 内 0.20-0.30 买入 30U")
        teams = st.get("teams")
        start_ts = state.get("start_ts")
        if teams is None or start_ts is None:
            return []

        # ---- 持仓管理（先于开仓判断）----
        if st["phase"] == "HOLDING":
            snap = self._clock(ctx, st, teams, start_ts, now)
            leg_id, side = st["leg_id"], st["side"]
            fee_buy, shares = st["fee_buy"], st["shares"]
            fee_rate = state.get("fee_rate") or 0.05
            bid = state.get("a_bid") if side == "a" else state.get("b_bid")
            # 终场：按胜负结算（买入费计入）
            if snap is not None and snap["state"] == "final":
                won = snap.get("winner") == (teams[0] if side == "a" else teams[1])
                pnl = ctx["ledger"].close_leg(leg_id, 1.0 if won else 0.0,
                                              fee_buy, ts=now)
                self._log(ctx, f"{q} 终场结算：{'A' if side == 'a' else 'B'}队"
                               f"{'赢' if won else '输'}，盈亏 {pnl:+.2f}")
                st["phase"] = "DONE"
                return []
            # 触及目标落袋（maker，费 0；买入费已计）
            if bid is not None and bid >= p["target"]:
                pnl = ctx["ledger"].close_leg(leg_id, p["target"], fee_buy, ts=now)
                self._log(ctx, f"{q} 触及 {p['target']:.2f} 落袋：{'A' if side == 'a' else 'B'}队 "
                               f"{st['entry_price']:.2f} 买入 {p['target']:.2f} 卖出，盈亏 {pnl:+.2f}")
                st["phase"] = "DONE"
                return []
            # Q3 中段（比赛时钟剩 6:00）：市价平掉（taker 费 + 买入费）。
            # 时钟跳过 Q3 中段直接到 Q4（漏轮询）时也出场——Q4 开始是兜底。
            if snap is not None and snap["state"] == "in" \
                    and snap["period"] is not None \
                    and ((snap["period"] == 3 and snap["clock_s"] is not None
                          and snap["clock_s"] <= p["exit_q3_clock_s"])
                         or snap["period"] >= 4):
                if bid is None:
                    self._log(ctx, f"{q} Q3 中段该出场但无买价，"
                                   "等下一 tick（终场结算兜底）")
                    return []
                fee_sell = fee_rate * shares * bid * (1 - bid)
                pnl = ctx["ledger"].close_leg(leg_id, bid, fee_sell + fee_buy,
                                              ts=now)
                where = "Q3 中段" if snap["period"] == 3 else "Q4 开始（Q3 中段漏过）"
                self._log(ctx, f"{q} {where}未到 {p['target']:.2f}，市价 {bid:.2f} 平掉"
                               f"（taker 费 {fee_sell:.2f} + 买入费 {fee_buy:.2f}），"
                               f"盈亏 {pnl:+.2f}")
                st["phase"] = "DONE"
                return []
            return []

        # ---- 开仓（只在赛前窗口 [T0-24h, T0) 内）----
        if now < start_ts - p["entry_window_s"]:
            return []  # 还没到买入窗口
        if now >= start_ts:
            self._log(ctx, f"{q} 已过赛前窗口，未成交，不做这场")
            st["phase"] = "DONE"
            return []
        # 窗口内：时钟可达才买（否则 Q4 出场没法管）；缺失就等下一 tick，不放弃
        if self._clock(ctx, st, teams, start_ts, now) is None:
            return []
        a_ask, b_ask = state.get("a_ask"), state.get("b_ask")
        if a_ask is None or b_ask is None:
            return []
        side, ask, both = None, None, False
        if p["lo"] <= a_ask <= p["hi"]:
            side, ask = "a", a_ask
            both = p["lo"] <= b_ask <= p["hi"]
        elif p["lo"] <= b_ask <= p["hi"]:
            side, ask = "b", b_ask
        if side is None:
            return []  # 价格不在区间，继续等
        fee_rate = state.get("fee_rate") or 0.05
        shares = p["size_u"] / ask
        fee_buy = fee_rate * shares * ask * (1 - ask)
        ledger = ctx["ledger"]
        ok, reason = ctx["risk"].check(ledger, p["size_u"])
        if not ok:
            self._log(ctx, f"{q} 熔断拒单（赛前买入）：{reason}")
            st["phase"] = "DONE"
            return []
        leg_id = ledger.open_leg(mid, side, ask, shares, ts=now)
        st["phase"] = "HOLDING"
        st["side"] = side
        st["leg_id"] = leg_id
        st["entry_price"] = ask
        st["shares"] = shares
        st["fee_buy"] = fee_buy
        self._log(ctx, f"{q} 赛前买入：{'A' if side == 'a' else 'B'}队 "
                       f"{ask:.2f}x{shares:.1f} 份（30U，taker 费 {fee_buy:.2f}）"
                       f"{'（两边都在区间，先买 A）' if both else ''}，"
                       f"等开赛，触 {p['target']:.2f} 落袋，否则 Q3 中段市价出")
        return []


STRATEGIES = {"observe_only": ObserveOnlyStrategy, "scalp_dip": ScalpDipStrategy,
              "pregame_2030": Pregame2030Strategy}

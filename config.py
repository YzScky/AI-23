# Polymarket 量化平台 · 配置
# 策略以后再定；钱包以后再接。现在只跑纸面：数据 -> 引擎 -> 账本 -> 风控，整条链跑通。
CONFIG = {
    "mode": "paper",          # paper | live（实盘以后再开）
    "live_trading": False,    # 实盘总开关：默认关。改 true 之前必须配好钱包 + API 凭证 + 确认参数
    "markets": {
        "tags": ["nba", "nfl", "soccer", "tennis", "crypto", "politics", "tech"],
        "min_liquidity": 1000,
        "max_markets": 20,
    },
    "risk": {
        "total_capital": 1000.0,
        "max_pair_size": 200.0,
        "daily_loss_cap": 100.0,
    },
    "strategy": {
        "name": "observe_only",  # 策略以后再定；新策略接 quant_platform/strategy.py 的 Strategy 接口
        # 篮球低吸 scalp_dip（NBA 比赛盘，纸面模式）：T0=开球时间（市场 start_ts，
        # 即 Gamma 事件 startTime；startDate 是上架时间，不能当 T0）
        "scalp_dip": {
            "bid_price": 0.30,              # 挂单买入价（两队各 30 份）
            "target": 0.40,                 # 卖单落袋价（maker，手续费 0）
            "size": 30.0,                   # 每腿份数
            "pregame_window_s": 60,         # 赛前挂单窗口：T0-60s 至 T0+60s
            "entry_cutoff_q4_clock_s": 300,  # 赛中开仓截止：Q4 比赛时钟剩 5:00
            "exit_q4_clock_s": 120,         # 出场：Q4 比赛时钟剩 2:00 市价平掉
            "underdog_min": 0.25,           # 比赛筛选：弱队赛前价下限
            "underdog_max": 0.40,           # 比赛筛选：弱队赛前价上限
            "game_filter": True,            # 比赛筛选开关
        },
    },
    "history": True,      # 历史数据存本地 history.db（SQLite）；run.py --no-history 可关
    # 合约板块（币安期货）：连接 + 账户 + 下单管道。策略以后再定，本节不含任何信号。
    "binance": {
        "live_trading": False,  # 合约实盘总开关：默认关（和上面 Polymarket 的开关分开）
        "capital": 200.0,       # 合约本金（纸面）
        "margin_per_trade": 10.0,  # 每单保证金 10U
        "leverage": 10,         # 杠杆 10x
        "stop_pct": 0.05,       # 止损 5%
        "take_pct": 0.60,       # 止盈 60%
        "long_only": True,      # 只做多，不开空
        "daily_loss_cap": 50.0,    # 日亏上限 50U
        "max_total_loss": 500.0,   # 累计亏 500U 熔断
        "fee_rate": 0.0005,     # 每笔 0.05%
        "symbols": ["BTCUSDT"],
    },
}

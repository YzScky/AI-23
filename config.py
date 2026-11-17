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
    },
    "history": True,      # 历史数据存本地 history.db（SQLite）；run.py --no-history 可关
}

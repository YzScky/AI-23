# Polymarket 量化平台（Mac 版，纸面模式）

## 现在是什么状态
- 策略：`observe_only`（占位，只观察不下单，策略按你的决定放最后）；另有篮球低吸 `scalp_dip`（见下），默认不开，看板里选
- 钱包：没接。先跑 `link_wallet.py` 链接（只读验证）。实盘开关 `live_trading: false`，强行开会直接报错停住；下单默认 dry-run，实盘要 `--live` + `--confirm` + 开关三重确认
- 跑通的是：Gamma 发现市场 → CLOB REST 种子盘口 → CLOB WebSocket 实时盘口 → 引擎算双边和 → 纸面账本 + 风控；钱包链接走官方 py-clob-client-v2（L1 派生凭证 + L2 查余额/挂单/下单）

## 策略：篮球低吸 scalp_dip（NBA 比赛盘，纸面模式）
- 时点表：T0=开球时间（Gamma 事件 `startTime`；注意市场 `startDate` 是上架时间，不能当 T0）→ T0-60s 两队各挂 0.30 买单 30 份 → 任一侧成交撤另一侧、该侧挂 0.40 卖单 → T0+60s 无成交全撤 → 赛中（Q1 至 Q4 比赛时钟剩 5:00）任一队卖价 ≤0.30 低吸 30 份 → 触及 0.40 落袋 +$3.00 → Q4 比赛时钟剩 2:00 仍未到 0.40 就市价平掉，不持有到终场。
- 双边同时成交锁利 +$12.00；比赛筛选默认开（只做弱队赛前价 0.25–0.40 的局）；日亏熔断沿用 `risk.daily_loss_cap`。
- 比赛时钟走 ESPN 公开记分牌（`quant_platform/nba_clock.py`，无需 key，15 秒节流）；时钟缺失时不新开仓。终场按胜负结算（赢 1.00 / 输 0.00），加时不特殊处理。
- 参数全在 `config.py` 的 `strategy.scalp_dip`（bid_price/target/size/pregame_window_s/entry_cutoff_q4_clock_s/exit_q4_clock_s/underdog_min/underdog_max/game_filter）。
- 看板里选：控制台预测区「策略管理」点「切换到此策略（模拟）」（引擎在跑时一键重启切换，纸面账本会重置）；或调 `POST /api/strategy {"name": "scalp_dip", "restart": true}`。
- 终端跑：`.venv/bin/python run.py --duration 28800 --markets 20 --poll 5`（一晚比赛 8 小时；看板启动 scalp_dip 时自动用 28800）。日志看 `platform.log` 里 `[SCALP]` 开头的行。
- 状态：已回测（见下，2026-11-20，过去 7 天 52 场）。策略自管执行（自己管状态机、自己记账，`on_book` 永远返回 `[]`），不走 Order 双边对模型。单测：`python3 test_scalp.py`（10 项，假时钟 + 合成盘口，不碰网络）。

## 回测：scalp_dip（NBA，过去打完的比赛）
- 命令：`python3 backtest_nba.py`（过去 7 天全量，先拉数据再重放；结果写 `nba_backtest_results.json`）；`--games 5` 最近 5 场小样本；`--fetch-only` 只拉数据；`--no-fetch` 只用缓存重放。离线单测：`python3 test_backtest_nba.py`（3 项，合成两场假比赛，不碰网络）。
- 数据源（只读）：ESPN 记分牌（按美东日期找已打完的比赛）→ Gamma slug（`nba-<客>-<主>-<美东日期>`，moneyline 市场）→ CLOB `/prices-history`（两边 token 价格历史；注意端点带连字符，`/prices/history` 是 404）→ ESPN summary plays（时钟线）。原始数据缓存到 `nba_backtest_data/`，重跑不再重复拉。
- 粒度：开球 <24h 前 1 分钟，<7 天 5 分钟，更老 10 分钟。价格序列每时间点只有一个价，回测里同时当 bid 和 ask 用（忽略点差，会轻微高估成交率）；粗粒度会漏掉短暂触及，触及概率是下限。
- 2026-11-20 回测结果（过去 7 天，52 场，0 场跳过）：入场 24 腿（赛前 6 / 赛中低吸 18），摸到 0.40 落袋 13 次（+39.00），Q4 剩 2:00 市价出 8 次（-67.40，出场价基本是 0.00–0.09），终场结算 1 赢 2 输（+3.00），双边锁利 0 次。**关键数 P(0.30 成交 → Q4 剩 2:00 前摸到 0.40) = 0.5417（13/24），总盈亏 -25.38，每场平均 -0.49，每成交腿平均 -1.06，最大单场亏损 -9.00。** 结论：触及概率过半，但盈亏不对称（赢一腿 +3.00，输一腿平均 -8.43），期望仍是负的。

## Mac 上跑（三步）
1. 双击 `install.command`（第一次装依赖，装一次就行）
2. 双击 `start.command`（跑 10 分钟纸面）
3. 看终端输出：`[HB]` 每 10 秒一行（WS 更新数/秒、最好买两边/卖两边、虚拟资金、交易数）

手动跑：`.venv/bin/python run.py --duration 600 --markets 20`

## 历史数据（本地 SQLite）
- 跑平台时默认记录到 `history.db`（SQLite，标准库，不用装数据库），只在 Mac 本地，**不打包进 zip、不上传**。
- 存两张表（Polymarket 没有资金费率，不存）：
  - `books(ts, market_id, question, a_bid, a_ask, b_bid, b_ask, fee_rate)`：盘口快照，每个市场最多每 5 秒一行；缺 bid/ask 存 NULL。价格历史 = 中价 `(bid+ask)/2` 随时间变化。
  - `trades(ts, market_id, price, size, side)`：公开成交，每 60 秒从 data-api v2 拉一次（CLOB `/trades` 要鉴权，公开成交走 data-api v2 按 condition_id 查），`(market_id, ts, price, size)` 重复自动去重。
- 查数据：
  ```
  .venv/bin/python history.py --stats                       # 各表行数、DB 大小、各市场最新盘口
  .venv/bin/python history.py --market <market_id> --limit 5 # 该市场最近 5 行盘口
  ```
- 关掉记录：`config.py` 里 `history: False`，或跑 `run.py --no-history`。

## 回测（重放 history.db 的盘口快照）
```
.venv/bin/python backtest.py                                  # 默认阈值 0.99/1.01
.venv/bin/python backtest.py --buy-threshold 1.02             # 放宽触发，验证引擎真会下单
.venv/bin/python backtest.py --from 2026-11-16 --to 2026-11-17 --market 663583
```
- 策略：双边 MM（buy_both <= 0.99 开 BUY 对，sell_both >= 1.01 开 SELL 对），
  下单量按价格比例分（payoff 相等），每市场同时最多一对，maker 完成对手续费 0。
- 成交模型：一条腿只在**下一个快照**仍显示该边价格不差于触发价时才算成交；
  单边超时（默认 30 秒，按快照时间）后按当前市价平掉已成交腿（taker，扣该市场
  fee_rate），两条腿都没成交则全撤；单边出过亏损的市场当天不再进；日亏到上限后
  当天不再开新对（已开的对照常结算）。北京时间日。
- 样本内外：按时间切前 70% in-sample、后 30% out-of-sample，同一套参数各跑一遍，
  分别报告再汇总（`--split` 可改比例）。这是防过拟合的检查。
- 输出：stdout 人类摘要 + JSON；逐笔记录写 `backtest_trades.jsonl`（每对一行的
  市场/方向/触发和/盈亏/手续费）。快照 < 50 行或 0 触发时明说数据太薄 / 无触发，不编结果。
- 诚实限制：Mac 上 5 秒轮询看不到 5 秒内的价格跳动，本回测是 5 秒分辨率近似，
  不是 tick 级回测；完成对的锁定利润按立即到账记，会高估资金周转。

## 第六步 看板（仪表盘 + 守护 + 日志轮转）
- 启动看板（只用标准库，不用装依赖）：
  ```
  ./dashboard.command
  ```
  打开 http://127.0.0.1:8931（只限本机访问，别人打不开；Ctrl+C 停看板）。
  看板本身不下单、不连钱包，只是把平台的状态、数据、回测变成页面。
- 页面上有：
  - 状态卡：运行/停止、更新速度、虚拟资金、当日亏损、交易数、最好买两边/卖两边（每 5 秒自动刷新）
  - 市场盘口表：各市场最新盘口 + 买两边/卖两边；买 ≤ 0.99 或卖 ≥ 1.01 的行会高亮（只是触发阈值，不代表能成交）
  - 启动平台 / 停止平台：启动后跑 1 小时（轮询 5 秒，20 个市场），数据写入 history.db；停止会杀掉平台进程
  - 跑回测：填阈值、每对金额、总资金、日亏上限，点一下就跑 `backtest.py`，结果直接显示在页面上
  - 运行日志：最近 100 行 platform.log
- 接口（看板自己用，浏览器里也能直接看）：
  - `GET /api/status`：运行状态 + 最新心跳 + 各市场最新盘口 + 数据流健康（feed）
  - `GET /api/history`：history.db 行数 + 各市场最新盘口
  - `GET /api/logs`：最近 100 行日志
  - `POST /api/platform/start`：后台启动 run.py（1 小时，轮询 5 秒），PID 记 platform.pid
  - `POST /api/platform/stop`：停掉平台进程
  - `POST /api/backtest`：JSON 参数 `{buy_threshold, sell_threshold, size, bankroll, daily_loss_cap}`，返回回测 JSON 汇总
  - history.db / platform.log / platform.pid 不存在时，各接口返回空状态，不会崩。
- 开机自启 + 崩了自动重启（launchd 守护，手动装一次就行）：
  ```
  cp com.user.polymarket-platform.plist ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/com.user.polymarket-platform.plist
  ```
  停用：`launchctl unload ~/Library/LaunchAgents/com.user.polymarket-platform.plist`
  注意：plist 里写的是 `/Users/qinzimu/Downloads/polymarket-platform`，如果你的平台装在别处，
  先把 plist 里的三处路径改成实际位置再装。start.command 每轮跑 600 秒后正常退出，
  KeepAlive 会立刻再拉起一轮，所以实际是 10 分钟一轮、不间断地跑。
- 日志轮转：`platform.log` 超过 5 MB 时自动轮转，保留最近 5 个归档（platform.log.1 ~ .5）。
  看板启动时自动做一次；也可以手动跑：
  ```
  python3 rotate_logs.py
  ```

## 第 4a 步 钱包链接（只读验证，不下单）

先说结论（以 py-clob-client-v2 1.2.2 源码 + 官方文档为准，v1 已归档停更，绝不用）：
- 认证分两层：L1 钱包私钥 EIP-712 签名 → 派生 API 凭证；L2 API 凭证 HMAC → 查余额/挂单/下单/撤单。
- **私钥运行时必须有**：L2 请求头要带由私钥派生的钱包地址，官方客户端没私钥就拒绝
  查余额、查挂单——不只是下单才要。没有网站页面可以点一点就生成 API 凭证，
  官方路径就是用钱包签名派生。
- 凭证只放 Mac 本地 `.env`（0600），绝不发聊天、不进 git、不进日志。

链接步骤（Mac 上）：
```
./install.command                                  # 装 py-clob-client-v2
.venv/bin/python link_wallet.py                    # 交互式：选路、填凭证、验证
.venv/bin/python link_wallet.py --check            # 只验证当前 .env 的链接状态
.venv/bin/python link_wallet.py --derive           # 用 .env 里的 PK 派生 API 凭证并写入
```
- 选 1：你已有 API 凭证（apiKey / secret / passphrase），逐个粘贴（输入不回显）。
- 选 2：没有凭证，用钱包私钥派生：私钥只在内存里签名，绝不打印；派生完问你
  是否把私钥也存进 .env——存（输 yes）才能让看板查余额、下单；不存的话，
  查余额/挂单/下单都会报缺少 PK，直到你改主意再跑一次。
- 链接成功会显示：已链接 + 掩码地址（0x1234...abcd）+ pUSD 余额 + 未成交挂单数。
- 看板上也有钱包卡（http://127.0.0.1:8931），显示同样的只读状态；未链接时显示原因。
- `.env` 变量名按官方 .env.example：`PK`、`CLOB_API_KEY`、`CLOB_SECRET`、
  `CLOB_PASS_PHRASE`、`CLOB_API_URL`（默认 https://clob.polymarket.com）、
  `POLYMARKET_SIGNATURE_TYPE`（0=EOA 普通钱包默认；1=POLY_PROXY；
  2=POLY_GNOSIS_SAFE；3=POLY_1271，2026-05-04 后建的 Deposit Wallet）。
  注意官方叫 `CLOB_SECRET`，不是 `CLOB_API_SECRET`。
- 余额是 CLOB 抵押物 pUSD：接口返回 6 位定点整数（'125500000' → 125.5），已换算显示。

## 下单（默认 dry-run，实盘多重锁）

```
.venv/bin/python place_order.py --token <token_id> --side BUY --price 0.40 --size 100
```
- 默认 dry-run：只校验参数 + 风控上限 + 查公开盘口，**什么都不发**，返回模拟结果。
- 发真单要同时满足四个条件，缺一个都不发：
  1. 命令行同时带 `--live` 和 `--confirm`
  2. `config.py` 里 `live_trading=true`（默认 False）
  3. 风控上限通过：单笔名义金额（价×量）≤ `max_pair_size`（默认 200 U）；
     当日已实现亏损 < `daily_loss_cap`（默认 100 U）
  4. 钱包已链接（.env 凭证齐全）
- 输出开头大字标明【DRY-RUN 模拟】或【LIVE 实盘】。每一次尝试（模拟/实盘/撤单）
  都追加记进 `orders.log`（只记白名单字段：时间、种类、token、方向、价格、数量、
  结果、订单号；绝不记凭证）。
- 撤单、查单（命令行暂不提供入口时用 Python 调 `ClobLink().cancel_order(id)` /
  `get_order(id)`；查单是只读，实盘开关不影响；撤单同样受实盘开关管）。
- **实盘现在还是关的**：策略、钱包、实盘参数都要你亲口确认后才开。策略按你的决定
  放最后，第四步先把接口做到位。

## 数据接入（统一数据流，给策略用）

`quant_platform/data_feed.py` 把行情和账户拼成一份快照，策略以后直接吃它，
不用自己再拼。只读，不下单。
- 快照形状：`{ts, markets: [...], account: {...}, health: {...}}`；每市场一行：
  `market_id / question / yes_bid / yes_ask / no_bid / no_ask / buy_both
  （两边都买的成本）/ sell_both（两边都卖的收入）/ spread（sell_both - buy_both，
  通常为负）/ fee_rate / as_of`；账户是 `linked / wallet_address（已掩码）/
  pusd_balance / open_orders / error / as_of`。
- 行情来源：默认读 `history.db` 里每个市场最新一行盘口；策略在进程内用时
  可传 `market_source` 回调（返回引擎 feed 那种 state dict 列表）直连实时行情。
- 账户节流：`link_status()` 最多 30 秒实读一次，两次之间用缓存值。
  没配 `.env` / 没装 v2 客户端时账户部分显示未链接，不崩。
- 账户快照落盘：`history.db` 新增 `account_snapshots(ts, pusd_balance,
  open_orders)` 表（books/trades 两张表不动）；已联通时才记，最多 60 秒一次，
  或余额/挂单数变了立刻记。重启后先用库里最新一行做基准，不重复记。
- 命令行（在平台目录跑）：
  ```
  .venv/bin/python -m quant_platform.data_feed --once        # 打印一份快照（只读探针，不写盘）
  .venv/bin/python -m quant_platform.data_feed --watch 10    # 每 10 秒打印一份（会按规则落盘账户快照）
  ```
  `--once` 不建文件、不写盘，`history.db` 不存在时市场为空。`--watch` 是数据流
  的记录进程，Ctrl+C 停；引擎本身不写账户快照，策略步接手后由策略在进程内调
  `DataFeed.build_snapshot()`。
- 看板 `GET /api/status` 多了一个 `feed` 字段：`last_market_ts`（最新盘口时间）、
  `last_account_refresh_ts`（最新账户快照时间）、`account_linked`（10 分钟内有
  账户快照即为真）。页面没改，只加了接口字段。

## 文件
- `config.py`：配置（模式、市场、风控上限、策略名）
- `quant_platform/feed.py`：行情（Gamma + CLOB WS）
- `quant_platform/engine.py`：引擎
- `quant_platform/ledger.py`：纸面账本
- `quant_platform/risk.py`：风控（单笔上限、日亏熔断、kill switch）
- `quant_platform/strategy.py`：策略接口 + 占位策略
- `quant_platform/storage.py`：历史数据（SQLite，`history.db`；books/trades +
  account_snapshots）
- `quant_platform/data_feed.py`：统一数据流（行情 + 账户快照，只读，不下单；
  `--once` / `--watch N`）
- `quant_platform/clob_client.py`：CLOB v2 链接 + 只读查询 + 下单（默认 dry-run，
  实盘开关 + 风控上限双重锁；凭证只从本地 .env 读）
- `link_wallet.py`：Mac 本地交互式钱包链接（填凭证 / 用私钥派生凭证 / 验证）
- `place_order.py`：下单命令行（默认 dry-run；实盘要 --live + --confirm + 开关）
- `orders.log`：每次下单/撤单尝试的记录（只记白名单字段，不记凭证）
- `.env`：本地凭证（0600，只放 Mac，不打包、不进 git）
- `.env.example`：凭证模板（变量名按 v2 官方 .env.example）
- `history.py`：查历史数据（`--stats` / `--market <id>`）
- `backtest.py`：回测引擎（重放盘口快照，样本内外切分，逐笔写 `backtest_trades.jsonl`）
- `dashboard.py`：看板后端（标准库 only，127.0.0.1:8931，状态/启停/回测接口）
- `dashboard.html`：看板页面（中文，状态卡 + 盘口表 + 回测 + 日志，5 秒自动刷新）
- `dashboard.command`：启动看板
- `rotate_logs.py`：日志轮转（platform.log 超 5 MB 轮转，保留 5 个归档）
- `com.user.polymarket-platform.plist`：launchd 守护模板（登录自启 + 崩了重启，手动安装）
- `platform.pid`：看板启动的平台进程 PID（运行时自动生成，不打包）
- `platform.log`：运行日志
- `history.db`：本地历史库（运行后自动生成，不打包）
- `.env.example`：实盘凭证模板（以后填，只放本地）

## 安全红线
- 私钥、助记词绝不进代码、绝不发聊天
- API 凭证只放 Mac 本地 `.env`，不上传、不分享
- 实盘要开，必须同时满足：钱包接好 + 凭证配好 + 参数（每单/总资金/日亏上限）你亲口确认

## 第七步 合约（Binance 期货）——聚合平台第二个板块

看板现在是两个板块：**预测**（Polymarket，上面）+ **合约**（Binance 期货，
下面）。合约这边只建了连接 + 账户 + 下单管道，策略以后再定，本模块不含
任何策略信号。

### 先测通不通（不用凭证，先跑这个）
```bash
.venv/bin/python binance_link.py --test
```
测两样：① fapi.binance.com 的 REST 现价通不通；② wss://fstream.binance.com
的 WS 标记价流通不通。REST 不通就别填凭证——填了也连不上，先换网络/VPN
出口。WS 不通不影响下单和查账户（那两样走 REST），只影响看板里的实时价，
看板会降级成手动刷新取价。

### 接 API（Mac 本地交互式）
```bash
.venv/bin/python binance_link.py           # 填 Key/Secret、验证
.venv/bin/python binance_link.py --check   # 只验证当前 .env 的链接状态
```
开 Key 时必须勾 Enable Futures（期货权限），必须不勾 Enable Withdrawals
（提现），建议再勾 IP 白名单。凭证只进 Mac 本地 `.env`（0600），和
Polymarket 的凭证共存一个文件，绝不打印、绝不外发。

### 下单（默认 dry-run，不发真单）
```bash
.venv/bin/python binance_order.py --symbol BTCUSDT --side BUY
.venv/bin/python binance_order.py --symbol BTCUSDT --side BUY --live --confirm
.venv/bin/python binance_order.py --symbol BTCUSDT --side SELL --close   # 平多
```
纸面默认：保证金 10U，杠杆 10x，止损 5%，止盈 60%，只做多，日亏上限 50U。
实盘要三重确认：`--live` + `--confirm` + config.py 里 `binance.live_trading=true`
（默认 False）。进场是市价单，随后自动挂止损市价单（STOP_MARKET）和止盈
市价单（TAKE_PROFIT_MARKET），都是 closePosition；止损/止盈有挂不上的，
输出里会明说，主单已成交，必须人工检查挂单。每一次尝试都记进
`binance_orders.log`（只记白名单字段，不记凭证）。

### 看板合约区
- 币安合约卡：BTCUSDT 标记价（WS 流）、USDT 钱包余额/可用、持仓、挂单、
  价格流状态；按钮只有“刷新账户”和“重连价格流”，没有下单按钮。
- 合约最近记录：`binance_orders.log` 最近 20 条。
- 接口：`GET /api/binance`，`POST /api/binance/refresh`（手动刷新），
  `POST /api/binance/reconnect`（手动重连价格流）。

### 防封设计（2026-11-12 被封过 IP，code -1003）
- 没有任何自动重试定时器：任何请求只发一次，失败就报错停住，等人手动再跑。
- 没有 REST 轮询循环：实时价走 websocket；REST 只在人点了操作时单次调用；
  看板里的账户快照有 30 秒缓存，5 秒刷一次页面也不多调币安。
- 遇到 HTTP 429 / 418 / -1003 立刻停（ApiBanned），看板自动刷新随之停掉，
  只等手动刷新；遇到 HTTP 451（出口 IP 被币安地理限制）明说换网络再试。

### 依赖与端点（决策记录）
- 不新增依赖：价格流用 websocket-client，平台 install.command 本来就装它，
  feed.py 也在用。其他全是标准库。
- 期货在币安 .com（fapi.binance.com + wss://fstream.binance.com），
  Binance US 没有期货。WS 优先，REST 只做签名调用和单次取价。
- 文件：`quant_platform/binance_client.py`（连接/签名/下单/WS 流）、
  `binance_link.py`（链接）、`binance_order.py`（下单）、
  `test_binance.py`（单测）、`config.py` 里新增 `binance` 一节、
  `dashboard.py` / `dashboard.html` 增加合约区。
- 单测：`python3 test_binance.py`（24 个，离线）；`python3 test_binance.py
  --live-price` 再加一节实网 dry-run（用 data-api.binance.vision 的真实
  币安现货价验证管线 + 数学；fapi 在测试 VM 上 451，期货价以 Mac 上
  `binance_link.py --test` 的结果为准）。

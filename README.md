# Polymarket 量化平台（Mac 版，纸面模式）

## 现在是什么状态
- 策略：`observe_only`（占位，只观察不下单，策略按你的决定放最后）
- 钱包：没接。先跑 `link_wallet.py` 链接（只读验证）。实盘开关 `live_trading: false`，强行开会直接报错停住；下单默认 dry-run，实盘要 `--live` + `--confirm` + 开关三重确认
- 跑通的是：Gamma 发现市场 → CLOB REST 种子盘口 → CLOB WebSocket 实时盘口 → 引擎算双边和 → 纸面账本 + 风控；钱包链接走官方 py-clob-client-v2（L1 派生凭证 + L2 查余额/挂单/下单）

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
  - `GET /api/status`：运行状态 + 最新心跳 + 各市场最新盘口
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

## 文件
- `config.py`：配置（模式、市场、风控上限、策略名）
- `quant_platform/feed.py`：行情（Gamma + CLOB WS）
- `quant_platform/engine.py`：引擎
- `quant_platform/ledger.py`：纸面账本
- `quant_platform/risk.py`：风控（单笔上限、日亏熔断、kill switch）
- `quant_platform/strategy.py`：策略接口 + 占位策略
- `quant_platform/storage.py`：历史数据（SQLite，`history.db`）
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

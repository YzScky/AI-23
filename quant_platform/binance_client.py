"""Binance 合约（USDT 本位永续）连接 + 下单（默认 dry-run）。

只跑在用户自己的 Mac 上。纸面先行：连接、账户、订单管道先跑通，
策略以后再定（本模块不含任何策略信号）。

端点（币安 .com 期货，Binance US 没有期货，别用 api.binance.us）：
- REST 公开：https://fapi.binance.com/fapi/v1/ticker/price（现价）、
  /fapi/v1/exchangeInfo（精度/最小下单量）
- REST 签名：/fapi/v2/account（余额/持仓）、/fapi/v1/openOrders、
  /fapi/v1/order（下单/撤单）、/fapi/v1/leverage、/fapi/v1/marginType
- WS 行情：wss://fstream.binance.com/ws/<symbol>@markPrice（标记价流）

认证：HMAC SHA256 签名（API Key + Secret），请求头 X-MBX-APIKEY。
凭证只从 Mac 本地 .env 读 BINANCE_API_KEY / BINANCE_API_SECRET，
绝不写进代码、日志、看板输出、报错信息。没有凭证时报“未连接”，不崩。

防封设计（2026-11-12 用户被币安封过 IP，code -1003，起因是 REST 轮询
太密 + 一个未知脚本每 600 秒自动重试）：
- 没有任何自动重试定时器：任何请求只发一次，失败就报错停住，等人手动再跑。
- 没有 REST 轮询循环：实时价走 websocket；REST 只在“人点了操作”时
  单次调用（查账户、下单、dry-run 取价），看板里的账户快照有 30 秒缓存。
- 遇到 HTTP 429 / 418 / -1003 立刻抛 ApiBanned，不重试、不继续。
- 遇到 HTTP 451（出口 IP 被币安地理限制）抛 GeoBlocked，明说换网络再试。

下单硬护栏（按顺序，缺一不可才会发真单）：
1. dry_run=True（默认）：什么都不发，只校验 + 风控上限 + 取公开价算单，
   返回模拟结果。
2. dry_run=False：命令行同时带 --live 和 --confirm（见 binance_order.py）。
3. config.py 里 binance.live_trading=true（默认 False）。
4. 风控上限：只做多（SELL 只能是平多，不开空）；保证金 ≤ 10U；杠杆 ≤ 10x；
   名义 ≤ 100U；当日已实现亏损 < 日亏上限 50U。
5. 通过才发单：先设杠杆 + 逐仓，再发市价单进场，随后挂止损市价单
   （STOP_MARKET，closePosition）和止盈市价单（TAKE_PROFIT_MARKET，
   closePosition）。每一次尝试（模拟/实盘/撤单）都追加记到
   binance_orders.log，只记白名单字段，绝不记凭证。

依赖：标准库 only + websocket-client（价格流用，平台 install.command
本来就装它，feed.py 也在用，不新增依赖）。
"""
import hashlib
import hmac
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from config import CONFIG
except Exception:
    CONFIG = {"binance": {"live_trading": False, "capital": 200.0,
                          "margin_per_trade": 10.0, "leverage": 10,
                          "stop_pct": 0.05, "take_pct": 0.60,
                          "long_only": True, "daily_loss_cap": 50.0,
                          "max_total_loss": 500.0, "fee_rate": 0.0005,
                          "symbols": ["BTCUSDT"]}}

BINANCE_FAPI = os.environ.get("BINANCE_FAPI_BASE") or "https://fapi.binance.com"
BINANCE_WS = "wss://fstream.binance.com"
USER_AGENT = "agg-quant-binance/1.0"
RECV_WINDOW = 5000
REQUEST_TIMEOUT = 10

ENV_VARS = ["BINANCE_API_KEY", "BINANCE_API_SECRET"]
ORDERS_LOG = os.path.join(ROOT, "binance_orders.log")

# 默认精度（exchangeInfo 拉不到时用，dry-run 会注明“用的默认值”）。
DEFAULT_FILTERS = {"step_size": 0.001, "tick_size": 0.1,
                   "min_qty": 0.001, "min_notional": 5.0}
CST = timezone(timedelta(hours=8))


class BinanceError(Exception):
    """币安接口错误（信息已脱敏）。"""


class ApiBanned(BinanceError):
    """429 / 418 / -1003：限流或封 IP。立刻停，不重试。"""


class GeoBlocked(BinanceError):
    """HTTP 451：出口 IP 被币安地理限制，换网络再试。"""


class LiveTradingDisabledError(Exception):
    """实盘开关没开，拒绝发真单。"""


class RiskRefusedError(Exception):
    """风控上限拒绝。"""


# ---------- .env ----------

def load_env(path=None):
    """读 .env（KEY=VALUE，# 注释、空行、引号都处理）。缺失的键再看同名环境变量。
    只返回 dict，绝不打印值。"""
    path = path or os.path.join(ROOT, ".env")
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                if k:
                    env[k] = v
    except FileNotFoundError:
        pass
    for k in ENV_VARS:
        if not env.get(k) and os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _scrub(text, secrets):
    s = str(text)
    for v in secrets:
        if v and len(v) >= 8 and v in s:
            s = s.replace(v, "***")
    return s


# ---------- HTTP（单次请求，绝不重试） ----------

def _http_json(method, url, headers=None, data=None, timeout=REQUEST_TIMEOUT):
    """发一次 HTTP 请求，返回解析后的 JSON。429/418/-1003 → ApiBanned；
    451 → GeoBlocked；其他 HTTP 错误 → BinanceError。绝不重试。"""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore") if hasattr(e, "read") else ""
        if e.code in (429, 418) or "-1003" in body or "banned" in body.lower():
            raise ApiBanned(
                f"币安限流/封 IP（HTTP {e.code}）：已停机，不会自动重试。"
                f"等至少 30 分钟再手动重跑一次。{body[:150]}")
        if e.code == 451 or "restricted location" in body:
            raise GeoBlocked(
                "币安拒绝了这个出口 IP（HTTP 451，地理限制）。"
                "这台机器的网络到不了 fapi.binance.com，换网络/VPN 出口再试。")
        raise BinanceError(f"币安接口 HTTP {e.code}：{body[:200]}")
    except urllib.error.URLError as e:
        raise BinanceError(f"网络不通（{e.reason}）：检查网络 / fapi.binance.com 是否可达")


def _sign_params(secret, params):
    """HMAC SHA256 签名：对 urlencode 后的 query 串签名，返回 (query, signature)。"""
    query = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode("utf-8"), query.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return query, sig


# ---------- 公开行情（不用凭证） ----------

def public_price(symbol, base=None, path="/fapi/v1/ticker/price"):
    """一根现价：GET /fapi/v1/ticker/price。单次请求，失败就抛，不重试。
    base/path 只给测试用（比如指向 data-api.binance.vision 的现货价验证管线）；
    正常永远用 fapi.binance.com——期货价只能从期货接口拿，现货价不能替。"""
    url = (base or BINANCE_FAPI) + path + "?symbol=" + symbol.upper()
    data = _http_json("GET", url, headers={"User-Agent": USER_AGENT})
    try:
        return float(data["price"])
    except (KeyError, TypeError, ValueError):
        raise BinanceError(f"现价接口返回格式不对：{str(data)[:150]}")


_exchange_info_cache = {}


def public_filters(symbol, base=None, path="/fapi/v1/exchangeInfo"):
    """精度/最小下单量：GET /fapi/v1/exchangeInfo（进程内缓存一次）。
    拉不到就返回默认值并注明 filters_from_defaults=True。"""
    symbol = symbol.upper()
    if symbol in _exchange_info_cache:
        return _exchange_info_cache[symbol]
    url = (base or BINANCE_FAPI) + path + "?symbol=" + symbol
    try:
        data = _http_json("GET", url, headers={"User-Agent": USER_AGENT})
        sym = (data.get("symbols") or [{}])[0]
        out = dict(DEFAULT_FILTERS)
        out["filters_from_defaults"] = False
        for f in sym.get("filters", []):
            t = f.get("filterType")
            if t == "LOT_SIZE":
                out["step_size"] = float(f["stepSize"])
                out["min_qty"] = float(f["minQty"])
            elif t == "PRICE_FILTER":
                out["tick_size"] = float(f["tickStep"]) if "tickStep" in f else float(f["tickSize"])
            elif t in ("MIN_NOTIONAL", "NOTIONAL"):
                out["min_notional"] = float(f.get("minNotional") or f.get("notional", 5.0))
        _exchange_info_cache[symbol] = out
        return out
    except Exception:
        out = dict(DEFAULT_FILTERS)
        out["filters_from_defaults"] = True
        _exchange_info_cache[symbol] = out
        return out


# ---------- 下单数学（纯函数，可单测） ----------

def _floor_to_step(value, step):
    if step <= 0:
        return value
    # round(..., 10) 去掉二进制浮点尾巴（比如 98536.40000000001 → 98536.4）
    return round(int(value / step) * step, 10)


def compute_order(price, margin, leverage, stop_pct, take_pct,
                  fee_rate, filters):
    """按纸面默认参数算一单：保证金 margin、杠杆 leverage、止损 stop_pct、
    止盈 take_pct。返回数量/名义/止损价/止盈价/手续费/止损时最大亏损。
    数量向下取整到 stepSize（宁可少买，不多扣保证金）。"""
    price = float(price)
    if price <= 0:
        raise RiskRefusedError("价格必须大于 0")
    notional_target = float(margin) * float(leverage)
    qty = _floor_to_step(notional_target / price, filters["step_size"])
    if qty <= 0:
        raise RiskRefusedError(
            f"算出的数量为 0（名义 {notional_target}U / 价 {price} 小于最小步长）")
    if qty < filters["min_qty"]:
        raise RiskRefusedError(f"数量 {qty} 小于最小下单量 {filters['min_qty']}")
    notional = qty * price
    if notional < filters["min_notional"]:
        raise RiskRefusedError(
            f"名义 {notional:.2f}U 小于最小名义 {filters['min_notional']}U")
    stop_price = _floor_to_step(price * (1 - stop_pct), filters["tick_size"])
    take_price = _floor_to_step(price * (1 + take_pct), filters["tick_size"])
    fee_open = notional * fee_rate
    fee_close = notional * fee_rate
    max_loss_at_stop = (price - stop_price) * qty + fee_open + fee_close
    return {
        "price": price, "qty": qty, "notional": round(notional, 4),
        "margin": float(margin), "leverage": float(leverage),
        "stop_price": stop_price, "take_price": take_price,
        "stop_pct": stop_pct, "take_pct": take_pct,
        "fee_open": round(fee_open, 4), "fee_close": round(fee_close, 4),
        "max_loss_at_stop": round(max_loss_at_stop, 4),
        "filters_from_defaults": filters.get("filters_from_defaults", False),
    }


def binance_cfg():
    """config.py 里的 binance 一节（缺了就用纸面默认）。"""
    b = CONFIG.get("binance") or {}
    return {
        "live_trading": bool(b.get("live_trading", False)),
        "capital": float(b.get("capital", 200.0)),
        "margin_per_trade": float(b.get("margin_per_trade", 10.0)),
        "leverage": float(b.get("leverage", 10)),
        "stop_pct": float(b.get("stop_pct", 0.05)),
        "take_pct": float(b.get("take_pct", 0.60)),
        "long_only": bool(b.get("long_only", True)),
        "daily_loss_cap": float(b.get("daily_loss_cap", 50.0)),
        "max_total_loss": float(b.get("max_total_loss", 500.0)),
        "fee_rate": float(b.get("fee_rate", 0.0005)),
        "symbols": list(b.get("symbols", ["BTCUSDT"])),
    }


def check_risk(cfg, side, margin, leverage, notional, close, daily_loss):
    """风控上限。返回 (ok, reason)。只做多：SELL 只能是平多（close=True），
    不开空；保证金/杠杆/名义/日亏四道上限。"""
    side = (side or "").upper()
    if side not in ("BUY", "SELL"):
        return False, "side 只能是 BUY 或 SELL"
    if side == "SELL" and not close:
        return False, "只做多：SELL 只能是平多（--close），不开空"
    if cfg["long_only"] and side == "SELL" and not close:
        return False, "long_only=true：不开空"
    if margin > cfg["margin_per_trade"]:
        return False, f"保证金 {margin}U 超过单笔上限 {cfg['margin_per_trade']}U"
    if leverage > cfg["leverage"]:
        return False, f"杠杆 {leverage}x 超过上限 {cfg['leverage']}x"
    if notional > cfg["margin_per_trade"] * cfg["leverage"]:
        return False, (f"名义 {notional:.2f}U 超过上限 "
                       f"{cfg['margin_per_trade'] * cfg['leverage']:.0f}U")
    if daily_loss >= cfg["daily_loss_cap"]:
        return False, f"当日亏损 {daily_loss}U 已到日亏上限 {cfg['daily_loss_cap']}U"
    return True, "ok"


# ---------- 链接 + 账户 + 下单 ----------

class BinanceLink:
    def __init__(self, root=None):
        self.root = root or ROOT
        self.env_path = os.path.join(self.root, ".env")
        self.orders_log = os.path.join(self.root, "binance_orders.log")
        self.env = load_env(self.env_path)
        self._secrets = [v for k, v in self.env.items() if v]

    # ---------- 内部 ----------

    def _scrub(self, text):
        return _scrub(text, self._secrets)

    def _missing(self):
        return [k for k in ENV_VARS if not self.env.get(k)]

    def _signed(self, method, path, params=None):
        """一次签名请求。params 里不用带 timestamp/signature，会自动补。
        失败抛 BinanceError / ApiBanned / GeoBlocked（已脱敏），绝不重试。"""
        missing = self._missing()
        if missing:
            raise BinanceError("凭证缺失：" + "、".join(missing) +
                               "（.env 里补齐，或跑 binance_link.py）")
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = RECV_WINDOW
        query, sig = _sign_params(self.env["BINANCE_API_SECRET"], p)
        url = BINANCE_FAPI + path + "?" + query + "&signature=" + sig
        try:
            return _http_json(method, url,
                              headers={"User-Agent": USER_AGENT,
                                       "X-MBX-APIKEY": self.env["BINANCE_API_KEY"]})
        except ApiBanned as e:
            # 限流/封 IP 的类型要原样传出去，看板靠它停掉自动刷新
            raise ApiBanned(self._scrub(str(e))[:300])
        except GeoBlocked as e:
            raise GeoBlocked(self._scrub(str(e))[:300])
        except BinanceError as e:
            raise BinanceError(self._scrub(str(e))[:300])

    def _log_attempt(self, record):
        """记 binance_orders.log：只写白名单字段，绝不写凭证。"""
        safe = {"ts": int(time.time()),
                "kind": record.get("kind"),
                "symbol": record.get("symbol"),
                "side": record.get("side"),
                "price": record.get("price"),
                "qty": record.get("qty"),
                "notional": record.get("notional"),
                "margin": record.get("margin"),
                "leverage": record.get("leverage"),
                "stop_price": record.get("stop_price"),
                "take_price": record.get("take_price"),
                "outcome": record.get("outcome"),
                "order_id": record.get("order_id"),
                "realized_loss": record.get("realized_loss"),
                "error": record.get("error")}
        try:
            with open(self.orders_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(safe, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def daily_realized_loss(self):
        """今天 binance_orders.log 里记的实现亏损之和。
        目前只在平仓成交后才会有 realized_loss 字段；没平仓就是 0，
        上限检查照常跑（和 Polymarket 那边一样的诚实口径）。"""
        if not os.path.exists(self.orders_log):
            return 0.0
        today = datetime.now(CST).strftime("%Y-%m-%d")
        total = 0.0
        try:
            with open(self.orders_log, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    ts = r.get("ts")
                    if not ts:
                        continue
                    day = datetime.fromtimestamp(ts, tz=CST).strftime("%Y-%m-%d")
                    if day == today and isinstance(r.get("realized_loss"), (int, float)):
                        total += r["realized_loss"]
        except Exception:
            return 0.0
        return total

    # ---------- 只读：链接状态 / 账户 / 持仓 / 挂单 ----------

    def link_status(self):
        """返回 dict，绝不抛异常、绝不含凭证。
        没凭证 → linked=False + 未连接原因；有凭证 → 调 /fapi/v2/account 验证。"""
        out = {"linked": False, "balance_usdt": None, "available_usdt": None,
               "positions": [], "open_orders": None, "error": None}
        if not os.path.exists(self.env_path):
            out["error"] = "未连接：没有找到 .env 文件（先跑 binance_link.py 配 API Key）"
            return out
        missing = self._missing()
        if missing:
            out["error"] = ("未连接：凭证缺失 " + "、".join(missing) +
                            "（.env 里补齐，或跑 binance_link.py）")
            return out
        try:
            snap = self.account_snapshot()
        except Exception as e:
            out["error"] = self._scrub(e)[:200] or "链接失败（未知错误）"
            return out
        if not snap.get("ok"):
            out["error"] = snap.get("error") or "链接失败（未知错误）"
            return out
        out.update({"linked": True,
                    "balance_usdt": snap.get("balance_usdt"),
                    "available_usdt": snap.get("available_usdt"),
                    "positions": snap.get("positions", []),
                    "open_orders": snap.get("open_orders")})
        return out

    def account_snapshot(self):
        """一次签名查询：余额 + 持仓 + 挂单数。返回 dict，ok=False 时带 error。"""
        try:
            acct = self._signed("GET", "/fapi/v2/account")
        except Exception as e:
            return {"ok": False, "error": self._scrub(e)[:200]}
        try:
            orders = self._signed("GET", "/fapi/v1/openOrders")
        except Exception:
            orders = None  # 挂单拉不到不致命，余额/持仓照常用
        usdt = None
        for a in acct.get("assets") or []:
            if a.get("asset") == "USDT":
                usdt = a
                break
        positions = []
        for p in acct.get("positions") or []:
            amt = float(p.get("positionAmt") or 0)
            if amt != 0:
                positions.append({
                    "symbol": p.get("symbol"),
                    "side": "LONG" if amt > 0 else "SHORT",
                    "qty": abs(amt),
                    "entry_price": float(p.get("entryPrice") or 0),
                    "unrealized_pnl": float(p.get("unRealizedProfit") or 0),
                    "leverage": p.get("leverage"),
                    "margin_type": p.get("marginType"),
                })
        return {"ok": True,
                "balance_usdt": float(usdt.get("walletBalance")) if usdt else None,
                "available_usdt": float(usdt.get("availableBalance")) if usdt else None,
                "positions": positions,
                "open_orders": len(orders) if isinstance(orders, list) else None,
                "raw_orders": orders if isinstance(orders, list) else []}

    def get_open_orders(self, symbol=None):
        """只读挂单列表（原始字段只取白名单）。"""
        try:
            params = {"symbol": symbol} if symbol else {}
            orders = self._signed("GET", "/fapi/v1/openOrders", params)
        except Exception as e:
            return {"ok": False, "orders": [], "count": 0,
                    "error": self._scrub(e)[:200]}
        trimmed = []
        for o in orders if isinstance(orders, list) else []:
            if not isinstance(o, dict):
                continue
            trimmed.append({"id": o.get("orderId"), "symbol": o.get("symbol"),
                            "side": o.get("side"), "type": o.get("type"),
                            "price": o.get("price"), "qty": o.get("origQty"),
                            "status": o.get("status"),
                            "reduce_only": o.get("reduceOnly")})
        return {"ok": True, "orders": trimmed, "count": len(trimmed), "error": None}

    def get_position_qty(self, symbol):
        """当前该币种的多头数量（没有持仓返回 0.0）。"""
        try:
            snap = self.account_snapshot()
        except Exception:
            return 0.0
        if not snap.get("ok"):
            return 0.0
        for p in snap.get("positions", []):
            if p.get("symbol") == symbol.upper() and p.get("side") == "LONG":
                return float(p.get("qty") or 0)
        return 0.0

    # ---------- 下单 / 撤单 ----------

    def place_order(self, symbol, side, margin=None, leverage=None,
                    dry_run=True, close=False, price=None, price_base=None):
        """开多（BUY）或平多（SELL + close=True）。
        dry_run=True（默认）：只校验 + 风控 + 取公开价算单，不发任何东西。
        dry_run=False：依次过实盘开关 → 风控 → 设杠杆/逐仓 → 发单 → 挂止损/止盈。"""
        cfg = binance_cfg()
        symbol = (symbol or "BTCUSDT").upper()
        side = (side or "").upper()
        margin = cfg["margin_per_trade"] if margin is None else float(margin)
        leverage = cfg["leverage"] if leverage is None else float(leverage)
        base = {"symbol": symbol, "side": side, "margin": margin,
                "leverage": leverage}
        kind = "DRY_RUN" if dry_run else "LIVE"

        # 风控上限：模拟和实盘都要过（拒绝也要记日志）
        daily_loss = self.daily_realized_loss()
        ok, reason = check_risk(cfg, side, margin, leverage,
                                margin * leverage, close, daily_loss)
        if not ok:
            self._log_attempt({**base, "kind": kind,
                               "outcome": "refused: " + reason[:120]})
            raise RiskRefusedError(f"风控拒绝：{reason}")

        # 取价：指定价就用指定价，否则拉一次公开价（单次请求）
        try:
            px = float(price) if price else public_price(symbol, base=price_base)
        except Exception as e:
            msg = self._scrub(e)[:200]
            self._log_attempt({**base, "kind": kind, "outcome": "price_failed",
                               "error": msg})
            raise BinanceError(f"现价拿不到：{msg}")
        filters = public_filters(symbol, base=price_base)
        try:
            calc = compute_order(px, margin, leverage, cfg["stop_pct"],
                                 cfg["take_pct"], cfg["fee_rate"], filters)
        except RiskRefusedError as e:
            self._log_attempt({**base, "kind": kind,
                               "outcome": "refused: " + str(e)[:120]})
            raise

        if dry_run:
            self._log_attempt({**base, "kind": kind, "outcome": "would_send",
                               "price": calc["price"], "qty": calc["qty"],
                               "notional": calc["notional"],
                               "stop_price": calc["stop_price"],
                               "take_price": calc["take_price"]})
            note = ("dry-run：校验 + 风控 + 公开价都通过，未发送任何下单请求"
                    + ("（精度用的默认值，exchangeInfo 拉不到）"
                       if calc["filters_from_defaults"] else ""))
            if close:
                note += "（平多模拟：没连账户时不验证当前持仓量）"
            return {"ok": True, "dry_run": True, "would_send": True,
                    "symbol": symbol, "side": side, "close": close, **calc,
                    "reason": note}

        # ---- 实盘：开关先行 ----
        if not cfg["live_trading"]:
            self._log_attempt({**base, "kind": kind,
                               "outcome": "refused: live_trading off"})
            raise LiveTradingDisabledError(
                "实盘开关未开（config.py binance.live_trading=false）。未发送。"
                "开实盘要先定策略、接好币安 API、亲口确认实盘参数。")
        # 发单前风控再过一次
        ok, reason = check_risk(cfg, side, margin, leverage,
                                margin * leverage, close, self.daily_realized_loss())
        if not ok:
            self._log_attempt({**base, "kind": kind,
                               "outcome": "refused: " + reason[:120]})
            raise RiskRefusedError(f"风控拒绝：{reason}")

        qty = calc["qty"]
        if close:
            # 平多：只平现有的多头，不超过持仓量
            held = self.get_position_qty(symbol)
            if held <= 0:
                self._log_attempt({**base, "kind": kind,
                                   "outcome": "refused: no long position"})
                raise RiskRefusedError(f"没有 {symbol} 多头持仓，平不了")
            qty = min(qty, _floor_to_step(held, filters["step_size"]))
            if qty <= 0:
                raise RiskRefusedError("持仓量小于最小下单量，平不了")

        try:
            if not close:
                # 设杠杆 + 逐仓（已是逐仓时会报 -4046，忽略）
                self._signed("POST", "/fapi/v1/leverage",
                             {"symbol": symbol, "leverage": int(leverage)})
                try:
                    self._signed("POST", "/fapi/v1/marginType",
                                 {"symbol": symbol, "marginType": "ISOLATED"})
                except BinanceError as e:
                    if "-4046" not in str(e):
                        raise
            resp = self._signed("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": side, "type": "MARKET",
                "quantity": qty,
                **({"reduceOnly": "true"} if close else {}),
            })
        except Exception as e:
            msg = self._scrub(e)[:200]
            self._log_attempt({**base, "kind": kind, "outcome": "failed",
                               "price": calc["price"], "qty": qty,
                               "notional": calc["notional"], "error": msg})
            raise BinanceError("发单失败：" + msg)
        order_id = resp.get("orderId") if isinstance(resp, dict) else None
        self._log_attempt({**base, "kind": kind, "outcome": "sent",
                           "price": calc["price"], "qty": qty,
                           "notional": calc["notional"],
                           "stop_price": calc["stop_price"],
                           "take_price": calc["take_price"],
                           "order_id": str(order_id) if order_id else None})
        out = {"ok": True, "dry_run": False, "live": True, "symbol": symbol,
               "side": side, "close": close, **calc, "qty": qty,
               "order_id": order_id,
               "status": resp.get("status") if isinstance(resp, dict) else None}
        if close:
            return out
        # 进场后挂止损 + 止盈（各是一次签名请求；挂不上会在返回里注明，
        # 主单已成交，止损没挂上必须人工处理——绝不静默）
        sl, tp = None, None
        try:
            sl = self._signed("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": "SELL", "type": "STOP_MARKET",
                "stopPrice": calc["stop_price"], "closePosition": "true"})
        except Exception as e:
            out["stop_order_error"] = self._scrub(e)[:150]
        try:
            tp = self._signed("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": "SELL", "type": "TAKE_PROFIT_MARKET",
                "stopPrice": calc["take_price"], "closePosition": "true"})
        except Exception as e:
            out["take_profit_order_error"] = self._scrub(e)[:150]
        out["stop_order_id"] = sl.get("orderId") if isinstance(sl, dict) else None
        out["take_profit_order_id"] = tp.get("orderId") if isinstance(tp, dict) else None
        return out

    def cancel_order(self, symbol, order_id):
        """撤单（实盘操作，同样受 live_trading 开关管）。"""
        if not order_id:
            raise RiskRefusedError("order_id 不能为空")
        if not binance_cfg()["live_trading"]:
            self._log_attempt({"kind": "CANCEL", "symbol": symbol,
                               "order_id": str(order_id)[:64],
                               "outcome": "refused: live_trading off"})
            raise LiveTradingDisabledError(
                "实盘开关未开（config.py binance.live_trading=false）。未发送撤单。")
        try:
            resp = self._signed("DELETE", "/fapi/v1/order",
                                {"symbol": symbol, "orderId": int(order_id)})
        except Exception as e:
            msg = self._scrub(e)[:200]
            self._log_attempt({"kind": "CANCEL", "symbol": symbol,
                               "order_id": str(order_id)[:64],
                               "outcome": "failed", "error": msg})
            raise BinanceError("撤单失败：" + msg)
        self._log_attempt({"kind": "CANCEL", "symbol": symbol,
                           "order_id": str(order_id)[:64], "outcome": "sent"})
        return {"ok": True, "order_id": order_id,
                "result": resp if isinstance(resp, dict) else {"raw": str(resp)[:200]}}


def binance_status(root=None):
    """给看板用的便捷入口：返回 link_status() dict，绝不抛异常。"""
    try:
        return BinanceLink(root=root).link_status()
    except Exception as e:
        return {"linked": False, "balance_usdt": None, "available_usdt": None,
                "positions": [], "open_orders": None,
                "error": f"币安状态查询失败：{str(e)[:150]}"}


def recent_binance_orders(root=None, limit=20):
    """读 binance_orders.log 最近 limit 行（看板用），文件不存在返回 []。"""
    path = os.path.join(root or ROOT, "binance_orders.log")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return list(reversed(out))
    except Exception:
        return []


# ---------- WS 标记价流（价格流，不走 REST） ----------

class BinancePriceStream:
    """Binance 期货标记价 websocket 流（wss://fstream.binance.com）。

    用 websocket-client 的 create_connection + 手动 recv 循环（不用
    WebSocketApp 回调，连接超时可控）。只订阅 markPrice，不发任何 REST。
    断线重连最多 3 次、每次间隔 10 秒；3 次都失败就停住并在 status 里
    写明原因，等人手动点重连——不搞无限自动重连。
    websocket-client 没装时 status 直接报安装提示，不崩。"""

    MAX_RECONNECTS = 3
    RECONNECT_DELAY = 10
    CONNECT_TIMEOUT = 10
    RECV_TIMEOUT = 15

    def __init__(self, symbols=None):
        self.symbols = [s.upper() for s in (symbols or ["BTCUSDT"])]
        self.prices = {}   # symbol -> {"price": float, "ts": float}
        self.connected = False
        self.running = False
        self.error = None
        self.attempts = 0
        self._thread = None
        self._stop = threading.Event()

    def _stream_url(self):
        streams = "/".join(f"{s.lower()}@markPrice" for s in self.symbols)
        return f"{BINANCE_WS}/stream?streams={streams}"

    def _run_once(self):
        """一次连接 + 收消息循环。返回 False 表示这次连不上/断了。"""
        try:
            import websocket
        except ImportError:
            self.error = "websocket-client 未安装：跑 ./install.command 装依赖"
            return False
        try:
            ws = websocket.create_connection(
                self._stream_url(), timeout=self.CONNECT_TIMEOUT)
        except Exception as e:
            self.error = f"WS 连不上 {BINANCE_WS}：{str(e)[:120]}"
            return False
        try:
            ws.settimeout(self.RECV_TIMEOUT)
            self.connected = True
            self.error = None
            while not self._stop.is_set():
                try:
                    raw = ws.recv()
                except Exception:
                    # 超时/断线：跳出，交给重连逻辑
                    return False
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                data = msg.get("data") or msg
                if data.get("e") == "markPriceUpdate" and data.get("s"):
                    try:
                        self.prices[data["s"]] = {
                            "price": float(data["p"]), "ts": time.time()}
                    except (TypeError, ValueError):
                        pass
            return True
        finally:
            self.connected = False
            try:
                ws.close()
            except Exception:
                pass

    def _loop(self):
        self.running = True
        self.attempts = 0
        while not self._stop.is_set() and self.attempts <= self.MAX_RECONNECTS:
            self.attempts += 1
            ok = self._run_once()
            if ok:
                break
            if self._stop.is_set():
                break
            if self.attempts > self.MAX_RECONNECTS:
                break
            # 重连前等 10 秒；这只是 WS 重连，不是 REST 轮询，不产生 REST 权重
            if self._stop.wait(self.RECONNECT_DELAY):
                break
        if not self.connected and not self.error:
            self.error = "WS 已断开（重连 3 次都失败），点“重连价格流”手动再试"
        self.running = False

    def start(self):
        """启动后台线程（守护线程）。已在跑时什么都不做。"""
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self.error = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self.connected = False

    def status(self):
        """看板用：绝不抛异常。"""
        return {"running": self.running, "connected": self.connected,
                "symbols": self.symbols,
                "prices": {k: dict(v) for k, v in self.prices.items()},
                "error": self.error, "attempts": self.attempts}

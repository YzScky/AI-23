"""CLOB v2 钱包链接 + 只读查询 + 下单（默认 dry-run）。

只用官方 py-clob-client-v2（v1 已归档停更，绝不用）。
认证模型（以 v2 1.2.2 源码 + docs.polymarket.com 为准）：
- L1：钱包私钥 EIP-712 签名 → create_or_derive_api_key() 生成 API 凭证
- L2：API 凭证 HMAC → 查余额 / 挂单 / 下单 / 撤单
- 关键：L2 请求头要带 POLY_ADDRESS（由私钥派生），官方客户端要求运行时
  必须有私钥——只读的查余额、查挂单也要私钥，不只是下单才要。
- 下单还要私钥再签一次 EIP-712 订单（L2 认证请求 + 钱包签名授权订单本身）。
- 凭证只从 Mac 本地 .env 读，绝不写进代码、日志、看板输出、报错信息。
  报错信息会把所有凭证值替换成 *** 再返回。

下单硬护栏（按顺序）：
1. dry_run=True（默认）：什么都不发，只校验参数 + 风控上限 + 查公开盘口，
   返回模拟结果。
2. dry_run=False：先看 config.py live_trading（默认 False）——没开就抛
   LiveTradingDisabledError，什么都不发。
3. 再过 risk.py 的上限（单笔名义金额 ≤ max_pair_size；当日已实现亏损 < 日亏上限）。
4. 通过才调 v2 客户端发单。每一次尝试（模拟/实盘/撤单）都追加记到 orders.log，
   只记白名单字段，绝不记凭证。
"""
import concurrent.futures
import datetime
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from config import CONFIG
except Exception:
    CONFIG = {"live_trading": False,
              "risk": {"total_capital": 1000.0, "max_pair_size": 200.0,
                       "daily_loss_cap": 100.0}}

DEFAULT_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
CLOB_BOOK = "https://clob.polymarket.com/book"

# .env 里读的变量名——与 py-clob-client-v2 官方 .env.example 一致。
# 注意官方叫 CLOB_SECRET，不是 CLOB_API_SECRET。
ENV_VARS = ["PK", "CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASS_PHRASE",
            "CLOB_API_URL", "POLYMARKET_SIGNATURE_TYPE"]
REQUIRED_FOR_LINK = ["PK", "CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASS_PHRASE"]

# 签名类型（v2 SignatureTypeV2）：0=EOA 普通钱包（默认）；1=POLY_PROXY；
# 2=POLY_GNOSIS_SAFE；3=POLY_1271（2026-05-04 之后建的 Deposit Wallet）。
DEFAULT_SIGNATURE_TYPE = 0

ORDERS_LOG = os.path.join(ROOT, "orders.log")


class LiveTradingDisabledError(Exception):
    """实盘开关没开，拒绝发真单。"""


class RiskRefusedError(Exception):
    """风控上限拒绝。"""


class ClobLinkError(Exception):
    """链接/认证/网络错误（信息已脱敏）。"""


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


def mask_address(addr):
    if not addr or len(addr) < 10:
        return None
    return f"{addr[:6]}...{addr[-4:]}"


def _scrub(text, secrets):
    """把报错信息里出现过的凭证值替换成 ***（纵深防御）。"""
    s = str(text)
    for v in secrets:
        if v and len(v) >= 8 and v in s:
            s = s.replace(v, "***")
    return s


def _call_with_timeout(fn, timeout=20):
    """带超时的同步调用：CLOB 没响应时不无限挂住。超时抛 TimeoutError。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"CLOB {timeout} 秒没响应（网络或服务问题）")


def _parse_balance(raw):
    """余额接口返回 6 位定点整数字符串（'125500000' → 125.5 pUSD），
    老版本也可能直接给小数。两种都认，返回 float；解析失败返回 None。"""
    if raw is None:
        return None
    try:
        s = str(raw).strip()
        if s == "":
            return None
        if "." not in s:
            return int(s) / 1_000_000.0
        return float(s)
    except (ValueError, TypeError):
        return None


class ClobLink:
    def __init__(self, root=None):
        self.root = root or ROOT
        self.env_path = os.path.join(self.root, ".env")
        self.orders_log = os.path.join(self.root, "orders.log")
        self.env = load_env(self.env_path)
        self._secrets = [v for k, v in self.env.items() if v]
        self._cached_client = None

    # ---------- 内部 ----------

    def _scrub(self, text):
        return _scrub(text, self._secrets)

    def _missing(self):
        return [k for k in REQUIRED_FOR_LINK if not self.env.get(k)]

    def _import_client(self):
        try:
            from py_clob_client_v2 import ApiCreds, ClobClient
            return ApiCreds, ClobClient
        except ImportError:
            raise ClobLinkError("py-clob-client-v2 未安装：先跑 ./install.command 装依赖")
        except Exception as e:
            # 比如某些代理环境变量会让它底层的 httpx 在 import 时就报错；
            # 这是环境问题，不是凭证问题，照实报，不带凭证内容。
            raise ClobLinkError("py-clob-client-v2 导入失败：" + self._scrub(e)[:150])

    def _client(self):
        """建 L2 客户端（私钥 + API 凭证）。失败抛 ClobLinkError（已脱敏）。"""
        if self._cached_client is not None:
            return self._cached_client
        missing = self._missing()
        if not os.path.exists(self.env_path):
            raise ClobLinkError("没有找到 .env 文件：先在 Mac 终端跑 .venv/bin/python link_wallet.py 配好凭证")
        if missing:
            raise ClobLinkError("凭证缺失：" + "、".join(missing) + "（.env 里补齐，或重跑 link_wallet.py）")
        ApiCreds, ClobClient = self._import_client()
        try:
            sig_type = int(self.env.get("POLYMARKET_SIGNATURE_TYPE") or DEFAULT_SIGNATURE_TYPE)
        except ValueError:
            sig_type = DEFAULT_SIGNATURE_TYPE
        creds = ApiCreds(api_key=self.env["CLOB_API_KEY"],
                         api_secret=self.env["CLOB_SECRET"],
                         api_passphrase=self.env["CLOB_PASS_PHRASE"])
        try:
            self._cached_client = ClobClient(
                host=self.env.get("CLOB_API_URL") or DEFAULT_HOST,
                chain_id=CHAIN_ID,
                key=self.env["PK"],
                creds=creds,
                signature_type=sig_type)
        except Exception as e:
            raise ClobLinkError("建 CLOB 客户端失败（可能是私钥格式不对）：" + self._scrub(e)[:200])
        return self._cached_client

    def _log_attempt(self, record):
        """记 orders.log：只写白名单字段，绝不写凭证。"""
        safe = {"ts": int(time.time()),
                "kind": record.get("kind"),
                "token_id": record.get("token_id"),
                "side": record.get("side"),
                "price": record.get("price"),
                "size": record.get("size"),
                "outcome": record.get("outcome"),
                "order_id": record.get("order_id"),
                "error": record.get("error")}
        try:
            with open(self.orders_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(safe, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _daily_realized_loss(self):
        """今天 orders.log 里已记的实现亏损之和（目前下单只记名义金额，
        实际成交盈亏要等后续的成交跟踪；现在恒为 0，但上限检查照常跑）。"""
        if not os.path.exists(self.orders_log):
            return 0.0
        today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
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
                    day = datetime.datetime.fromtimestamp(
                        ts, tz=datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
                    if day == today and isinstance(r.get("realized_loss"), (int, float)):
                        total += r["realized_loss"]
        except Exception:
            return 0.0
        return total

    def _risk_check(self, notional):
        """过 risk.py 的上限：单笔名义金额 + 日亏上限。不通过抛 RiskRefusedError。"""
        try:
            from quant_platform.risk import RiskManager
        except Exception:
            from risk import RiskManager
        risk = RiskManager(CONFIG)

        class _ShimLedger:
            def __init__(self, daily_loss):
                self.daily_loss = daily_loss

        ok, reason = risk.check(_ShimLedger(self._daily_realized_loss()), notional)
        if not ok:
            raise RiskRefusedError(f"风控拒绝：{reason}（单笔上限 {risk.max_pair_size} U，日亏上限 {risk.daily_loss_cap} U）")

    @staticmethod
    def _public_book(token_id):
        """公开盘口（不用凭证）。返回 {"best_bid": float|None, "best_ask": float|None}。
        请求头和 feed.py 一致（不带会被 403），gzip 响应要解压。"""
        import gzip
        req = urllib.request.Request(
            CLOB_BOOK + "?token_id=" + token_id,
            headers={"Accept": "application/json",
                     "Accept-Encoding": "gzip",
                     "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8"))
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        best_bid = max((float(b["price"]) for b in bids if b.get("price") is not None),
                       default=None)
        best_ask = min((float(a["price"]) for a in asks if a.get("price") is not None),
                       default=None)
        return {"best_bid": best_bid, "best_ask": best_ask}

    # ---------- 只读：链接状态 / 余额 / 挂单 ----------

    def link_status(self):
        """返回 dict，绝不抛异常、绝不含凭证。"""
        out = {"linked": False, "wallet_address": None, "balance": None,
               "allowance": None, "open_orders": None, "error": None}
        if not os.path.exists(self.env_path):
            out["error"] = "没有找到 .env 文件：先在 Mac 终端跑 .venv/bin/python link_wallet.py 配好凭证"
            return out
        missing = self._missing()
        if missing:
            out["error"] = "凭证缺失：" + "、".join(missing) + "（.env 里补齐，或重跑 link_wallet.py）"
            return out
        try:
            client = self._client()
            addr = _call_with_timeout(client.get_address, timeout=10)
            out["wallet_address"] = mask_address(addr)
            try:
                _call_with_timeout(
                    lambda: client.update_balance_allowance(), timeout=10)
            except Exception:
                pass  # 刷新缓存失败不致命，继续读缓存值
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams
            bal = _call_with_timeout(
                lambda: client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)),
                timeout=15)
            if isinstance(bal, dict):
                out["balance"] = _parse_balance(bal.get("balance"))
                allowances = bal.get("allowances") or {}
                if isinstance(allowances, dict) and allowances:
                    out["allowance"] = _parse_balance(next(iter(allowances.values())))
                else:
                    out["allowance"] = _parse_balance(bal.get("allowance"))
            orders = _call_with_timeout(
                lambda: client.get_open_orders(only_first_page=True), timeout=15)
            out["open_orders"] = len(orders) if isinstance(orders, list) else 0
            out["linked"] = True
        except Exception as e:
            out["error"] = self._scrub(e)[:200] or "链接失败（未知错误）"
        return out

    def get_balance(self):
        """只读余额。返回 {"ok": bool, "balance": float|None, "allowance": float|None, "error": str|None}。"""
        st = self.link_status()
        if not st["linked"]:
            return {"ok": False, "balance": None, "allowance": None, "error": st["error"]}
        return {"ok": True, "balance": st["balance"], "allowance": st["allowance"], "error": None}

    def list_open_orders(self):
        """只读挂单列表（只取第一页）。返回 {"ok": bool, "orders": [...], "count": int, "error": str|None}。"""
        try:
            client = self._client()
            orders = _call_with_timeout(
                lambda: client.get_open_orders(only_first_page=True), timeout=15)
        except Exception as e:
            return {"ok": False, "orders": [], "count": 0, "error": self._scrub(e)[:200]}
        trimmed = []
        for o in orders if isinstance(orders, list) else []:
            if not isinstance(o, dict):
                continue
            trimmed.append({"id": o.get("id"), "market": o.get("market"),
                            "asset_id": o.get("asset_id"), "side": o.get("side"),
                            "price": o.get("price"), "size": o.get("original_size") or o.get("size"),
                            "status": o.get("status")})
        return {"ok": True, "orders": trimmed, "count": len(trimmed), "error": None}

    def get_order(self, order_id):
        """只读查单（链接后可用，实盘开关不影响）。"""
        if not order_id:
            return {"ok": False, "order": None, "error": "order_id 不能为空"}
        try:
            client = self._client()
            o = _call_with_timeout(lambda: client.get_order(order_id), timeout=15)
        except Exception as e:
            return {"ok": False, "order": None, "error": self._scrub(e)[:200]}
        if not isinstance(o, dict):
            return {"ok": False, "order": None, "error": "CLOB 返回的订单格式不对"}
        self._log_attempt({"kind": "READ", "order_id": str(order_id)[:64],
                           "outcome": "ok"})
        return {"ok": True,
                "order": {"id": o.get("id"), "market": o.get("market"),
                          "asset_id": o.get("asset_id"), "side": o.get("side"),
                          "price": o.get("price"),
                          "size": o.get("original_size") or o.get("size"),
                          "status": o.get("status")},
                "error": None}

    # ---------- 下单 / 撤单 ----------

    def place_order(self, token_id, side, price, size, dry_run=True):
        """限价单。dry_run=True（默认）只校验 + 查公开盘口，不发任何东西。
        dry_run=False 时依次过：实盘开关 → 风控上限 → v2 发单。"""
        token_id = str(token_id or "").strip()
        side = str(side or "").strip().upper()
        base = {"token_id": token_id, "side": side, "price": price, "size": size}
        try:
            price = float(price)
            size = float(size)
        except (TypeError, ValueError):
            err = "价格/数量必须是数字"
            self._log_attempt({**base, "kind": "DRY_RUN" if dry_run else "LIVE",
                               "outcome": "refused: " + err})
            raise RiskRefusedError(err)
        if not token_id:
            err = "token_id 不能为空"
            self._log_attempt({**base, "kind": "DRY_RUN" if dry_run else "LIVE",
                               "outcome": "refused: " + err})
            raise RiskRefusedError(err)
        if side not in ("BUY", "SELL"):
            err = "side 只能是 BUY 或 SELL"
            self._log_attempt({**base, "kind": "DRY_RUN" if dry_run else "LIVE",
                               "outcome": "refused: " + err})
            raise RiskRefusedError(err)
        if not (0 < price < 1):
            err = f"价格 {price} 不在 (0, 1) 区间"
            self._log_attempt({**base, "kind": "DRY_RUN" if dry_run else "LIVE",
                               "outcome": "refused: " + err})
            raise RiskRefusedError(err)
        if size <= 0:
            err = "数量必须大于 0"
            self._log_attempt({**base, "kind": "DRY_RUN" if dry_run else "LIVE",
                               "outcome": "refused: " + err})
            raise RiskRefusedError(err)

        notional = round(price * size, 4)
        # 风控上限：模拟和实盘都要过（拒绝也要记 orders.log）
        try:
            self._risk_check(notional)
        except RiskRefusedError as e:
            self._log_attempt({**base, "kind": "DRY_RUN" if dry_run else "LIVE",
                               "outcome": "refused: " + str(e)[:120]})
            raise

        if dry_run:
            try:
                book = self._public_book(token_id)
            except Exception as e:
                self._log_attempt({**base, "kind": "DRY_RUN", "outcome": "book_failed",
                                   "error": self._scrub(e)[:120]})
                return {"ok": True, "dry_run": True, "would_send": False,
                        "token_id": token_id, "side": side, "price": price,
                        "size": size, "notional": notional,
                        "book": {"best_bid": None, "best_ask": None},
                        "reason": "公开盘口拉取失败（网络问题），无法确认当前盘口；未发送"}
            self._log_attempt({**base, "kind": "DRY_RUN", "outcome": "would_send"})
            return {"ok": True, "dry_run": True, "would_send": True,
                    "token_id": token_id, "side": side, "price": price,
                    "size": size, "notional": notional,
                    "book": book,
                    "reason": "dry-run：校验 + 风控 + 公开盘口都通过，未发送任何下单请求"}

        # 实盘：开关先行
        if not CONFIG.get("live_trading", False):
            self._log_attempt({**base, "kind": "LIVE",
                               "outcome": "refused: live_trading off"})
            raise LiveTradingDisabledError(
                "实盘开关未开（config.py live_trading=false）。未发送。"
                "开实盘要先定策略、接好钱包、亲口确认实盘参数。")
        # 风控已在上面过过一遍；发单前再过一次（防止两次调用之间状态变了）
        try:
            self._risk_check(notional)
        except RiskRefusedError as e:
            self._log_attempt({**base, "kind": "LIVE",
                               "outcome": "refused: " + str(e)[:120]})
            raise
        try:
            from py_clob_client_v2 import (OrderArgsV2, OrderType,
                                           PartialCreateOrderOptions, Side)
            client = self._client()
            resp = _call_with_timeout(
                lambda: client.create_and_post_order(
                    OrderArgsV2(token_id=token_id, price=price, size=size,
                                side=Side.BUY if side == "BUY" else Side.SELL),
                    options=PartialCreateOrderOptions(tick_size="0.01"),
                    order_type=OrderType.GTC),
                timeout=30)
        except Exception as e:
            msg = self._scrub(e)[:200]
            self._log_attempt({**base, "kind": "LIVE", "outcome": "failed",
                               "error": msg})
            raise ClobLinkError("发单失败：" + msg)
        if not isinstance(resp, dict):
            self._log_attempt({**base, "kind": "LIVE", "outcome": "bad_response"})
            raise ClobLinkError("发单返回格式不对（不是 JSON 对象），按未成交处理，请用查单确认")
        order_id = resp.get("orderID") or resp.get("orderId") or resp.get("id")
        status = resp.get("status") or ("success" if resp.get("success") else "submitted")
        self._log_attempt({**base, "kind": "LIVE", "outcome": "sent",
                           "order_id": str(order_id)[:64] if order_id else None})
        return {"ok": True, "dry_run": False, "live": True,
                "token_id": token_id, "side": side, "price": price,
                "size": size, "notional": notional,
                "order_id": order_id, "status": status}

    def cancel_order(self, order_id):
        """撤单（实盘操作，同样受 live_trading 开关管）。"""
        if not order_id:
            raise RiskRefusedError("order_id 不能为空")
        if not CONFIG.get("live_trading", False):
            self._log_attempt({"kind": "CANCEL", "order_id": str(order_id)[:64],
                               "outcome": "refused: live_trading off"})
            raise LiveTradingDisabledError(
                "实盘开关未开（config.py live_trading=false）。未发送撤单。")
        try:
            from py_clob_client_v2 import OrderPayload
            client = self._client()
            resp = _call_with_timeout(
                lambda: client.cancel_order(OrderPayload(orderID=str(order_id))),
                timeout=30)
        except Exception as e:
            msg = self._scrub(e)[:200]
            self._log_attempt({"kind": "CANCEL", "order_id": str(order_id)[:64],
                               "outcome": "failed", "error": msg})
            raise ClobLinkError("撤单失败：" + msg)
        self._log_attempt({"kind": "CANCEL", "order_id": str(order_id)[:64],
                           "outcome": "sent"})
        return {"ok": True, "order_id": order_id,
                "result": resp if isinstance(resp, dict) else {"raw": str(resp)[:200]}}


def link_status():
    """给看板用的便捷入口：返回 link_status() dict，绝不抛异常。"""
    try:
        return ClobLink().link_status()
    except Exception as e:
        return {"linked": False, "wallet_address": None, "balance": None,
                "allowance": None, "open_orders": None,
                "error": f"钱包状态查询失败：{str(e)[:150]}"}

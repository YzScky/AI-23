"""Polymarket 行情层：Gamma 发现市场 + CLOB REST 种子盘口 + CLOB WebSocket 实时盘口。只读行情，不下单。"""
import json
import ssl
import time
import threading
import requests
import websocket

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    # 没装 certifi 时用默认证书链（Mac 自带 Python 可能报证书错误，装 certifi 即可）
    _SSL_CONTEXT = ssl.create_default_context()

GAMMA = "https://gamma-api.polymarket.com"
CLOB_BOOK = "https://clob.polymarket.com/book"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def get_json(url, params=None, timeout=15):
    r = requests.get(url, params=params,
                     headers={"Accept": "application/json", "Accept-Encoding": "gzip"},
                     timeout=timeout)
    r.raise_for_status()
    return r.json()


def _parse_ts(s):
    """ISO 时间 -> epoch 秒，解析失败返回 None。"""
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def discover_markets(tags, min_liquidity=1000, max_markets=20):
    """返回 [{market_id, question, token_a, token_b, liquidity, fee_rate,
    tag, start_ts}]，按流动性取前 N。
    注意：Gamma /events 按流动性排序，NBA 比赛盘排在期货盘后面（实测：
    tag_slug=nba 前 100 条全是 2027 冠军这类期货盘，58 场球赛在第 2 页），
    所以必须翻页拉（最多 5 页），只拉 1 页永远发现不了比赛盘。
    start_ts = 事件 startTime（真正开球时间，UTC）；市场 startDate 是上架时间
    （实测：11-11 上架 11-25 的比赛），绝不能当开球时间用。startTime 缺失
    时 start_ts 为 None，策略会跳过这场。"""
    seen = {}
    for tag in tags:
        offset = 0
        for _page in range(5):
            try:
                evs = get_json(f"{GAMMA}/events", {"tag_slug": tag, "limit": 100,
                                                    "closed": "false", "offset": offset})
            except Exception:
                break
            if not evs:
                break
            for ev in evs:
                for m in ev.get("markets", []) or []:
                    if m.get("question") != ev.get("title"):
                        continue
                    toks = json.loads(m.get("clobTokenIds") or "[]")
                    if len(toks) != 2:
                        continue
                    liq = m.get("liquidityClob") or 0
                    if liq < min_liquidity:
                        continue
                    fs = m.get("feeSchedule") or {}
                    seen[m["id"]] = {
                        "market_id": m["id"],
                        "question": m["question"],
                        "condition_id": m.get("conditionId"),
                        "token_a": toks[0],
                        "token_b": toks[1],
                        "liquidity": liq,
                        "fee_rate": fs.get("rate", 0.05),
                        "tag": tag,
                        "start_ts": _parse_ts(ev.get("startTime")),
                    }
            if len(evs) < 100:
                break
            offset += 100
    return sorted(seen.values(), key=lambda x: -x["liquidity"])[:max_markets]


def seed_book(token_id):
    try:
        b = get_json(CLOB_BOOK, {"token_id": token_id})
        bids = b.get("bids", [])
        asks = b.get("asks", [])
        return (max(float(x["price"]) for x in bids) if bids else None,
                min(float(x["price"]) for x in asks) if asks else None)
    except Exception:
        return None, None


class MarketFeed:
    def __init__(self, markets, on_book):
        self.markets = {m["market_id"]: m for m in markets}
        self.tokens = {}
        for m in markets:
            self.tokens[m["token_a"]] = {"market": m["market_id"], "bid": None, "ask": None}
            self.tokens[m["token_b"]] = {"market": m["market_id"], "bid": None, "ask": None}
        self.on_book = on_book
        self.updates = 0
        self.ws = None
        self.lock = threading.Lock()
        self.last_error = None
        self.stop_event = threading.Event()
        self.poll_thread = None

    def _on_error(self, ws, e):
        self.last_error = str(e)
        print("[feed error]", e)

    def seed(self):
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=10) as ex:
            for tok, (bid, ask) in zip(self.tokens, ex.map(seed_book, self.tokens)):
                self.tokens[tok]["bid"], self.tokens[tok]["ask"] = bid, ask

    def _connect_once(self, sslopt):
        self.ws = websocket.WebSocketApp(
            WS_URL,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=lambda w, s, m: print(f"[feed closed] {s} {m}"))
        threading.Thread(
            target=lambda: self.ws.run_forever(ping_interval=20, sslopt=sslopt),
            daemon=True).start()
        for _ in range(150):
            if self.ws.sock and self.ws.sock.connected:
                return True
            time.sleep(0.1)
        return False

    def start(self):
        # 先用验证证书连（Mac 自带 Python 证书链缺失时需要 certifi）；
        # 连不上再退到不验证证书（公司/代理网络中间证书会挡验证，只读行情，不影响资金安全）。
        # 连不上就重试并报清楚原因，不再崩 traceback。
        modes = [("verified", {"context": _SSL_CONTEXT}, 1),
                 ("unverified", {"cert_reqs": ssl.CERT_NONE}, 2)]
        connected = False
        for label, sslopt, tries in modes:
            for attempt in range(1, tries + 1):
                if self._connect_once(sslopt):
                    connected = True
                    if label == "unverified":
                        print("[feed] warning: WS 用未验证证书连接（网络中间证书问题），仅读行情")
                    break
                print(f"[feed] connect {label} attempt {attempt} failed, retrying ...")
                try:
                    self.ws.close()
                except Exception:
                    pass
                time.sleep(1)
            if connected:
                break
        if not connected:
            raise RuntimeError(
                "WebSocket 连不上 wss://ws-subscriptions-clob.polymarket.com。"
                f"最后一次错误：{self.last_error or '连接超时（TCP 建连无响应，可能是网络层拦了 WS）'}。"
                "改用 REST 轮询模式跑：./start.command 已默认 --poll 5")
        if not self.ws.sock or not self.ws.sock.connected:
            raise RuntimeError("WebSocket 刚连上就断了，重试一次 ./start.command")
        self.ws.send(json.dumps({"type": "market", "assets_ids": list(self.tokens)}))

    def start_poll(self, interval=5):
        """REST 轮询模式：WS 被网络拦住时的退路。REST /book 每 interval 秒刷一次盘口，
        更新频率低（5 秒一次），但能跑。每次轮询并行拉全部 token 的盘口。"""
        def loop():
            while not self.stop_event.is_set():
                now = time.time()
                import concurrent.futures as cf
                with cf.ThreadPoolExecutor(max_workers=10) as ex:
                    results = list(ex.map(seed_book, self.tokens))
                with self.lock:
                    for tok, (bid, ask) in zip(self.tokens, results):
                        self.tokens[tok]["bid"], self.tokens[tok]["ask"] = bid, ask
                    self.updates += len(self.tokens)
                    seen = set()
                    for m in self.markets.values():
                        if m["market_id"] in seen:
                            continue
                        seen.add(m["market_id"])
                        self.on_book(m["market_id"], self._market_state(m["market_id"]), now)
                self.stop_event.wait(interval)
        self.poll_thread = threading.Thread(target=loop, daemon=True)
        self.poll_thread.start()

    def stop(self):
        self.stop_event.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    def _on_message(self, ws, msg):
        try:
            d = json.loads(msg)
        except Exception:
            return
        items = d if isinstance(d, list) else [d]
        for item in items:
            self._handle(item)

    def _handle(self, d):
        if not isinstance(d, dict):
            return
        now = time.time()
        if "price_changes" in d:
            for ch in d["price_changes"]:
                tok = ch.get("asset_id")
                if tok not in self.tokens:
                    continue
                with self.lock:
                    self.updates += 1
                    info = self.tokens[tok]
                    if ch.get("best_bid") is not None:
                        info["bid"] = float(ch["best_bid"])
                    if ch.get("best_ask") is not None:
                        info["ask"] = float(ch["best_ask"])
                    self.on_book(info["market"], self._market_state(info["market"]), now)
            return
        # 初始快照：asset_id 是 token；"market" 是 0x 市场地址，不能当 token 用
        tok = d.get("asset_id") or d.get("market")
        if tok not in self.tokens:
            return
        buys = d.get("buys") or d.get("bids") or []
        sells = d.get("sells") or d.get("asks") or []
        with self.lock:
            self.updates += 1
            info = self.tokens[tok]
            if buys:
                info["bid"] = max(float(b["price"]) for b in buys)
            if sells:
                info["ask"] = min(float(s["price"]) for s in sells)
            self.on_book(info["market"], self._market_state(info["market"]), now)

    def _market_state(self, market_id):
        m = self.markets[market_id]
        a = self.tokens[m["token_a"]]
        b = self.tokens[m["token_b"]]
        return {"market_id": market_id, "question": m["question"],
                "a_bid": a["bid"], "a_ask": a["ask"],
                "b_bid": b["bid"], "b_ask": b["ask"],
                "fee_rate": m["fee_rate"], "tag": m.get("tag"),
                "start_ts": m.get("start_ts")}

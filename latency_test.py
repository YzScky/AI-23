"""Polymarket 延迟测试（Mac 上跑）。
测四段，不下真单、不用钱包、不用 API 凭证：
  1. REST 往返：Gamma / CLOB 读盘口，新建连接 vs 长连接复用
  2. WS 建连：从发起连接到收到第一条盘口
  3. WS 消息延迟：服务器时间戳 vs 本地收到时间（有本机时钟偏差，仅供参考）
  4. 本地签名：一次 EIP-712 下单签名耗时（临时生成的废 key，只在内存里，不存盘）
用法：python3 latency_test.py [--samples 20] [--ws-seconds 30]
输出：终端表格 + latency_report.json
"""
import argparse
import json
import ssl
import statistics
import threading
import time

import requests
import websocket

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def get_json(url, params=None, session=None, timeout=15):
    s = session or requests
    r = s.get(url, params=params,
              headers={"Accept": "application/json", "Accept-Encoding": "gzip"},
              timeout=timeout)
    r.raise_for_status()
    return r.json()


def pick_token():
    """按流动性取未结束的 NBA 市场，逐个用 CLOB /price 探活，第一个通的才用。
    （已结束的比赛 token 会被 CLOB 判 Invalid，WS 也不推消息。）"""
    from datetime import datetime, timezone
    try:
        evs = get_json(f"{GAMMA}/events", {"tag_slug": "nba", "limit": 100, "closed": "false"})
        now = datetime.now(timezone.utc)
        cands = []
        for ev in evs:
            for m in ev.get("markets", []) or []:
                if m.get("question") != ev.get("title"):
                    continue
                toks = json.loads(m.get("clobTokenIds") or "[]")
                if len(toks) != 2:
                    continue
                end = m.get("endDate")
                if end:
                    try:
                        if datetime.fromisoformat(end.replace("Z", "+00:00")) <= now:
                            continue
                    except Exception:
                        pass
                cands.append((m.get("liquidityClob") or 0, m["question"], toks[0]))
        cands.sort(key=lambda x: -x[0])
        for liq, q, tok in cands[:10]:
            try:
                get_json(f"{CLOB}/price", {"token_id": tok, "side": "BUY"}, timeout=10)
                return tok, q
            except Exception:
                continue
    except Exception as e:
        print(f"[warn] Gamma 发现市场失败: {e}")
    return None, None


def stats(name, samples):
    samples = sorted(s for s in samples if s is not None)
    if not samples:
        return {"name": name, "n": 0, "median_ms": None, "p95_ms": None,
                "min_ms": None, "max_ms": None}
    def pct(p):
        i = min(len(samples) - 1, int(p * len(samples)))
        return round(samples[i] * 1000, 1)
    return {"name": name, "n": len(samples),
            "median_ms": round(statistics.median(samples) * 1000, 1),
            "p95_ms": pct(0.95),
            "min_ms": round(min(samples) * 1000, 1),
            "max_ms": round(max(samples) * 1000, 1)}


def timeit(fn):
    t0 = time.time()
    fn()
    return time.time() - t0


def test_rest(token, samples_n):
    out = {}
    # 新建连接：每次 new Session（TLS 握手 + TCP 全算进去）
    fresh = []
    for _ in range(samples_n):
        try:
            fresh.append(timeit(lambda: get_json(f"{CLOB}/book", {"token_id": token},
                                                 session=requests.Session())))
        except Exception as e:
            print(f"[warn] REST fresh 失败: {e}")
    out["rest_fresh_connection"] = stats("REST 新建连接 /book", fresh)
    # 长连接复用：同一个 Session
    s = requests.Session()
    reused = []
    for _ in range(samples_n):
        try:
            reused.append(timeit(lambda: get_json(f"{CLOB}/book", {"token_id": token}, session=s)))
        except Exception as e:
            print(f"[warn] REST 复用失败: {e}")
    out["rest_reused_connection"] = stats("REST 长连接复用 /book", reused)
    # CLOB /price（下单前查价走的接口）
    price = []
    for _ in range(samples_n):
        try:
            price.append(timeit(lambda: get_json(f"{CLOB}/price",
                                                 {"token_id": token, "side": "BUY"}, session=s)))
        except Exception as e:
            print(f"[warn] REST /price 失败: {e}")
    out["rest_price"] = stats("REST 长连接 /price", price)
    return out


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def test_ws(token, ws_seconds):
    """返回建连耗时 + 消息延迟分布。连接逻辑与平台 feed.py 同款（轮询 sock.connected）。"""
    try:
        ctx = _ssl_context()
        sslopts = [{"context": ctx}, {"cert_reqs": ssl.CERT_NONE}]
    except Exception:
        sslopts = [{}]
    msg_lag = []
    first_recv = [None]
    lock = threading.Lock()

    def on_msg(ws, msg):
        now = time.time()
        with lock:
            if first_recv[0] is None:
                first_recv[0] = now
        try:
            d = json.loads(msg)
        except Exception:
            return
        items = d if isinstance(d, list) else [d]
        for item in items:
            if not isinstance(item, dict):
                continue
            ts = item.get("timestamp")
            if ts:
                try:
                    lag = now - int(ts) / 1000.0
                    if -5 < lag < 5:  # 过滤时钟偏差过大的脏数据
                        msg_lag.append(lag)
                except Exception:
                    pass

    first_msg_s = None
    for sslopt in sslopts:
        first_recv[0] = None
        ws = websocket.WebSocketApp(
            WS_URL, on_message=on_msg,
            on_error=lambda w, e: print("[ws error]", e),
            on_close=lambda w, s, m: print(f"[ws closed] {s} {m}"))
        t0 = time.time()
        threading.Thread(target=lambda: ws.run_forever(ping_interval=20, sslopt=sslopt),
                         daemon=True).start()
        connected = False
        for _ in range(150):
            if ws.sock and ws.sock.connected:
                connected = True
                break
            time.sleep(0.1)
        if not connected:
            print("[warn] WS 此种证书方式连不上，换下一种重试")
            try:
                ws.close()
            except Exception:
                pass
            continue
        ws.send(json.dumps({"type": "market", "assets_ids": [token]}))
        for _ in range(100):
            if first_recv[0] is not None:
                break
            time.sleep(0.1)
        if first_recv[0] is not None:
            first_msg_s = first_recv[0] - t0
        time.sleep(ws_seconds)
        try:
            ws.close()
        except Exception:
            pass
        break
    return {
        "ws_connect": {"name": "WS 建连到收到第一条盘口", "n": 1 if first_msg_s else 0,
                       "median_ms": round(first_msg_s * 1000, 1) if first_msg_s else None,
                       "p95_ms": None, "min_ms": None, "max_ms": None},
        "ws_msg_lag": stats("WS 消息延迟（服务器时间戳→本地收到）", msg_lag),
        "ws_msg_count": len(msg_lag),
    }


def test_signing(samples_n):
    """本地 EIP-712 签名耗时。没有 eth_account 就跳过。"""
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
    except Exception:
        return {"local_sign": {"name": "本地 EIP-712 签名", "n": 0,
                               "median_ms": None, "p95_ms": None, "min_ms": None,
                               "max_ms": None, "note": "未装 eth-account，跳过；装上后重跑：pip install eth-account"}}
    acct = Account.create()  # 临时废 key，只在内存里
    typed = {
        "types": {"EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"}],
            "Order": [
            {"name": "salt", "type": "uint256"},
            {"name": "maker", "type": "address"},
            {"name": "signer", "type": "address"},
            {"name": "taker", "type": "address"},
            {"name": "tokenId", "type": "uint256"},
            {"name": "makerAmount", "type": "uint256"},
            {"name": "takerAmount", "type": "uint256"},
            {"name": "expiration", "type": "uint256"},
            {"name": "nonce", "type": "uint256"},
            {"name": "feeRateBps", "type": "uint256"},
            {"name": "side", "type": "uint8"},
            {"name": "signatureType", "type": "uint8"}]},
        "primaryType": "Order",
        "domain": {"name": "Polymarket CTF Exchange", "version": "1", "chainId": 137},
        "message": {"salt": 1, "maker": acct.address, "signer": acct.address,
                    "taker": "0x0000000000000000000000000000000000000000",
                    "tokenId": 1, "makerAmount": 100, "takerAmount": 100,
                    "expiration": 9999999999, "nonce": 0, "feeRateBps": 0,
                    "side": 0, "signatureType": 0},
    }
    msg = encode_typed_data(full_message=typed)
    times = []
    for _ in range(samples_n):
        t0 = time.time()
        acct.sign_message(msg)
        times.append(time.time() - t0)
    return {"local_sign": stats("本地 EIP-712 签名", times)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--ws-seconds", type=int, default=30)
    args = ap.parse_args()

    print("=== Polymarket 延迟测试（不下真单，不用钱包）===")
    token, question = pick_token()
    print(f"测试市场: {question or '未知'}")
    if not token:
        print("拿不到测试 token，退出")
        return
    report = {"market": question, "token": token, "samples": args.samples}

    print(f"\n[1/4] REST 往返 x{args.samples} ...")
    report.update(test_rest(token, args.samples))
    print(f"[2/4] WS 建连 + {args.ws_seconds}s 消息 ...")
    report.update(test_ws(token, args.ws_seconds))
    print("[3/4] 本地签名 ...")
    report.update(test_signing(args.samples))

    print("\n=== 结果（毫秒）===")
    for k, v in report.items():
        if isinstance(v, dict) and "median_ms" in v:
            print(f"{v['name']:28s} n={v['n']:>3}  中位数 {v['median_ms']}  p95 {v['p95_ms']}  "
                  f"最小 {v['min_ms']}  最大 {v['max_ms']}"
                  + (f"  ({v['note']})" if v.get("note") else ""))
    if "ws_msg_count" in report:
        print(f"WS 消息样本数: {report['ws_msg_count']}")
    with open("latency_report.json", "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n报告已存 latency_report.json，把它发我，我帮你看瓶颈在哪。")


if __name__ == "__main__":
    main()

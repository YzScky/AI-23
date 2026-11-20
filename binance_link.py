"""binance_link.py — Mac 本地交互式币安合约链接（只跑在你自己的 Mac 上）。

只做两件事：把币安期货 API 凭证写进本地 .env（0600，仅本机可读），
然后验证链接（读余额/持仓/挂单）。绝不打印任何凭证、绝不把凭证发到
聊天、绝不传到币安之外的任何地方。

凭证要求（币安后台 api.binance.com 开 API Key 时勾选）：
- 必须勾：Enable Futures（期货交易权限）
- 必须不勾：Enable Withdrawals（提现）。本平台永远不需要提现权限。
- 建议再勾：IP 白名单（只放你 Mac 的出口 IP），丢了 Key 别人也用不了。

用法（在平台目录里，用平台自己的 venv 跑）：
  .venv/bin/python binance_link.py --test    # 不用凭证：测 fapi + WS 通不通
  .venv/bin/python binance_link.py --check   # 只验证当前 .env 的链接状态
  .venv/bin/python binance_link.py           # 交互式：填 Key/Secret、验证
"""
import argparse
import getpass
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

ENV_PATH = os.path.join(ROOT, ".env")
ENV_KEYS = ["BINANCE_API_KEY", "BINANCE_API_SECRET"]


def _read_env():
    env, order = {}, []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
                order.append(k.strip())
    return env, order


def _write_env(env):
    """写 .env，0600（仅本机当前用户可读）。不打印任何值。
    注意：只写币安这两个键会丢掉 Polymarket 的凭证——所以先读出现有的
    再合并写回，两个板块的凭证共存。"""
    existing, _ = _read_env()
    existing.update({k: v for k, v in env.items() if v})
    all_keys = ["PK", "CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASS_PHRASE",
                "CLOB_API_URL", "POLYMARKET_SIGNATURE_TYPE",
                "BINANCE_API_KEY", "BINANCE_API_SECRET"]
    lines = []
    for k in all_keys:
        if k in existing and existing[k] != "":
            lines.append(f"{k}={existing[k]}")
    fd = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(ENV_PATH, 0o600)
    except Exception:
        pass


def _ask_secret(prompt):
    return getpass.getpass(prompt).strip()


def _print_status(st):
    if st.get("linked"):
        print("链接状态：已链接")
        print(f"USDT 钱包余额：{st.get('balance_usdt') if st.get('balance_usdt') is not None else '—'}")
        print(f"USDT 可用余额：{st.get('available_usdt') if st.get('available_usdt') is not None else '—'}")
        pos = st.get("positions") or []
        if pos:
            for p in pos:
                print(f"持仓：{p.get('symbol')} {p.get('side')} {p.get('qty')} @ "
                      f"{p.get('entry_price')}，浮盈 {p.get('unrealized_pnl')} U")
        else:
            print("持仓：无")
        print(f"未成交挂单：{st.get('open_orders') if st.get('open_orders') is not None else '—'}")
    else:
        print("链接状态：未链接")
        print(f"原因：{st.get('error') or '未知'}")
    return bool(st.get("linked"))


def cmd_test():
    """不用凭证的连通性测试：fapi REST 现价 + WS 标记价流各测一次。
    先跑这个，通了再填凭证。任何一步不通都会明说原因，不重试。"""
    from quant_platform.binance_client import (
        BinanceError, public_filters, public_price)
    print("== 1/2 REST：fapi.binance.com 现价 ==")
    try:
        px = public_price("BTCUSDT")
        print(f"通：BTCUSDT 期货现价 {px}")
    except Exception as e:
        print(f"不通：{str(e)[:200]}")
        print("提示：这台机器的网络到不了 fapi.binance.com（可能要换 VPN 出口）。")
        print("凭证先别填，填了也连不上。")
        return 1
    print("== 2/2 WS：wss://fstream.binance.com 标记价流 ==")
    try:
        from quant_platform.binance_client import BinancePriceStream
        stream = BinancePriceStream(["BTCUSDT"])
        stream.start()
        deadline = time.time() + 15
        got = None
        while time.time() < deadline:
            st = stream.status()
            got = (st.get("prices") or {}).get("BTCUSDT")
            if got:
                break
            if st.get("error") and not st.get("running"):
                break
            time.sleep(0.5)
        stream.stop()
        if got:
            print(f"通：WS 标记价 {got['price']}（{time.time() - got['ts']:.1f} 秒前收到）")
        else:
            print("不通/没收到：WS 连上后 15 秒没收到标记价（这台机器的 WS 可能被拦）。")
            print("不影响下单和查账户（那两样走 REST），只影响看板里的实时价——")
            print("看板会降级成手动刷新取价。")
            return 0
    except Exception as e:
        print(f"WS 测试失败：{str(e)[:150]}")
        return 0
    print()
    print("REST + WS 都通。下一步：跑 .venv/bin/python binance_link.py 填凭证。")
    return 0


def cmd_check():
    from quant_platform.binance_client import BinanceLink
    return 0 if _print_status(BinanceLink().link_status()) else 1


def cmd_interactive():
    env, _ = _read_env()
    have = all(env.get(k) for k in ENV_KEYS)
    if have:
        print("检测到 .env 里已有币安 API 凭证。")
        ans = input("只验证链接？直接回车验证，输 n 重新填凭证：").strip().lower()
        if ans != "n":
            from quant_platform.binance_client import BinanceLink
            return 0 if _print_status(BinanceLink().link_status()) else 1
    print()
    print("填币安期货 API 凭证（在币安后台开 API Key 时勾选）：")
    print("  必须勾：Enable Futures（期货交易权限）")
    print("  必须不勾：Enable Withdrawals（提现）。本平台永远不需要提现权限。")
    print("  建议：IP 白名单只放你 Mac 的出口 IP。")
    print("  输入不回显，只在内存里用、写进本地 .env（0600），绝不打印、绝不外发。")
    key = _ask_secret("API Key（输入不回显）：")
    secret = _ask_secret("API Secret（输入不回显）：")
    if not key or not secret:
        print("有凭证为空，没写。退出。")
        return 1
    env["BINANCE_API_KEY"] = key
    env["BINANCE_API_SECRET"] = secret
    _write_env(env)
    print("已写入 .env（0600，仅本机可读）。")
    from quant_platform.binance_client import BinanceLink
    return 0 if _print_status(BinanceLink().link_status()) else 1


def main():
    ap = argparse.ArgumentParser(description="币安合约链接（Mac 本地交互式）")
    ap.add_argument("--check", action="store_true", help="只验证当前 .env 的链接状态")
    ap.add_argument("--test", action="store_true",
                    help="不用凭证：测 fapi REST + WS 通不通（先跑这个）")
    args = ap.parse_args()
    if args.test:
        return cmd_test()
    if args.check:
        return cmd_check()
    return cmd_interactive()


if __name__ == "__main__":
    sys.exit(main())

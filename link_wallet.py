"""link_wallet.py — Mac 本地交互式钱包链接（只跑在你自己的 Mac 上）。

只做两件事：把 CLOB v2 凭证写进本地 .env（0600，仅本机可读），然后验证链接。
绝不打印任何凭证、绝不把凭证发到聊天、绝不传到 Polymarket 之外的任何地方。

背景（以 py-clob-client-v2 1.2.2 源码 + docs.polymarket.com 为准）：
- L1：钱包私钥 EIP-712 签名 → 派生 API 凭证（create_or_derive_api_key）
- L2：API 凭证 HMAC → 查余额 / 挂单 / 下单 / 撤单
- 官方客户端要求运行时必须有私钥：L2 请求头要带由私钥派生的钱包地址，
  所以查余额、查挂单也要私钥，不只是下单才要。没有网站页面可以点一点
  就生成 API 凭证——官方路径就是用钱包签名派生（下面第 2 条路）。
- 私钥是否存进 .env，由你当场明确选 yes 才存；不存的话，看板余额和下单
  都用不了（会报缺少 PK），直到你改主意再跑一次。

用法（在平台目录里，用平台自己的 venv 跑）：
  .venv/bin/python link_wallet.py            # 交互式：选路、填凭证、验证
  .venv/bin/python link_wallet.py --check    # 只验证当前 .env 的链接状态
  .venv/bin/python link_wallet.py --derive   # 用 .env 里的 PK 派生 API 凭证并写入
"""
import argparse
import getpass
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

ENV_PATH = os.path.join(ROOT, ".env")
ENV_KEYS = ["PK", "CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASS_PHRASE",
            "CLOB_API_URL", "POLYMARKET_SIGNATURE_TYPE"]


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
    """写 .env，0600（仅本机当前用户可读）。不打印任何值。"""
    lines = []
    for k in ENV_KEYS:
        if k in env and env[k] != "":
            lines.append(f"{k}={env[k]}")
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
    v = getpass.getpass(prompt)
    return v.strip()


def _print_status(st):
    if st.get("linked"):
        print("链接状态：已链接")
        print(f"钱包地址：{st.get('wallet_address') or '—'}")
        b = st.get("balance")
        print(f"pUSD 余额：{b if b is not None else '—'}")
        print(f"未成交挂单：{st.get('open_orders') if st.get('open_orders') is not None else '—'}")
    else:
        print("链接状态：未链接")
        print(f"原因：{st.get('error') or '未知'}")
    return bool(st.get("linked"))


def _need_v2():
    try:
        import py_clob_client_v2  # noqa: F401
        return True
    except ImportError:
        print("py-clob-client-v2 未安装：先跑 ./install.command 装依赖，再跑本脚本。")
        return False


def _derive_creds(pk):
    """用私钥派生 API 凭证（只在内存里用，不打印、不写盘——写盘由调用方决定）。"""
    pk = (pk or "").strip()
    if pk.lower().startswith("0x"):
        pk = pk[2:]
    if len(pk) != 64 or any(c not in "0123456789abcdefABCDEF" for c in pk):
        raise ValueError(f"私钥必须是 64 位十六进制字符，现在是 {len(pk)} 位")
    from py_clob_client_v2 import ClobClient
    client = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk)
    return client.create_or_derive_api_key()


def cmd_check():
    from quant_platform.clob_client import ClobLink
    return 0 if _print_status(ClobLink().link_status()) else 1


def cmd_derive():
    if not _need_v2():
        return 1
    env, _ = _read_env()
    pk = env.get("PK") or _ask_secret("输入钱包私钥 PK（输入不回显，只在内存里用，不打印）：")
    if not pk:
        print("私钥为空，退出。")
        return 1
    try:
        creds = _derive_creds(pk)
    except Exception as e:
        print(f"派生失败：{str(e)[:200]}（私钥可能不对，或网络不通）")
        return 1
    env["CLOB_API_KEY"] = creds.api_key
    env["CLOB_SECRET"] = creds.api_secret
    env["CLOB_PASS_PHRASE"] = creds.api_passphrase
    _write_env(env)
    print("API 凭证已派生并写入 .env（不显示具体值）。")
    from quant_platform.clob_client import ClobLink
    return 0 if _print_status(ClobLink().link_status()) else 1


def cmd_interactive():
    if not _need_v2():
        return 1
    env, _ = _read_env()
    have_creds = all(env.get(k) for k in
                     ("CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASS_PHRASE"))
    if have_creds:
        print("检测到 .env 里已有 API 凭证。")
        ans = input("只验证链接？直接回车验证，输 n 重新填凭证：").strip().lower()
        if ans != "n":
            from quant_platform.clob_client import ClobLink
            return 0 if _print_status(ClobLink().link_status()) else 1
    print()
    print("选一条路：")
    print("  1：我已有 API 凭证（apiKey / secret / passphrase），直接填进去")
    print("  2：我没有 API 凭证，用钱包私钥派生（官方路径，私钥只在内存里签名）")
    choice = input("输入 1 或 2：").strip()
    if choice == "1":
        env["CLOB_API_KEY"] = _ask_secret("API Key（输入不回显）：")
        env["CLOB_SECRET"] = _ask_secret("API Secret（输入不回显）：")
        env["CLOB_PASS_PHRASE"] = _ask_secret("API Passphrase（输入不回显）：")
        if not all(env.get(k) for k in ("CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASS_PHRASE")):
            print("有凭证为空，没写。退出。")
            return 1
        _write_env(env)
        print("已写入 .env（0600，仅本机可读）。")
    elif choice == "2":
        print()
        print("说明：官方 v2 客户端要求运行时必须有私钥——")
        print("  派生凭证要私钥签名；查余额/挂单、下单签名也要私钥。")
        print("  私钥只在内存里用，绝不打印；是否存进 .env 由你下一步决定。")
        pk = _ask_secret("输入钱包私钥 PK（输入不回显）：")
        if not pk:
            print("私钥为空，退出。")
            return 1
        try:
            creds = _derive_creds(pk)
        except Exception as e:
            print(f"派生失败：{str(e)[:200]}（私钥可能不对，或网络不通）")
            return 1
        print("API 凭证已派生（不显示具体值）。")
        ans = input("是否把私钥也存入 .env？看板查余额/下单都需要它。存入输入 yes，其他不存：").strip().lower()
        if ans == "yes":
            env["PK"] = pk
            print("私钥已存入 .env（0600，仅本机当前用户可读）。")
        else:
            print("私钥没存。注意：没存私钥的话，查余额/挂单/下单都会报缺少 PK，")
            print("直到你改主意再跑一次本脚本选 yes。")
        env["CLOB_API_KEY"] = creds.api_key
        env["CLOB_SECRET"] = creds.api_secret
        env["CLOB_PASS_PHRASE"] = creds.api_passphrase
        _write_env(env)
        print("API 凭证已写入 .env（0600，仅本机可读）。")
    else:
        print("只认 1 或 2，退出。")
        return 1
    print()
    from quant_platform.clob_client import ClobLink
    return 0 if _print_status(ClobLink().link_status()) else 1


def main():
    ap = argparse.ArgumentParser(description="Polymarket 钱包链接（Mac 本地交互式）")
    ap.add_argument("--check", action="store_true", help="只验证当前 .env 的链接状态")
    ap.add_argument("--derive", action="store_true",
                    help="用 .env 里的 PK 派生 API 凭证并写入（私钥不打印）")
    args = ap.parse_args()
    if args.check:
        return cmd_check()
    if args.derive:
        return cmd_derive()
    return cmd_interactive()


if __name__ == "__main__":
    sys.exit(main())

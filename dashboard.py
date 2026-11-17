"""Polymarket 平台看板（第 6 步）：本地仪表盘 + 平台启停 + 回测触发。

标准库 only（http.server / sqlite3 / subprocess / json），不用 pip 装任何东西。
只绑 127.0.0.1:8931，不对外暴露。纸面模式：看板只读状态、启停已有的 run.py、
触发已有的 backtest.py，不下真单、不连钱包、不新增任何实盘能力。

用法：
  python3 dashboard.py [--port 8931] [--db /path/to/history.db]
  环境变量 POLYMARKET_DB 也可指定 history.db 路径（测试用）。
  启动时先做一次日志轮转（rotate_logs.py）。
"""
import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(ROOT, "platform.log")
PID_PATH = os.path.join(ROOT, "platform.pid")
HTML_PATH = os.path.join(ROOT, "dashboard.html")
BACKTEST_PY = os.path.join(ROOT, "backtest.py")
RUN_PY = os.path.join(ROOT, "run.py")
HOST = "127.0.0.1"
DEFAULT_PORT = 8931

# 启动时轮转一次日志（文件不存在或未超限时什么也不做）
try:
    sys.path.insert(0, ROOT)
    from rotate_logs import rotate as rotate_logs
    rotate_logs(LOG_PATH)
except Exception:
    pass


def db_path():
    return os.environ.get("POLYMARKET_DB") or os.path.join(ROOT, "history.db")


# ---------- 平台进程 ----------

_platform_proc = None  # 本看板进程内启动的 run.py 的 Popen 对象（用于正确回收子进程）


def _reap(pid):
    """如果 pid 是本看板进程的子进程且已退出成僵尸，回收它。返回 True 表示已确认死亡。
    注意：os.kill(pid, 0) 对僵尸进程也返回成功，所以必须先 waitpid，否则停掉的
    进程会一直被误判为“还在运行”。"""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == pid
    except ChildProcessError:
        return False
    except Exception:
        return False


def read_pid():
    try:
        with open(PID_PATH) as f:
            return int(f.read().strip())
    except Exception:
        return None


def pid_alive(pid):
    if not pid:
        return False
    if _reap(pid):
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def platform_running():
    pid = read_pid()
    if pid and pid_alive(pid):
        return pid
    if pid:  # 僵尸 pid 文件：进程已死，清掉
        try:
            os.remove(PID_PATH)
        except Exception:
            pass
    return None


def pick_python():
    """跑 run.py 的 python：优先平台自己的 .venv（和 start.command 一致），没有则用当前 python。"""
    venv_py = os.path.join(ROOT, ".venv", "bin", "python")
    if os.path.exists(venv_py):
        return venv_py
    return sys.executable


def start_platform():
    global _platform_proc
    pid = platform_running()
    if pid:
        return {"ok": False, "error": f"平台已在运行（PID {pid}），先停止再启动"}
    py = pick_python()
    # 注意：引擎 _log() 既 print 又写文件。子进程 stdout 丢弃、stderr 进日志文件，
    # 这样 platform.log 里每行只出现一次（print 的那份不再重复进文件），
    # 而崩溃时的 traceback（走 stderr）仍会被记下来。
    logf = open(LOG_PATH, "a")
    try:
        proc = subprocess.Popen(
            [py, RUN_PY, "--duration", "3600", "--markets", "20", "--poll", "5"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=logf,
            stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        logf.close()
    _platform_proc = proc
    with open(PID_PATH, "w") as f:
        f.write(str(proc.pid))
    return {"ok": True, "pid": proc.pid, "python": py}


def stop_platform():
    global _platform_proc
    pid = read_pid()
    if not pid:
        return {"ok": True, "running": False}
    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    deadline = time.time() + 5  # 等 5 秒优雅退出
    while time.time() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    if pid_alive(pid):  # 还不死就 SIGKILL，再等 2 秒
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        for _ in range(20):
            if not pid_alive(pid):
                break
            time.sleep(0.1)
    _reap(pid)
    _platform_proc = None
    try:
        os.remove(PID_PATH)
    except Exception:
        pass
    return {"ok": True, "running": False, "pid": pid}


# ---------- 日志解析 ----------

HB_RE = re.compile(
    r"\[HB\] updates=(\d+) \(([\d.]+)/s\) "
    r"best_buy=\((None|[\d.]+)(?:, '([^']*)')?\) "
    r"best_sell=\((None|[\d.]+)(?:, '([^']*)')?\) "
    r"cash=([\d.]+) daily_loss=([\d.]+) trades=(\d+)")


def parse_hb(line):
    m = HB_RE.search(line)
    if not m:
        return None
    g = m.groups()
    return {
        "updates": int(g[0]), "updates_per_s": float(g[1]),
        "best_buy": float(g[2]) if g[2] != "None" else None,
        "best_buy_market": g[3],
        "best_sell": float(g[4]) if g[4] != "None" else None,
        "best_sell_market": g[5],
        "cash": float(g[6]), "daily_loss": float(g[7]), "trades": int(g[8]),
    }


def last_hb():
    if not os.path.exists(LOG_PATH):
        return None
    try:
        with open(LOG_PATH, errors="replace") as f:
            lines = f.readlines()[-500:]
    except Exception:
        return None
    for line in reversed(lines):
        hb = parse_hb(line)
        if hb:
            hb["line"] = line.strip()
            return hb
    return None


def last_log_lines(n=100):
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, errors="replace") as f:
            return [l.rstrip("\n") for l in f.readlines()[-n:]]
    except Exception:
        return []


# ---------- history.db 只读查询 ----------

def query_db():
    """返回 (counts, markets)。DB 不存在 / 表不存在 / 被锁 → 空状态，绝不抛异常。"""
    path = db_path()
    if not os.path.exists(path):
        return {"exists": False, "books": 0, "trades": 0, "size_kb": 0.0}, []
    size_kb = os.path.getsize(path) / 1024.0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            books = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
            trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            rows = conn.execute(
                "SELECT b.ts, b.market_id, b.question, b.a_bid, b.a_ask, "
                "b.b_bid, b.b_ask, b.fee_rate FROM books b JOIN "
                "(SELECT market_id, MAX(ts) m FROM books GROUP BY market_id) x "
                "ON b.market_id = x.market_id AND b.ts = x.m "
                "ORDER BY b.market_id").fetchall()
        finally:
            conn.close()
    except Exception:
        return {"exists": True, "books": 0, "trades": 0, "size_kb": round(size_kb, 1)}, []
    markets = []
    for r in rows:
        a_bid, a_ask, b_bid, b_ask = r[3], r[4], r[5], r[6]
        buy_both = round(a_ask + b_ask, 4) if (a_ask is not None and b_ask is not None) else None
        sell_both = round(a_bid + b_bid, 4) if (a_bid is not None and b_bid is not None) else None
        markets.append({
            "ts": r[0], "market_id": r[1], "question": r[2],
            "a_bid": a_bid, "a_ask": a_ask, "b_bid": b_bid, "b_ask": b_ask,
            "fee_rate": r[7], "buy_both": buy_both, "sell_both": sell_both,
            "arb": (buy_both is not None and buy_both <= 0.99) or
                   (sell_both is not None and sell_both >= 1.01),
        })
    return {"exists": True, "books": books, "trades": trades,
            "size_kb": round(size_kb, 1)}, markets


# ---------- 回测 ----------

def run_backtest(params):
    """调已有的 backtest.py 子进程，取它最后输出的 JSON 块返回。"""
    def num(key, default, lo, hi):
        try:
            v = float(params.get(key, default))
        except (TypeError, ValueError):
            return default
        if v != v or v in (float("inf"), float("-inf")):
            return default
        return min(max(v, lo), hi)

    args = [sys.executable, BACKTEST_PY, "--db", db_path(),
            "--buy-threshold", str(num("buy_threshold", 0.99, 0.5, 1.5)),
            "--sell-threshold", str(num("sell_threshold", 1.01, 0.5, 1.5)),
            "--size", str(num("size", 200, 1, 100000)),
            "--bankroll", str(num("bankroll", 1000, 1, 10000000)),
            "--daily-loss-cap", str(num("daily_loss_cap", 100, 0, 1000000))]
    try:
        proc = subprocess.run(args, cwd=ROOT, capture_output=True,
                              text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "回测超时（300 秒未完成），数据量可能太大"}
    out = proc.stdout or ""
    lines = out.splitlines()
    json_start = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "{":
            json_start = i
            break
    if json_start is not None:
        try:
            return {"ok": True, "result": json.loads("\n".join(lines[json_start:]))}
        except Exception:
            pass
    tail = (out + "\n" + (proc.stderr or "")).strip().splitlines()[-5:]
    return {"ok": False, "error": "回测未返回结果：" + " / ".join(tail)}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # 看板自己的日志不刷屏

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        try:
            with open(HTML_PATH, "rb") as f:
                body = f.read()
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"dashboard.html 读不到：{e}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            p = urlparse(self.path).path
            if p in ("/", "/index.html"):
                self._send_html()
            elif p == "/api/status":
                counts, markets = query_db()
                pid = platform_running()
                self._send_json(200, {
                    "ok": True, "running": pid is not None, "pid": pid,
                    "hb": last_hb(), "markets": markets,
                    "db": counts, "log_exists": os.path.exists(LOG_PATH),
                })
            elif p == "/api/history":
                counts, markets = query_db()
                self._send_json(200, {"ok": True, "db": counts, "markets": markets})
            elif p == "/api/logs":
                self._send_json(200, {"ok": True, "lines": last_log_lines(100)})
            else:
                self._send_json(404, {"ok": False, "error": f"没有这个接口：{p}"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                params = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                params = {}
            p = urlparse(self.path).path
            if p == "/api/platform/start":
                self._send_json(200, start_platform())
            elif p == "/api/platform/stop":
                self._send_json(200, stop_platform())
            elif p == "/api/backtest":
                self._send_json(200, run_backtest(params if isinstance(params, dict) else {}))
            else:
                self._send_json(404, {"ok": False, "error": f"没有这个接口：{p}"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})


def main():
    ap = argparse.ArgumentParser(description="Polymarket 平台看板（只限本机访问）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", default=None, help="history.db 路径（默认平台根目录；也可设 POLYMARKET_DB）")
    args = ap.parse_args()
    if args.db:
        os.environ["POLYMARKET_DB"] = args.db
    srv = ThreadingHTTPServer((HOST, args.port), Handler)
    srv.daemon_threads = True
    print(f"看板已启动：http://{HOST}:{args.port}（只限本机访问，Ctrl+C 停止）", flush=True)
    print(f"history.db：{db_path()}{'（不存在，先跑平台攒数据）' if not os.path.exists(db_path()) else ''}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n看板已停止（平台进程不受影响，仍在后台跑的话请用看板或 kill 停它）。")


if __name__ == "__main__":
    main()

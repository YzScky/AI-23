"""日志轮转：platform.log 超过 5 MB 时轮转，保留最近 5 个归档（.1 ~ .5）。
标准库 only。引擎每次写日志都重新 open 文件，所以轮转时不需要停平台。

用法：
  python3 rotate_logs.py [日志路径]     # 默认平台根目录的 platform.log
  看板（dashboard.py）启动时会自动调一次；也可以手动跑，或加到 crontab 每天跑一次。
"""
import os
import sys

MAX_BYTES = 5 * 1024 * 1024
KEEP = 5


def rotate(log_path):
    """返回 (rotated: bool, message: str)。文件不存在或未超限 → 不做任何事。"""
    if not os.path.exists(log_path):
        return False, f"日志不存在，跳过：{log_path}"
    try:
        size = os.path.getsize(log_path)
    except OSError as e:
        return False, f"读不到日志大小：{e}"
    if size <= MAX_BYTES:
        return False, f"日志 {size / 1024:.0f} KB，未超 5 MB，不轮转"
    # 最老的归档先删，再逐个后移：.4 -> .5，.3 -> .4 ……
    oldest = f"{log_path}.{KEEP}"
    if os.path.exists(oldest):
        os.remove(oldest)
    for i in range(KEEP - 1, 0, -1):
        src, dst = f"{log_path}.{i}", f"{log_path}.{i + 1}"
        if os.path.exists(src):
            os.rename(src, dst)
    os.rename(log_path, f"{log_path}.1")
    # 新日志文件留空，等引擎下次写日志时自动创建
    return True, f"已轮转：{log_path}（{size / 1024:.0f} KB）→ {log_path}.1，保留最近 {KEEP} 个归档"


if __name__ == "__main__":
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform.log")
    path = sys.argv[1] if len(sys.argv) > 1 else default
    _, msg = rotate(path)
    print(msg)

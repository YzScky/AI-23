#!/bin/bash
cd "$(dirname "$0")"
# 默认走 REST 轮询（5 秒一次）：这台 Mac 的网络拦住了 Polymarket 的 WebSocket，
# TCP 直连超时，证书无关。WS 恢复后可去掉 --poll 5 改走实时。
.venv/bin/python run.py --duration 600 --markets 20 --poll 5

#!/bin/bash
cd "$(dirname "$0")"
set -e
if command -v python3.11 >/dev/null 2>&1; then
  PY=python3.11
elif command -v python3.12 >/dev/null 2>&1; then
  PY=python3.12
else
  PY=python3
fi
$PY -m venv .venv
.venv/bin/pip install websocket-client requests certifi py-clob-client-v2
echo "安装完成。双击 start.command 开始跑（纸面模式，不下真单）。"

#!/bin/bash
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install -q websocket-client requests certifi
echo "安装完成。双击 start.command 开始跑（纸面模式，不下真单）。"

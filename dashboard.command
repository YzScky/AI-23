#!/bin/bash
cd "$(dirname "$0")"
echo "看板：http://127.0.0.1:8931（只限本机访问，Ctrl+C 停止）"
python3 dashboard.py

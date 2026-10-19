#!/bin/bash
# 医药 Query 安全扩召回审核工作台 · 一键启动
# 双击运行，或终端执行本文件；启动后浏览器打开 http://127.0.0.1:8877
set -e
WORKBENCH_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$WORKBENCH_DIR"
echo "=============================================="
echo " 医药 Query 安全扩召回审核工作台"
echo " 启动后请打开 http://127.0.0.1:8877"
echo " 退出请按 Ctrl+C"
echo "=============================================="
exec python3 "$WORKBENCH_DIR/workbench/server.py"

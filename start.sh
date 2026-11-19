#!/usr/bin/env bash
# 启动 CLI 命令管理器：默认 http://127.0.0.1:8977
# 用法:
#   ./start.sh                 # 默认端口 8977
#   ./start.sh --port 8980     # 换端口
#   ./start.sh --check         # 只校验 modules/*.json 后退出
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
exec python3 -m cli_manager "$@"

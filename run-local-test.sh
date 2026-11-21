#!/bin/bash
# 本机快速测试（不经过 fnpack）
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA="${DATA_DIR:-/tmp/fnos-lxc-data}"
PORT="${PORT:-9100}"
mkdir -p "$DATA"
echo "data=$DATA port=$PORT"
echo "浏览器打开: http://127.0.0.1:${PORT}/（仅调试：TCP 模式无身份认证，默认只绑定本机）"
exec python3 "$ROOT/app/server/main.py" --port "$PORT" --data-dir "$DATA" --bind "${BIND:-127.0.0.1}"

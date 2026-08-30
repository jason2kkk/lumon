#!/usr/bin/env bash
# 本地开发：仅启动后端（8001 + reload）
#
# 用法：
#   ./scripts/start-local-dev.sh
# 前端另开终端：
#   cd frontend && npm run dev
#
# 本地前端 http://127.0.0.1:5173 → API http://127.0.0.1:8001
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOCAL_PORT="${LOCAL_API_PORT:-8001}"

if lsof -i ":${LOCAL_PORT}" -t >/dev/null 2>&1; then
  echo "端口 ${LOCAL_PORT} 已被占用。若已是本脚本启动的后端，可直接使用。"
  echo "否则请: lsof -i :${LOCAL_PORT} 查看进程"
  exit 1
fi

if [[ ! -x "${ROOT}/.venv/bin/python" ]]; then
  echo "错误: 缺少 .venv" >&2
  exit 1
fi

echo ">>> 本地开发后端: http://127.0.0.1:${LOCAL_PORT} (reload)"
echo ">>> 请另开终端: cd frontend && npm run dev  →  http://127.0.0.1:5173"
echo ""

exec "${ROOT}/.venv/bin/python" -m uvicorn server:app \
  --reload \
  --host 127.0.0.1 \
  --port "${LOCAL_PORT}"

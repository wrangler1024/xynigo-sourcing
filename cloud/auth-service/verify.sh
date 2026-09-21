#!/usr/bin/env bash
# 云端 auth-service 测试两档入口（20260921 Jeff 拍板简化流程）：
#   fast —— 受影响模块组：店铺授权 + 结算全家（~2 分钟），
#           日常改云端口径/店铺授权/结算的部署前验证用它。
#   full —— 全量（~10-15 分钟）：合并 main 前、发版前、或改动不在 fast
#           覆盖范围内时必跑。
# 用法：./verify.sh [fast|full]   （默认 fast）
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "环境未就绪：先 uv sync --frozen && uv pip install pytest" >&2
  exit 2
fi

MODE="${1:-fast}"
case "$MODE" in
  fast)
    exec "$PY" -m pytest \
      tests/test_shein_store_auth.py \
      tests/test_shein_settlement_service.py \
      tests/test_shein_settlement_sync.py \
      tests/test_shein_settlement_api.py \
      tests/test_shein_settlement_export.py \
      tests/test_shein_settlement_client.py \
      tests/test_shein_settlement_worker.py \
      -q --tb=short
    ;;
  full)
    exec "$PY" -m pytest tests -q --tb=short
    ;;
  *)
    echo "usage: verify.sh [fast|full]" >&2
    exit 2
    ;;
esac

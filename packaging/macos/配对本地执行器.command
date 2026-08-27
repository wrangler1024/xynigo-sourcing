#!/bin/bash
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
DATA_DIR="$HOME/Library/Application Support/XynigoSourcing"
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR" 2>/dev/null || true
export XYNIGO_DATA_DIR="$DATA_DIR"
export XYNIGO_INSTALL_DIR="$SCRIPT_DIR"
export XYNIGO_INSTALL_MODE=standard
echo "请从云端 Web 的“系统管理 → 本地执行器”复制 8 位配对码。"
read -r -p "配对码：" XYNIGO_PAIR_CODE
if [ -z "$XYNIGO_PAIR_CODE" ]; then
  echo "未输入配对码。"
  exit 2
fi
"$SCRIPT_DIR/runtime/xynigo-sourcing" pair "$XYNIGO_PAIR_CODE"
XYNIGO_EXIT_CODE=$?
if [ "$XYNIGO_EXIT_CODE" -eq 0 ]; then
  touch "$DATA_DIR/.standard-launched"
  echo "配对完成，正在启动本地执行器。"
  exec "$SCRIPT_DIR/runtime/xynigo-sourcing"
fi
echo "配对失败，退出码：$XYNIGO_EXIT_CODE"
read -r
exit "$XYNIGO_EXIT_CODE"

#!/bin/bash
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
DATA_DIR="$HOME/Library/Application Support/XynigoSourcing"
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR" 2>/dev/null || true
export XYNIGO_DATA_DIR="$DATA_DIR"
export XYNIGO_INSTALL_DIR="$SCRIPT_DIR"
export XYNIGO_INSTALL_MODE=standard
echo "Xynigo Sourcing 本地执行器启动中…"
echo "业务工作台在云端；保持此窗口开启。"
"$SCRIPT_DIR/runtime/xynigo-sourcing"
XYNIGO_EXIT_CODE=$?
if [ "$XYNIGO_EXIT_CODE" -ne 0 ]; then
  echo "启动失败，退出码：$XYNIGO_EXIT_CODE"
  echo "按回车键关闭窗口。"
  read -r
fi
exit "$XYNIGO_EXIT_CODE"

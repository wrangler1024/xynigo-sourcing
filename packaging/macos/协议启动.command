#!/bin/bash
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
DATA_DIR="$HOME/Library/Application Support/XynigoSourcing"
REQUEST_FILE="$DATA_DIR/protocol-request.txt"
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR" 2>/dev/null || true
export XYNIGO_DATA_DIR="$DATA_DIR"
export XYNIGO_INSTALL_DIR="$SCRIPT_DIR"
export XYNIGO_INSTALL_MODE=standard
if [ -L "$REQUEST_FILE" ] || [ ! -f "$REQUEST_FILE" ]; then
  echo "启动请求不存在或不安全，已停止。"
  read -r
  exit 2
fi
REQUEST_OWNER="$(stat -f '%u' "$REQUEST_FILE" 2>/dev/null || true)"
if [ "$REQUEST_OWNER" != "$(id -u)" ]; then
  echo "启动请求不属于当前用户，已停止。"
  rm -f "$REQUEST_FILE"
  read -r
  exit 2
fi
XYNIGO_PROTOCOL_URI=""
IFS= read -r XYNIGO_PROTOCOL_URI < "$REQUEST_FILE" || true
rm -f "$REQUEST_FILE"
if [ -z "$XYNIGO_PROTOCOL_URI" ]; then
  echo "启动请求为空，已停止。"
  read -r
  exit 2
fi
touch "$DATA_DIR/.standard-launched"
"$SCRIPT_DIR/runtime/xynigo-sourcing" protocol "$XYNIGO_PROTOCOL_URI"
XYNIGO_EXIT_CODE=$?
if [ "$XYNIGO_EXIT_CODE" -ne 0 ]; then
  echo "协议启动失败，退出码：$XYNIGO_EXIT_CODE"
  echo "按回车键关闭窗口。"
  read -r
fi
exit "$XYNIGO_EXIT_CODE"

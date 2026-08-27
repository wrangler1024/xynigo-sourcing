#!/bin/bash
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
DATA_DIR="$HOME/Library/Application Support/XynigoSourcing"
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR" 2>/dev/null || true
export XYNIGO_DATA_DIR="$DATA_DIR"
export XYNIGO_INSTALL_DIR="$SCRIPT_DIR"
export XYNIGO_INSTALL_MODE=standard
SOURCE_DIR="$(/usr/bin/osascript -e 'POSIX path of (choose folder with prompt "请选择已解压的 Xynigo 绿色包目录")' 2>/dev/null || true)"
if [ -z "$SOURCE_DIR" ]; then
  echo "未选择绿色包目录，迁移已取消。"
  exit 0
fi
"$SCRIPT_DIR/runtime/xynigo-sourcing" migrate "$SOURCE_DIR"
XYNIGO_EXIT_CODE=$?
if [ "$XYNIGO_EXIT_CODE" -eq 0 ]; then
  touch "$DATA_DIR/.standard-launched"
  echo "迁移完成，正在启动本地执行器。"
  exec "$SCRIPT_DIR/runtime/xynigo-sourcing"
fi
echo "迁移失败，未修改绿色包中的原数据。"
echo "按回车键关闭窗口。"
read -r
exit "$XYNIGO_EXIT_CODE"

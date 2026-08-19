#!/bin/bash
# macOS 源码开发启动器；发行绿色包使用包内的同名脚本。
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)/src"
if [ -x ".venv/bin/python" ]; then
  XYNIGO_PYTHON=".venv/bin/python"
else
  XYNIGO_PYTHON="python3"
fi
echo "Xynigo Sourcing 源码版启动中，浏览器将自动打开操作页面..."
echo "保持此窗口开启；关闭窗口即退出工具。"
"$XYNIGO_PYTHON" -m purchase_tool

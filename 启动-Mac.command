#!/bin/bash
# Mac 版启动器：双击运行，自动打开浏览器操作页面。
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)/src"
echo "Xynigo Sourcing 启动中，浏览器将自动打开操作页面..."
echo "保持此窗口开启；关闭窗口即退出工具。"
python3 -m purchase_tool

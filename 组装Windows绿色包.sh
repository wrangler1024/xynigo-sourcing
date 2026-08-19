#!/bin/bash
# 组装 Windows 绿色免安装包：内嵌官方 embeddable Python + 纯 Python 依赖 + 工具代码。
# 在 Mac 上执行即可产出 zip，无需 Windows 机器：
#   bash 组装Windows绿色包.sh
# 产物：dist/Xynigo_Sourcing_Windows_<日期>_<版本>.zip
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
STAGE="$(mktemp -d)/Xynigo-Sourcing"
mkdir -p "$STAGE"
PYVER=3.11.9

echo "[1/5] 下载 Windows embeddable Python ${PYVER} ..."
mkdir -p .cache
EMB=".cache/python-${PYVER}-embed-amd64.zip"
if [ ! -f "$EMB" ]; then
  curl -fL -o "$EMB" "https://www.python.org/ftp/python/${PYVER}/python-${PYVER}-embed-amd64.zip"
fi
mkdir -p "$STAGE/python-embed"
unzip -q "$EMB" -d "$STAGE/python-embed"

echo "[2/5] 安装纯 Python 依赖（websocket-client / openpyxl）..."
pip3 install --quiet --target "$STAGE/deps" websocket-client openpyxl

echo "[3/5] 拷贝工具代码 ..."
# 必须先建 app 再往里拷：macOS cp -R 在目标不存在时会把包内容摊平成 app/
# 本身（20260818 实测同事机器报 No module named purchase_tool 的根因）
mkdir -p "$STAGE/app"
cp -R src/purchase_tool "$STAGE/app/purchase_tool"

echo "[4/5] 配置 embeddable Python 路径与启动脚本 ..."
# ._pth 只保留解释器自身路径；代码/依赖路径由启动命令显式注入
# （._pth 相对路径在不同 Windows 上解析有歧义，实测 20260818 同事机器上
#   ..\app 未生效报 No module named purchase_tool，改为 -c 显式 sys.path 最稳）
PTH="$STAGE/python-embed/python311._pth"
cat > "$PTH" <<EOF
python311.zip
.

# Uncomment to run site.main() automatically
import site
EOF
# bat 必须是 GBK+CRLF（中文 Windows 的 cmd 用 GBK 解析批处理，UTF-8 中文会乱码致命令失效）
# bat 只负责拉起 python 跑包根的 run.py；路径定位/诊断都在 run.py 里（按自身绝对路径，免疫 cwd）
STAGE="$STAGE" python3 - <<'EOF'
import os

content = (
    '@echo off\r\n'
    'cd /d "%~dp0"\r\n'
    'echo Xynigo Sourcing v0.4.2 启动中，浏览器将自动打开操作页面...\r\n'
    'echo 保持此窗口开启；关闭窗口即退出工具。\r\n'
    'python-embed\\python.exe run.py\r\n'
    'pause\r\n'
)
path = os.path.join(os.environ['STAGE'], '启动.bat')
with open(path, 'wb') as f:
    f.write(content.encode('gbk'))

run_py = '''# -*- coding: utf-8 -*-
"""启动入口：按本文件绝对路径定位 deps/app，导入失败时打印目录诊断。"""
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'deps'))
sys.path.insert(0, os.path.join(ROOT, 'app'))

try:
    from purchase_tool import bootstrap
except Exception:
    app_dir = os.path.join(ROOT, 'app')
    pkg_dir = os.path.join(app_dir, 'purchase_tool')
    print('启动目录:', ROOT)
    print('app 目录存在:', os.path.isdir(app_dir))
    print('purchase_tool 目录存在:', os.path.isdir(pkg_dir))
    if os.path.isdir(app_dir):
        print('app 下内容:', sorted(os.listdir(app_dir)))
    if os.path.isdir(pkg_dir):
        print('包内文件数:', len(os.listdir(pkg_dir)))
    print('deps 目录存在:', os.path.isdir(os.path.join(ROOT, 'deps')))
    print('--- 详细报错 ---')
    traceback.print_exc()
    sys.exit(1)
'''
with open(os.path.join(os.environ['STAGE'], 'run.py'), 'w',
          encoding='utf-8') as f:
    f.write(run_py)
EOF

echo "[5/5] 打包 zip（Python zipfile，UTF-8 文件名，Windows 解压不乱码）..."
mkdir -p dist
STAMP="$(date +%Y%m%d)"
ZIP="dist/Xynigo_Sourcing_Windows_${STAMP}_v0.4.2.zip"
rm -f "$ZIP"
STAGE="$STAGE" ROOT_ZIP="$ROOT/$ZIP" python3 - <<'EOF'
import os
import zipfile

stage = os.environ['STAGE']
root = os.environ['ROOT_ZIP']
base = os.path.dirname(stage)
with zipfile.ZipFile(root, 'w', zipfile.ZIP_DEFLATED) as z:
    for dirpath, _dirs, files in os.walk(stage):
        for f in files:
            p = os.path.join(dirpath, f)
            z.write(p, os.path.relpath(p, base))
EOF
echo "完成：$ROOT/$ZIP"
du -sh "$ROOT/$ZIP"

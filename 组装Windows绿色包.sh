#!/bin/bash
# Build the complete Windows green package and its SHA-256 update manifest.
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$(pwd)"
VERSION="$(PYTHONPATH=src python3 -c 'from purchase_tool import __version__; print(__version__)')"
CHANNEL="${XYNIGO_RELEASE_CHANNEL:-stable}"
case "$CHANNEL" in
  stable|test) ;;
  *) echo "XYNIGO_RELEASE_CHANNEL must be stable or test: $CHANNEL" >&2; exit 2 ;;
esac
PYVER=3.11.9
STAGE="$(mktemp -d)/Xynigo-Sourcing"
STAMP="$(date +%Y%m%d)"
ZIP_NAME="Xynigo_Sourcing_Windows_${STAMP}_v${VERSION}.zip"
ZIP="dist/${ZIP_NAME}"
MANIFEST="dist/Xynigo_Sourcing_v${VERSION}_update.json"
SHA_FILE="dist/Xynigo_Sourcing_v${VERSION}_SHA256SUMS.txt"

mkdir -p "$STAGE" dist .cache

echo "[1/7] Download Windows embeddable Python ${PYVER} ..."
EMB=".cache/python-${PYVER}-embed-amd64.zip"
if [ ! -f "$EMB" ]; then
  curl -fL -o "$EMB" "https://www.python.org/ftp/python/${PYVER}/python-${PYVER}-embed-amd64.zip"
fi
mkdir -p "$STAGE/python-embed"
unzip -q "$EMB" -d "$STAGE/python-embed"

echo "[2/7] Install pure-Python dependencies ..."
python3 -m pip install --quiet --upgrade --target "$STAGE/deps" \
  "websocket-client>=1.6,<2" "openpyxl>=3.1,<4"

echo "[3/7] Copy application and updater ..."
mkdir -p "$STAGE/app"
cp -R src/purchase_tool "$STAGE/app/purchase_tool"
HELPER_SOURCE="$ROOT/packaging/windows/update-helper.ps1" \
HELPER_TARGET="$STAGE/update-helper.ps1" python3 - <<'PY'
import os
from pathlib import Path

text = Path(os.environ['HELPER_SOURCE']).read_text(encoding='utf-8')
Path(os.environ['HELPER_TARGET']).write_text(text, encoding='utf-8-sig')
PY

echo "[4/7] Write launchers and package metadata ..."
PTH="$STAGE/python-embed/python311._pth"
cat > "$PTH" <<EOF
python311.zip
.

import site
EOF

STAGE="$STAGE" VERSION="$VERSION" CHANNEL="$CHANNEL" python3 - <<'PY'
import json
import os

stage = os.environ['STAGE']
version = os.environ['VERSION']
channel = os.environ['CHANNEL']

def launcher(command, mode_text):
    return (
        '@echo off\r\n'
        'setlocal\r\n'
        'cd /d "%%~dp0"\r\n'
        'echo Xynigo Sourcing v%s 启动中...\r\n'
        'echo %s\r\n'
        'echo 保持此窗口开启；关闭窗口即退出工具。\r\n'
        '%s\r\n'
        'set "XYNIGO_EXIT_CODE=%%ERRORLEVEL%%"\r\n'
        'if "%%XYNIGO_EXIT_CODE%%"=="42" exit /b 0\r\n'
        'if not "%%XYNIGO_EXIT_CODE%%"=="0" echo 启动失败，退出码：%%XYNIGO_EXIT_CODE%%\r\n'
        'pause\r\n'
    ) % (version, mode_text, command)


bat = launcher(
    'python-embed\\python.exe run.py',
    '默认打开云端工作台；本地执行器同时运行。',
)
with open(os.path.join(stage, '启动.bat'), 'wb') as handle:
    handle.write(bat.encode('gbk'))
local_bat = launcher(
    'python-embed\\python.exe run.py --local-ui',
    '打开本机兼容界面，用于 HubStudio 与故障排查。',
)
with open(os.path.join(stage, '启动-本地执行器.bat'), 'wb') as handle:
    handle.write(local_bat.encode('gbk'))

run_py = '''# -*- coding: utf-8 -*-
"""Windows green-package entry point."""
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'deps'))
sys.path.insert(0, os.path.join(ROOT, 'app'))

try:
    os.environ.setdefault('XYNIGO_INSTALL_DIR', ROOT)
    from purchase_tool import bootstrap
except SystemExit:
    raise
except Exception:
    app_dir = os.path.join(ROOT, 'app')
    pkg_dir = os.path.join(app_dir, 'purchase_tool')
    print('启动目录:', ROOT)
    print('app 目录存在:', os.path.isdir(app_dir))
    print('purchase_tool 目录存在:', os.path.isdir(pkg_dir))
    print('deps 目录存在:', os.path.isdir(os.path.join(ROOT, 'deps')))
    print('--- 详细报错 ---')
    traceback.print_exc()
    sys.exit(1)
'''
with open(os.path.join(stage, 'run.py'), 'w', encoding='utf-8') as handle:
    handle.write(run_py)

version_info = {
    'schemaVersion': 2,
    'product': 'Xynigo Sourcing',
    'version': version,
    'channel': channel,
    'platform': 'windows-x86_64',
    'repository': 'wrangler1024/xynigo-sourcing',
    'managedPaths': [
        'app', 'deps', 'python-embed', 'run.py', '启动.bat',
        '启动-本地执行器.bat',
        'update-helper.ps1', 'VERSION.json', '使用说明.txt',
    ],
    'preservedPaths': [
        'config.json', '查询日志', '日志', 'logs', '运行数据',
        'data', '数据', 'imports', '导入文件',
    ],
}
with open(os.path.join(stage, 'VERSION.json'), 'w', encoding='utf-8') as handle:
    json.dump(version_info, handle, ensure_ascii=False, indent=2)

guide = '''Xynigo Sourcing v%s Windows 绿色包

1. 必须先完整解压 ZIP，不能直接在压缩包中双击。
2. 双击“启动.bat”默认打开云端工作台；本地执行器仍在此窗口运行。
3. 需要使用旧本机界面或排查 HubStudio 时，双击“启动-本地执行器.bat”。
4. 页面右上角会检查 GitHub 最新稳定版；发现新版本时点击提醒，回到运行窗口输入 Y 更新或 N 暂不更新。
5. 更新不需要 Git 或 GitHub 账号；校验失败或网络不可用时当前版本会继续运行。
6. config.json、查询日志、运行数据和用户导入文件不会被更新覆盖或上传。
7. 更新失败时程序会从本机备份自动回滚并重新启动。
''' % version
with open(os.path.join(stage, '使用说明.txt'), 'w', encoding='utf-8') as handle:
    handle.write(guide)
PY

echo "[5/7] Validate package safety and structure ..."
STAGE="$STAGE" python3 - <<'PY'
import os
from pathlib import Path

root = Path(os.environ['STAGE'])
required = [
    'app/purchase_tool/main.py', 'app/purchase_tool/updater.py',
    'python-embed/python.exe', 'deps/openpyxl', 'run.py', '启动.bat',
    '启动-本地执行器.bat',
    'update-helper.ps1', 'VERSION.json', '使用说明.txt',
]
missing = [name for name in required if not (root / name).exists()]
if missing:
    raise SystemExit('missing package files: ' + ', '.join(missing))

forbidden_names = {
    'config.json', '启动日志.txt', '.env', '.git', '查询日志',
    '运行数据', 'logs', '日志', 'imports', '导入文件',
}
found = []
for path in root.rglob('*'):
    if path.name in forbidden_names:
        found.append(str(path.relative_to(root)))
if found:
    raise SystemExit('forbidden runtime files in package: ' + ', '.join(found))
PY

echo "[6/7] Create Windows ZIP ..."
rm -f "$ZIP" "$MANIFEST" "$SHA_FILE"
STAGE="$STAGE" ROOT_ZIP="$ROOT/$ZIP" python3 - <<'PY'
import os
import zipfile

stage = os.environ['STAGE']
target = os.environ['ROOT_ZIP']
base = os.path.dirname(stage)
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
    for dirpath, dirs, files in os.walk(stage):
        dirs[:] = [name for name in dirs if name != '__pycache__']
        for name in files:
            if name.endswith(('.pyc', '.pyo')):
                continue
            path = os.path.join(dirpath, name)
            archive.write(path, os.path.relpath(path, base))
PY

echo "[7/7] Generate SHA-256 and update manifest ..."
python3 scripts/update_release_assets.py \
  --version "$VERSION" --channel "$CHANNEL" \
  --platform "windows-x86_64" --asset "$ZIP" \
  --notes "release/v${VERSION}.zh-CN.json" \
  --manifest "$MANIFEST" --sha-file "$SHA_FILE"

du -sh "$ZIP"

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
BUILD_LABEL="${XYNIGO_BUILD_LABEL:-}"
if [ -n "$BUILD_LABEL" ] && ! [[ "$BUILD_LABEL" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "XYNIGO_BUILD_LABEL only accepts letters, numbers, dot, underscore and dash." >&2
  exit 2
fi
PYVER=3.11.9
STAGE="$(mktemp -d)/Xynigo-Sourcing"
STAMP="$(date +%Y%m%d)"
BUILD_SUFFIX=""
if [ -n "$BUILD_LABEL" ]; then
  BUILD_SUFFIX="_${BUILD_LABEL}"
fi
ZIP_NAME="Xynigo_Sourcing_Windows_${STAMP}_v${VERSION}${BUILD_SUFFIX}.zip"
ZIP="dist/${ZIP_NAME}"
MANIFEST="dist/Xynigo_Sourcing_v${VERSION}${BUILD_SUFFIX}_update.json"
SHA_FILE="dist/Xynigo_Sourcing_v${VERSION}${BUILD_SUFFIX}_SHA256SUMS.txt"

mkdir -p "$STAGE" dist .cache

echo "[1/8] Download Windows embeddable Python ${PYVER} ..."
EMB=".cache/python-${PYVER}-embed-amd64.zip"
if [ ! -f "$EMB" ]; then
  curl -fL -o "$EMB" "https://www.python.org/ftp/python/${PYVER}/python-${PYVER}-embed-amd64.zip"
fi
mkdir -p "$STAGE/python-embed"
unzip -q "$EMB" -d "$STAGE/python-embed"

echo "[2/8] Install pure-Python dependencies ..."
python3 -m pip install --quiet --upgrade --target "$STAGE/deps" \
  "certifi>=2024.8.30,<2027" "websocket-client>=1.6,<2" "openpyxl>=3.1,<4"

echo "[3/8] Copy application and updater ..."
mkdir -p "$STAGE/app"
cp -R src/purchase_tool "$STAGE/app/purchase_tool"
HELPER_SOURCE="$ROOT/packaging/windows/update-helper.ps1" \
HELPER_TARGET="$STAGE/update-helper.ps1" python3 - <<'PY'
import os
from pathlib import Path

text = Path(os.environ['HELPER_SOURCE']).read_text(encoding='utf-8')
Path(os.environ['HELPER_TARGET']).write_text(text, encoding='utf-8-sig')
PY

echo "[4/8] Build portable Xynigo.exe status center ..."
bash packaging/windows/build-launcher.sh "$STAGE/Xynigo.exe"
cp src/purchase_tool/web/xynigo-logo.png "$STAGE/xynigo-logo.png"
cp src/purchase_tool/web/xynigo-x.ico "$STAGE/xynigo-x.ico"

echo "[5/8] Write launchers and package metadata ..."
PTH="$STAGE/python-embed/python311._pth"
cat > "$PTH" <<EOF
python311.zip
.

import site
EOF

STAGE="$STAGE" VERSION="$VERSION" CHANNEL="$CHANNEL" BUILD_LABEL="$BUILD_LABEL" python3 - <<'PY'
import json
import os

stage = os.environ['STAGE']
version = os.environ['VERSION']
channel = os.environ['CHANNEL']
build_label = os.environ.get('BUILD_LABEL', '')

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


bat = (
    '@echo off\r\n'
    'setlocal\r\n'
    'cd /d "%~dp0"\r\n'
    'start "" "Xynigo.exe" --show\r\n'
)
with open(os.path.join(stage, '启动.bat'), 'wb') as handle:
    handle.write(bat.encode('gbk'))
local_bat = launcher(
    'python-embed\\python.exe run.py --local-ui',
    '打开本机兼容界面，用于 HubStudio 与故障排查。',
)
with open(os.path.join(stage, '启动-本地执行器.bat'), 'wb') as handle:
    handle.write(local_bat.encode('gbk'))
pair_bat = (
    '@echo off\r\n'
    'setlocal\r\n'
    'cd /d "%~dp0"\r\n'
    'echo Xynigo 本地执行器设备配对\r\n'
    'echo 请从云端 Web 的“系统管理 - 本地执行器”复制 8 位配对码。\r\n'
    'set /p XYNIGO_PAIR_CODE=配对码：\r\n'
    'if "%XYNIGO_PAIR_CODE%"=="" (echo 未输入配对码。) else (python-embed\\python.exe run.py pair "%XYNIGO_PAIR_CODE%")\r\n'
    'pause\r\n'
)
with open(os.path.join(stage, '配对本地执行器.bat'), 'wb') as handle:
    handle.write(pair_bat.encode('gbk'))

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
    os.chdir(os.environ.get('XYNIGO_DATA_DIR') or ROOT)
    if len(sys.argv) > 1 and sys.argv[1] == 'pair':
        from purchase_tool.executor_channel import pair_cli
        sys.exit(pair_cli(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == 'protocol':
        from purchase_tool.executor_protocol import protocol_cli
        sys.exit(protocol_cli(sys.argv[2:]))
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
    'installMode': 'green_package',
    'launcherFile': 'Xynigo.exe',
    'statusCenter': True,
    'trayMenu': True,
    'buildLabel': build_label,
    'releaseEligible': not bool(build_label),
    'managedPaths': [
        'app', 'deps', 'python-embed', 'run.py', 'Xynigo.exe',
        'xynigo-logo.png', 'xynigo-x.ico', '启动.bat',
        '启动-本地执行器.bat', '配对本地执行器.bat',
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
2. 双击“Xynigo.exe”打开品牌状态中心；执行器会在后台启动并驻留系统托盘。
3. 首次连接云端时，在 Web 生成配对码，然后在状态中心输入配对码；也可使用“配对本地执行器.bat”。
4. “启动.bat”保留为兼容入口；需要旧本机界面或排查 HubStudio 时，双击“启动-本地执行器.bat”。
5. 绿色版不注册 xynigo:// 系统协议，网页无法静默启动本机程序；请由用户明确双击 Xynigo.exe。
6. 标准安装版后续统一在 Xynigo 状态中心检查更新；绿色版需重新下载并覆盖。
7. config.json、查询日志、运行数据和用户导入文件不会被更新覆盖或上传。
8. 更新失败时程序会从本机备份自动回滚并重新启动。
''' % version
with open(os.path.join(stage, '使用说明.txt'), 'w', encoding='utf-8') as handle:
    handle.write(guide)
PY

echo "[6/8] Validate package safety and structure ..."
STAGE="$STAGE" python3 - <<'PY'
import os
from pathlib import Path

root = Path(os.environ['STAGE'])
required = [
    'app/purchase_tool/main.py', 'app/purchase_tool/updater.py',
    'python-embed/python.exe', 'deps/openpyxl', 'run.py', '启动.bat',
    '启动-本地执行器.bat', '配对本地执行器.bat',
    'Xynigo.exe', 'xynigo-logo.png', 'xynigo-x.ico',
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

echo "[7/8] Create Windows ZIP ..."
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

echo "[8/8] Generate SHA-256 and update manifest ..."
python3 scripts/update_release_assets.py \
  --version "$VERSION" --channel "$CHANNEL" \
  --platform "windows-x86_64" --asset "$ZIP" \
  --notes "release/v${VERSION}.zh-CN.json" \
  --manifest "$MANIFEST" --sha-file "$SHA_FILE"

du -sh "$ZIP"

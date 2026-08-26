#!/bin/bash
# Build a self-contained macOS green package and update the shared manifest.
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$(pwd)"
VERSION="$(PYTHONPATH=src python3 -c 'from purchase_tool import __version__; print(__version__)')"
CHANNEL="${XYNIGO_RELEASE_CHANNEL:-stable}"
case "$CHANNEL" in
  stable|test) ;;
  *) echo "XYNIGO_RELEASE_CHANNEL must be stable or test: $CHANNEL" >&2; exit 2 ;;
esac
RAW_ARCH="$(uname -m)"
case "$RAW_ARCH" in
  arm64|aarch64) PLATFORM="macos-arm64"; ARCH_LABEL="arm64" ;;
  *) echo "Only Apple Silicon arm64 is maintained: $RAW_ARCH" >&2; exit 1 ;;
esac
STAGE="$(mktemp -d)/Xynigo-Sourcing"
BUILD_ROOT="$(mktemp -d)"
STAMP="$(date +%Y%m%d)"
ZIP_NAME="Xynigo_Sourcing_macOS_${ARCH_LABEL}_${STAMP}_v${VERSION}.zip"
ZIP="dist/${ZIP_NAME}"
MANIFEST="dist/Xynigo_Sourcing_v${VERSION}_update.json"
SHA_FILE="dist/Xynigo_Sourcing_v${VERSION}_SHA256SUMS.txt"
PACKAGER_VENV=".cache/macos-packager-${RAW_ARCH}"

mkdir -p "$STAGE" dist .cache

echo "[1/7] Prepare macOS ${ARCH_LABEL} packager ..."
if [ ! -x "$PACKAGER_VENV/bin/python" ]; then
  python3 -m venv "$PACKAGER_VENV"
fi
"$PACKAGER_VENV/bin/python" -m pip install --quiet --upgrade pip
"$PACKAGER_VENV/bin/python" -m pip install --quiet --upgrade \
  "pyinstaller>=6.10,<7" .

echo "[2/7] Build self-contained runtime ..."
"$PACKAGER_VENV/bin/python" -m PyInstaller \
  --noconfirm --clean --onedir --console \
  --name xynigo-sourcing \
  --paths src --collect-data purchase_tool \
  --distpath "$BUILD_ROOT/dist" \
  --workpath "$BUILD_ROOT/work" \
  --specpath "$BUILD_ROOT/spec" \
  packaging/macos/entry.py
mkdir -p "$STAGE/runtime"
cp -RL "$BUILD_ROOT/dist/xynigo-sourcing/"* "$STAGE/runtime/"
cp packaging/macos/update-helper.sh "$STAGE/update-helper.sh"

echo "[3/7] Write launcher and package metadata ..."
STAGE="$STAGE" VERSION="$VERSION" CHANNEL="$CHANNEL" PLATFORM="$PLATFORM" python3 - <<'PY'
import json
import os
from pathlib import Path

stage = Path(os.environ['STAGE'])
version = os.environ['VERSION']
channel = os.environ['CHANNEL']
platform_key = os.environ['PLATFORM']
launcher = '''#!/bin/bash
cd "$(dirname "$0")"
echo "Xynigo Sourcing v%s 启动中..."
echo "默认打开云端工作台；本地执行器同时运行。"
echo "保持此窗口开启；关闭窗口即退出工具。"
./runtime/xynigo-sourcing
XYNIGO_EXIT_CODE=$?
if [ "$XYNIGO_EXIT_CODE" -eq 42 ]; then exit 0; fi
if [ "$XYNIGO_EXIT_CODE" -ne 0 ]; then
  echo "启动失败，退出码：$XYNIGO_EXIT_CODE"
fi
echo "按回车键关闭窗口。"
read -r
''' % version
(stage / '启动-Mac.command').write_text(launcher, encoding='utf-8')
local_launcher = '''#!/bin/bash
cd "$(dirname "$0")"
echo "Xynigo Sourcing v%s 本地执行器启动中..."
echo "打开本机兼容界面，用于 HubStudio 与故障排查。"
echo "保持此窗口开启；关闭窗口即退出工具。"
./runtime/xynigo-sourcing --local-ui
XYNIGO_EXIT_CODE=$?
if [ "$XYNIGO_EXIT_CODE" -eq 42 ]; then exit 0; fi
if [ "$XYNIGO_EXIT_CODE" -ne 0 ]; then
  echo "启动失败，退出码：$XYNIGO_EXIT_CODE"
fi
echo "按回车键关闭窗口。"
read -r
''' % version
(stage / '启动-本地执行器-Mac.command').write_text(
    local_launcher, encoding='utf-8')
version_info = {
    'schemaVersion': 2,
    'product': 'Xynigo Sourcing',
    'version': version,
    'channel': channel,
    'platform': platform_key,
    'repository': 'wrangler1024/xynigo-sourcing',
    'managedPaths': [
        'runtime', '启动-Mac.command', '启动-本地执行器-Mac.command',
        'update-helper.sh',
        'VERSION.json', '使用说明.txt',
    ],
    'preservedPaths': [
        'config.json', '查询日志', '日志', 'logs', '运行数据',
        'data', '数据', 'imports', '导入文件',
    ],
}
(stage / 'VERSION.json').write_text(
    json.dumps(version_info, ensure_ascii=False, indent=2), encoding='utf-8')
guide = '''Xynigo Sourcing v%s macOS %s 绿色包

1. 必须先完整解压 ZIP，不能直接在压缩包中双击。
2. 首次运行右键“启动-Mac.command”选择“打开”。
3. 双击“启动-Mac.command”默认打开云端工作台；本地执行器仍在此窗口运行。
4. 需要使用旧本机界面或排查 HubStudio 时，打开“启动-本地执行器-Mac.command”。
5. 页面右上角会检查 GitHub 最新稳定版；发现新版本时点击提醒，回到运行窗口输入 Y 更新或 N 暂不更新。
6. 更新不需要 Git 或 GitHub 账号；校验失败或网络不可用时当前版本会继续运行。
7. config.json、查询日志、运行数据和用户导入文件不会被更新覆盖或上传。
8. 更新失败时会从本机备份自动回滚并重新启动。
''' % (version, platform_key)
(stage / '使用说明.txt').write_text(guide, encoding='utf-8')
PY
chmod +x "$STAGE/启动-Mac.command" "$STAGE/启动-本地执行器-Mac.command" \
  "$STAGE/update-helper.sh" \
  "$STAGE/runtime/xynigo-sourcing"

echo "[4/7] Ad-hoc sign and verify runtime ..."
/usr/bin/codesign --force --deep --sign - "$STAGE/runtime/xynigo-sourcing"
/usr/bin/codesign --verify --deep --strict "$STAGE/runtime/xynigo-sourcing"

echo "[5/7] Validate package safety and structure ..."
STAGE="$STAGE" PLATFORM="$PLATFORM" python3 - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ['STAGE'])
required = [
    'runtime/xynigo-sourcing', '启动-Mac.command',
    '启动-本地执行器-Mac.command', 'update-helper.sh',
    'VERSION.json', '使用说明.txt',
]
missing = [name for name in required if not (root / name).exists()]
if missing:
    raise SystemExit('missing package files: ' + ', '.join(missing))
info = json.loads((root / 'VERSION.json').read_text(encoding='utf-8'))
if info.get('platform') != os.environ['PLATFORM']:
    raise SystemExit('package platform mismatch')
if any(path.is_symlink() for path in root.rglob('*')):
    raise SystemExit('package contains symbolic links')
forbidden_names = {
    'config.json', '启动日志.txt', '.env', '.git', '查询日志',
    '运行数据', 'logs', '日志', 'imports', '导入文件',
}
found = [str(path.relative_to(root)) for path in root.rglob('*')
         if path.name in forbidden_names]
if found:
    raise SystemExit('forbidden runtime files in package: ' + ', '.join(found))
PY

echo "[6/7] Create macOS ZIP ..."
ZIP_PATH="$ROOT/$ZIP" STAGE="$STAGE" python3 - <<'PY'
import os
from pathlib import Path
import zipfile

stage = Path(os.environ['STAGE'])
target = Path(os.environ['ZIP_PATH'])
if target.exists():
    target.unlink()
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(stage.rglob('*')):
        if path.is_dir():
            continue
        arcname = str(Path(stage.name) / path.relative_to(stage))
        archive.write(path, arcname)
PY

echo "[7/7] Update SHA-256 and shared manifest ..."
python3 scripts/update_release_assets.py \
  --version "$VERSION" --channel "$CHANNEL" \
  --platform "$PLATFORM" --asset "$ZIP" \
  --notes "release/v${VERSION}.zh-CN.json" \
  --manifest "$MANIFEST" --sha-file "$SHA_FILE"
du -sh "$ZIP"

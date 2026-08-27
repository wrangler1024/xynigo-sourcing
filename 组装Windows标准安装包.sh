#!/bin/bash
# Build the per-user Windows standard installer from the reviewed green payload.
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$(pwd)"
VERSION="$(PYTHONPATH=src python3 -c 'from purchase_tool import __version__; print(__version__)')"
CHANNEL="${XYNIGO_RELEASE_CHANNEL:-test}"
case "$CHANNEL" in
  stable|test) ;;
  *) echo "XYNIGO_RELEASE_CHANNEL must be stable or test: $CHANNEL" >&2; exit 2 ;;
esac

WORK_ROOT="$(mktemp -d)"
trap 'rm -rf "$WORK_ROOT"' EXIT
LAUNCHER_BIN="$WORK_ROOT/Xynigo Launcher.exe"
if [ -n "${XYNIGO_WINDOWS_LAUNCHER_BIN:-}" ]; then
  cp "$XYNIGO_WINDOWS_LAUNCHER_BIN" "$LAUNCHER_BIN"
else
  bash packaging/windows/build-launcher.sh "$LAUNCHER_BIN"
fi
if [ ! -f "$LAUNCHER_BIN" ]; then
  echo "Windows status-center launcher was not built." >&2
  exit 2
fi

MAKENSIS_BIN="${XYNIGO_MAKENSIS_BIN:-}"
if [ -z "$MAKENSIS_BIN" ]; then
  MAKENSIS_BIN="$(command -v makensis || true)"
fi
if [ -z "$MAKENSIS_BIN" ] || [ ! -x "$MAKENSIS_BIN" ]; then
  echo "makensis is required. Install NSIS 3.12+ or set XYNIGO_MAKENSIS_BIN." >&2
  exit 2
fi

echo "[1/6] Build branded tray/status-center launcher ..."
file "$LAUNCHER_BIN"

echo "[2/6] Build reviewed Windows payload ..."
XYNIGO_RELEASE_CHANNEL="$CHANNEL" bash 组装Windows绿色包.sh

PAYLOAD_ROOT="$WORK_ROOT/payload"
mkdir -p "$PAYLOAD_ROOT"

echo "[3/6] Extract immutable payload selected by the update manifest ..."
ROOT="$ROOT" VERSION="$VERSION" PAYLOAD_ROOT="$PAYLOAD_ROOT" python3 - <<'PY'
import json
import os
from pathlib import Path
import zipfile

root = Path(os.environ['ROOT'])
version = os.environ['VERSION']
manifest = json.loads(
    (root / 'dist' / ('Xynigo_Sourcing_v%s_update.json' % version))
    .read_text(encoding='utf-8'))
platform = manifest.get('platforms', {}).get('windows-x86_64') or manifest
archive = root / 'dist' / str(platform.get('assetName') or '')
if not archive.is_file():
    raise SystemExit('Windows payload archive is missing: %s' % archive)
with zipfile.ZipFile(archive) as bundle:
    names = set(bundle.namelist())
    required = {
        'Xynigo-Sourcing/VERSION.json',
        'Xynigo-Sourcing/run.py',
        'Xynigo-Sourcing/python-embed/python.exe',
        'Xynigo-Sourcing/app/purchase_tool/main.py',
        'Xynigo-Sourcing/app/purchase_tool/web/xynigo-x.ico',
    }
    missing = sorted(required - names)
    if missing:
        raise SystemExit('payload is incomplete: ' + ', '.join(missing))
    forbidden = {
        'config.json', '查询日志', '日志', 'logs', '运行数据',
        'data', '数据', 'imports', '导入文件', '.env', '.git',
    }
    leaked = [name for name in names if (
        len(Path(name).parts) > 1 and Path(name).parts[1] in forbidden)]
    if leaked:
        raise SystemExit('payload contains user/runtime data: ' + leaked[0])
    bundle.extractall(os.environ['PAYLOAD_ROOT'])
PY

PAYLOAD_DIR="$PAYLOAD_ROOT/Xynigo-Sourcing"
OUTPUT_FILE="$ROOT/dist/Xynigo_Sourcing_Windows_Setup_v${VERSION}.exe"
METADATA_FILE="$ROOT/dist/Xynigo_Sourcing_Windows_Setup_v${VERSION}.json"
SHA_FILE="$ROOT/dist/Xynigo_Sourcing_Windows_Setup_v${VERSION}.sha256"
WRAPPER="$WORK_ROOT/installer-wrapper.nsi"
VERSION_QUAD="${VERSION}.0"

echo "[4/6] Compile branded Unicode per-user NSIS installer ..."
ROOT="$ROOT" PAYLOAD_DIR="$PAYLOAD_DIR" OUTPUT_FILE="$OUTPUT_FILE" \
VERSION="$VERSION" VERSION_QUAD="$VERSION_QUAD" WRAPPER="$WRAPPER" \
LAUNCHER_BIN="$LAUNCHER_BIN" \
python3 - <<'PY'
import os
from pathlib import Path

def quote(value):
    value = str(value)
    if os.name == 'nt':
        value = value.replace('/', '\\')
    else:
        value = value.replace('\\', '/')
    return value.replace('"', '$\\"')

root = Path(os.environ['ROOT'])
defines = {
    'APP_VERSION': os.environ['VERSION'],
    'APP_VERSION_QUAD': os.environ['VERSION_QUAD'],
    'PAYLOAD_DIR': os.environ['PAYLOAD_DIR'],
    'OUTPUT_FILE': os.environ['OUTPUT_FILE'],
    'LICENSE_FILE': root / 'LICENSE',
    'INSTALLER_ICON': (
        Path(os.environ['PAYLOAD_DIR']) /
        'app/purchase_tool/web/xynigo-x.ico'),
    'INSTALLER_WELCOME_BITMAP': (
        root / 'packaging/windows/branding/installer-welcome.bmp'),
    'INSTALLER_HEADER_BITMAP': (
        root / 'packaging/windows/branding/installer-header.bmp'),
    'STANDARD_GUI_LAUNCHER': os.environ['LAUNCHER_BIN'],
    'STANDARD_LOGO_PNG': root / 'src/purchase_tool/web/xynigo-logo.png',
    'STANDARD_ICON_ICO': root / 'src/purchase_tool/web/xynigo-x.ico',
    'STANDARD_LAUNCHER': root / 'packaging/windows/Xynigo.cmd',
    'STANDARD_PAIR_LAUNCHER': root / 'packaging/windows/配对本地执行器.cmd',
}
lines = [
    '!define %s "%s"' % (name, quote(value))
    for name, value in defines.items()
]
template = (root / 'packaging/windows/xynigo-standard-installer.nsi').read_text(
    encoding='utf-8')
Path(os.environ['WRAPPER']).write_text(
    '\n'.join(lines) + '\n' + template, encoding='utf-8-sig')
PY
rm -f "$OUTPUT_FILE" "$METADATA_FILE" "$SHA_FILE"
"$MAKENSIS_BIN" -V3 "$WRAPPER"

echo "[5/6] Verify installer metadata and safety contract ..."
OUTPUT_FILE="$OUTPUT_FILE" METADATA_FILE="$METADATA_FILE" \
SHA_FILE="$SHA_FILE" VERSION="$VERSION" CHANNEL="$CHANNEL" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

installer = Path(os.environ['OUTPUT_FILE'])
if not installer.is_file() or installer.stat().st_size < 1_000_000:
    raise SystemExit('compiled installer is missing or unexpectedly small')
digest = hashlib.sha256(installer.read_bytes()).hexdigest()
metadata = {
    'schemaVersion': 1,
    'product': 'Xynigo Sourcing',
    'version': os.environ['VERSION'],
    'channel': os.environ['CHANNEL'],
    'platform': 'windows-x86_64',
    'installMode': 'standard_per_user',
    'requiresElevation': False,
    'autoStart': False,
    'protocol': 'xynigo',
    'statusCenter': True,
    'trayMenu': True,
    'launcherFile': 'Xynigo.exe',
    'assetName': installer.name,
    'size': installer.stat().st_size,
    'sha256': digest,
    'authenticodeSigned': False,
    'releaseEligible': False,
}
Path(os.environ['METADATA_FILE']).write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2) + '\n',
    encoding='utf-8')
Path(os.environ['SHA_FILE']).write_text(
    '%s  %s\n' % (digest, installer.name), encoding='utf-8')
PY

echo "[6/6] Windows standard installer ready (unsigned until signing gate) ..."
du -sh "$OUTPUT_FILE"
echo "$METADATA_FILE"

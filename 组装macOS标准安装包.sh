#!/bin/bash
# Build the macOS .app + .pkg standard installer from the reviewed green payload.
set -euo pipefail
cd "$(dirname "$0")"
export COPYFILE_DISABLE=1

ROOT="$(pwd)"
VERSION="$(PYTHONPATH=src python3 -c 'from purchase_tool import __version__; print(__version__)')"
CHANNEL="${XYNIGO_RELEASE_CHANNEL:-test}"
case "$CHANNEL" in
  stable|test) ;;
  *) echo "XYNIGO_RELEASE_CHANNEL must be stable or test: $CHANNEL" >&2; exit 2 ;;
esac
for tool in pkgbuild productbuild swiftc iconutil codesign sips; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "$tool is required to build the macOS standard installer." >&2
    exit 2
  fi
done
case "$(uname -m)" in
  arm64|aarch64) ;;
  *) echo "Only Apple Silicon arm64 is maintained for the macOS standard installer." >&2; exit 2 ;;
esac

echo "[1/7] Build reviewed macOS green payload ..."
XYNIGO_RELEASE_CHANNEL="$CHANNEL" bash 组装macOS绿色包.sh

WORK_ROOT="$(mktemp -d)"
trap 'rm -rf "$WORK_ROOT"' EXIT
EXTRACT_ROOT="$WORK_ROOT/extracted"
PACKAGE_ROOT="$WORK_ROOT/package-root"
APP="$PACKAGE_ROOT/Applications/Xynigo Sourcing.app"
CONTENTS="$APP/Contents"
RESOURCES="$CONTENTS/Resources"
mkdir -p "$EXTRACT_ROOT" "$CONTENTS/MacOS" "$RESOURCES/runtime"

echo "[2/7] Extract immutable green runtime selected by the manifest ..."
ROOT="$ROOT" VERSION="$VERSION" EXTRACT_ROOT="$EXTRACT_ROOT" python3 - <<'PY'
import json
import os
from pathlib import Path
import zipfile

root = Path(os.environ['ROOT'])
manifest = json.loads((root / 'dist' / (
    'Xynigo_Sourcing_v%s_update.json' % os.environ['VERSION']
)).read_text(encoding='utf-8'))
package = manifest.get('platforms', {}).get('macos-arm64') or manifest
archive = root / 'dist' / str(package.get('assetName') or '')
if not archive.is_file():
    raise SystemExit('macOS green payload archive is missing: %s' % archive)
with zipfile.ZipFile(archive) as bundle:
    names = set(bundle.namelist())
    required = {
        'Xynigo-Sourcing/VERSION.json',
        'Xynigo-Sourcing/runtime/xynigo-sourcing',
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
    bundle.extractall(os.environ['EXTRACT_ROOT'])
PY
cp -R "$EXTRACT_ROOT/Xynigo-Sourcing/runtime/." "$RESOURCES/runtime/"

echo "[3/7] Build native launcher, app metadata and icon ..."
/usr/bin/swiftc -O -target arm64-apple-macos13.0 \
  -framework AppKit packaging/macos/launcher.swift \
  -o "$CONTENTS/MacOS/xynigo-launcher"
cp packaging/macos/启动本地执行器.command "$RESOURCES/启动本地执行器.command"
cp packaging/macos/协议启动.command "$RESOURCES/协议启动.command"
cp packaging/macos/配对本地执行器.command "$RESOURCES/配对本地执行器.command"
cp packaging/macos/迁移绿色包数据.command "$RESOURCES/迁移绿色包数据.command"
chmod 755 "$CONTENTS/MacOS/xynigo-launcher" \
  "$RESOURCES/启动本地执行器.command" "$RESOURCES/协议启动.command" \
  "$RESOURCES/配对本地执行器.command" "$RESOURCES/迁移绿色包数据.command" \
  "$RESOURCES/runtime/xynigo-sourcing"

INFO_PLIST="$CONTENTS/Info.plist"
INFO_PLIST="$INFO_PLIST" VERSION="$VERSION" python3 - <<'PY'
import os
from pathlib import Path
import plistlib

payload = {
    'CFBundleDevelopmentRegion': 'zh_CN',
    'CFBundleDisplayName': 'Xynigo Sourcing',
    'CFBundleExecutable': 'xynigo-launcher',
    'CFBundleIconFile': 'xynigo.icns',
    'CFBundleIdentifier': 'icu.samforo.xynigo.sourcing',
    'CFBundleInfoDictionaryVersion': '6.0',
    'CFBundleName': 'Xynigo Sourcing',
    'CFBundlePackageType': 'APPL',
    'CFBundleShortVersionString': os.environ['VERSION'],
    'CFBundleVersion': os.environ['VERSION'],
    'CFBundleURLTypes': [{
        'CFBundleTypeRole': 'Viewer',
        'CFBundleURLName': 'Xynigo local executor launcher',
        'CFBundleURLSchemes': ['xynigo'],
    }],
    'LSMinimumSystemVersion': '13.0',
    'LSMultipleInstancesProhibited': True,
}
with Path(os.environ['INFO_PLIST']).open('wb') as handle:
    plistlib.dump(payload, handle, sort_keys=True)
PY

ICONSET="$WORK_ROOT/xynigo.iconset"
mkdir -p "$ICONSET"
SOURCE_ICON="src/purchase_tool/web/xynigo-favicon.png"
for spec in \
  "16 icon_16x16.png" "32 icon_16x16@2x.png" \
  "32 icon_32x32.png" "64 icon_32x32@2x.png" \
  "128 icon_128x128.png" "256 icon_128x128@2x.png" \
  "256 icon_256x256.png" "512 icon_256x256@2x.png" \
  "512 icon_512x512.png" "1024 icon_512x512@2x.png"; do
  set -- $spec
  /usr/bin/sips -s format png -z "$1" "$1" "$SOURCE_ICON" \
    --out "$ICONSET/$2" >/dev/null
done
/usr/bin/iconutil -c icns "$ICONSET" -o "$RESOURCES/xynigo.icns"

RESOURCES="$RESOURCES" VERSION="$VERSION" CHANNEL="$CHANNEL" python3 - <<'PY'
import json
import os
from pathlib import Path

resources = Path(os.environ['RESOURCES'])
metadata = {
    'schemaVersion': 1,
    'product': 'Xynigo Sourcing',
    'version': os.environ['VERSION'],
    'channel': os.environ['CHANNEL'],
    'platform': 'macos-arm64',
    'installMode': 'standard_system_application',
    'dataDirectory': '~/Library/Application Support/XynigoSourcing',
    'autoStart': False,
    'protocol': 'xynigo',
    'managedPaths': [
        'runtime', '启动本地执行器.command', '协议启动.command',
        '配对本地执行器.command', '迁移绿色包数据.command', 'xynigo.icns',
    ],
    'preservedPaths': [
        'config.json', '查询日志', '日志', 'logs', '运行数据',
        'data', '数据', 'imports', '导入文件',
    ],
}
(resources / 'VERSION.json').write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2) + '\n',
    encoding='utf-8')
PY

# Keep Finder metadata and AppleDouble files out of the installer payload.
/usr/bin/xattr -cr "$APP"
find "$APP" -name '._*' -type f -delete
if find "$APP" -name '._*' -type f -print -quit | grep -q .; then
  echo "app source unexpectedly contains AppleDouble files" >&2
  exit 1
fi

echo "[4/7] Ad-hoc sign and verify local test app ..."
/usr/bin/codesign --force --deep --sign - "$APP"
/usr/bin/codesign --verify --deep --strict "$APP"
/usr/bin/plutil -lint "$INFO_PLIST"
# codesign may preserve source extended attributes; remove them before pkgbuild.
/usr/bin/xattr -cr "$APP"
/usr/bin/codesign --verify --deep --strict "$APP"

echo "[5/7] Build unsigned macOS installer package ..."
COMPONENT_PKG="$WORK_ROOT/XynigoSourcing-component.pkg"
COMPONENT_PLIST="$WORK_ROOT/components.plist"
OUTPUT_FILE="$ROOT/dist/Xynigo_Sourcing_macOS_Standard_v${VERSION}.pkg"
METADATA_FILE="$ROOT/dist/Xynigo_Sourcing_macOS_Standard_v${VERSION}.json"
SHA_FILE="$ROOT/dist/Xynigo_Sourcing_macOS_Standard_v${VERSION}.sha256"
rm -f "$OUTPUT_FILE" "$METADATA_FILE" "$SHA_FILE"
/usr/bin/pkgbuild --analyze --root "$PACKAGE_ROOT" "$COMPONENT_PLIST"
COMPONENT_PLIST="$COMPONENT_PLIST" python3 - <<'PY'
import os
from pathlib import Path
import plistlib

path = Path(os.environ['COMPONENT_PLIST'])
with path.open('rb') as handle:
    components = plistlib.load(handle)
for component in components:
    component['BundleIsRelocatable'] = False
    component['BundleIsVersionChecked'] = True
    component['BundleHasStrictIdentifier'] = True
    component['BundleOverwriteAction'] = 'upgrade'
with path.open('wb') as handle:
    plistlib.dump(components, handle, sort_keys=True)
PY
/usr/bin/pkgbuild --root "$PACKAGE_ROOT" \
  --component-plist "$COMPONENT_PLIST" \
  --identifier icu.samforo.xynigo.sourcing \
  --version "$VERSION" --install-location / "$COMPONENT_PKG"
/usr/bin/productbuild --package "$COMPONENT_PKG" "$OUTPUT_FILE"

echo "[6/7] Verify payload, protocol and no-autostart contract ..."
PAYLOAD_LIST="$WORK_ROOT/payload-files.txt"
/usr/sbin/pkgutil --payload-files "$OUTPUT_FILE" > "$PAYLOAD_LIST"
grep -Fq 'Applications/Xynigo Sourcing.app/Contents/MacOS/xynigo-launcher' "$PAYLOAD_LIST"
grep -Fq 'Applications/Xynigo Sourcing.app/Contents/Resources/runtime/xynigo-sourcing' "$PAYLOAD_LIST"
if grep -Eiq 'LaunchAgents|LaunchDaemons|LoginItems' "$PAYLOAD_LIST"; then
  echo "installer unexpectedly contains an autostart component" >&2
  exit 1
fi

echo "[7/7] Write non-release artifact metadata and SHA-256 ..."
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
    'platform': 'macos-arm64',
    'installMode': 'standard_system_application',
    'installLocation': '/Applications/Xynigo Sourcing.app',
    'dataDirectory': '~/Library/Application Support/XynigoSourcing',
    'requiresElevation': True,
    'autoStart': False,
    'protocol': 'xynigo',
    'assetName': installer.name,
    'size': installer.stat().st_size,
    'sha256': digest,
    'appSignature': 'adhoc',
    'developerIdApplicationSigned': False,
    'developerIdInstallerSigned': False,
    'notarized': False,
    'stapled': False,
    'releaseEligible': False,
}
Path(os.environ['METADATA_FILE']).write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2) + '\n',
    encoding='utf-8')
Path(os.environ['SHA_FILE']).write_text(
    '%s  %s\n' % (digest, installer.name), encoding='utf-8')
PY
du -sh "$OUTPUT_FILE"
echo "$METADATA_FILE"

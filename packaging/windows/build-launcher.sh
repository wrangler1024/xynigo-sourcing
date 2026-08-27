#!/bin/bash
# Cross-compile the branded Windows tray/status-center launcher.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SOURCE_DIR="$SCRIPT_DIR/launcher"
OUTPUT="${1:-$ROOT/dist/Xynigo Launcher.exe}"

command -v go >/dev/null 2>&1 || {
  echo "Go is required to build the Windows status-center launcher." >&2
  exit 2
}

BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
cp "$SOURCE_DIR/go.mod" "$SOURCE_DIR/go.sum" \
  "$SOURCE_DIR/main.go" "$SOURCE_DIR/XynigoLauncher.manifest" "$BUILD_DIR/"
cp "$ROOT/src/purchase_tool/web/xynigo-x.ico" "$BUILD_DIR/xynigo-x.ico"

mkdir -p "$(dirname "$OUTPUT")"
(
  cd "$BUILD_DIR"
  go mod download
  go run github.com/akavel/rsrc@v0.10.2 \
    -arch amd64 \
    -manifest XynigoLauncher.manifest \
    -ico xynigo-x.ico \
    -o rsrc_windows_amd64.syso
  GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build \
    -trimpath -ldflags='-s -w -H=windowsgui' -o "$OUTPUT" .
)

file "$OUTPUT"

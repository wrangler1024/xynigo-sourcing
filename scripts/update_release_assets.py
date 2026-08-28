#!/usr/bin/env python3
"""Add one built platform asset to the shared release update manifest."""
import argparse
import hashlib
import json
from pathlib import Path


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True)
    parser.add_argument(
        '--channel', choices=('stable', 'test'), default='stable')
    parser.add_argument('--platform', required=True)
    parser.add_argument('--asset', type=Path, required=True)
    parser.add_argument('--notes', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--sha-file', type=Path, required=True)
    args = parser.parse_args()

    asset = args.asset.resolve()
    if not asset.is_file():
        raise SystemExit('asset does not exist: %s' % asset)
    notes = json.loads(args.notes.read_text(encoding='utf-8'))['notesZh']
    digest = sha256_file(asset)
    package = {
        'assetName': asset.name,
        'sha256': digest,
        'size': asset.stat().st_size,
    }

    manifest = {}
    if args.manifest.is_file():
        manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
        if str(manifest.get('version')) != args.version:
            manifest = {}
    manifest.update({
        'schemaVersion': 2,
        'product': 'Xynigo Sourcing',
        'channel': args.channel,
        'version': args.version,
        'notesZh': notes,
    })
    platforms = manifest.setdefault('platforms', {})
    platforms[args.platform] = package

    # v0.5.0 Windows clients only understand the top-level package fields.
    # Keep those fields pointed at Windows whenever it is available.
    # Rebuilding the same sole platform must refresh the legacy top-level
    # fields as well.  The macOS standard-package job intentionally rebuilds
    # its reviewed green payload, so leaving these fields on the first archive
    # would advertise a stale hash to legacy clients.  In a combined manifest
    # Windows remains the legacy default.
    if args.platform.startswith('windows-') or len(platforms) == 1:
        manifest.update(package)

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    lines = []
    for platform_key in sorted(platforms):
        item = platforms[platform_key]
        lines.append('%s  %s' % (item['sha256'], item['assetName']))
    args.sha_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('ASSET:', asset)
    print('PLATFORM:', args.platform)
    print('SHA-256:', digest)


if __name__ == '__main__':
    main()

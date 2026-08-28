# -*- coding: utf-8 -*-
"""Entry point embedded in the self-contained macOS green package."""
from pathlib import Path
import os
import sys
import traceback


INSTALL_ROOT = Path(sys.executable).resolve().parent.parent

if '--package-self-test' in sys.argv:
    from purchase_tool import __version__
    from purchase_tool.updater import current_platform_key

    print('Xynigo Sourcing v%s %s package OK' % (
        __version__, current_platform_key()))
    sys.exit(0)

if '--cloud-tls-self-test' in sys.argv:
    from purchase_tool.cloud_auth import CloudAuthClient

    result = CloudAuthClient(timeout=15.0)._request('/healthz')
    if result.get('status') != 'ok':
        raise SystemExit('cloud TLS self-test returned an invalid response')
    print('Xynigo cloud TLS self-test OK')
    sys.exit(0)

if len(sys.argv) > 1 and sys.argv[1] == 'pair':
    from purchase_tool.executor_channel import pair_cli
    sys.exit(pair_cli(sys.argv[2:]))

if len(sys.argv) > 1 and sys.argv[1] == 'protocol':
    from purchase_tool.executor_protocol import protocol_cli
    sys.exit(protocol_cli(sys.argv[2:]))

if len(sys.argv) > 1 and sys.argv[1] == 'migrate':
    from purchase_tool.data_migration import migration_cli
    sys.exit(migration_cli(sys.argv[2:]))

try:
    os.environ.setdefault('XYNIGO_INSTALL_DIR', str(INSTALL_ROOT))
    from purchase_tool import bootstrap  # noqa: F401
except SystemExit:
    raise
except Exception:
    print('启动目录：%s' % INSTALL_ROOT)
    print('--- 详细报错 ---')
    traceback.print_exc()
    sys.exit(1)

# -*- coding: utf-8 -*-
"""Entry point embedded in the self-contained macOS green package."""
from pathlib import Path
import sys
import traceback


INSTALL_ROOT = Path(sys.executable).resolve().parent.parent

if '--package-self-test' in sys.argv:
    from purchase_tool import __version__
    from purchase_tool.updater import current_platform_key

    print('Xynigo Sourcing v%s %s package OK' % (
        __version__, current_platform_key()))
    sys.exit(0)

try:
    from purchase_tool import __version__
    from purchase_tool.updater import check_for_updates_at_startup

    if check_for_updates_at_startup(INSTALL_ROOT, __version__):
        sys.exit(42)
    from purchase_tool import bootstrap  # noqa: F401
except SystemExit:
    raise
except Exception:
    print('启动目录：%s' % INSTALL_ROOT)
    print('--- 详细报错 ---')
    traceback.print_exc()
    sys.exit(1)

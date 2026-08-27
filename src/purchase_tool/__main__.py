# -*- coding: utf-8 -*-
import sys


if len(sys.argv) > 1 and sys.argv[1] == 'register':
    from .register_cli import main
    sys.exit(main(sys.argv[2:]))
elif len(sys.argv) > 1 and sys.argv[1] == 'env-batch':
    from .env_batch import main
    sys.exit(main(sys.argv[2:]))
elif len(sys.argv) > 1 and sys.argv[1] == 'backfill':
    from .ledger_backfill import main
    sys.exit(main(sys.argv[2:]))
elif len(sys.argv) > 1 and sys.argv[1] == 'pair':
    from .executor_channel import pair_cli
    sys.exit(pair_cli(sys.argv[2:]))
elif len(sys.argv) > 1 and sys.argv[1] == 'protocol':
    from .executor_protocol import protocol_cli
    sys.exit(protocol_cli(sys.argv[2:]))
elif len(sys.argv) > 1 and sys.argv[1] == 'migrate':
    from .data_migration import migration_cli
    sys.exit(migration_cli(sys.argv[2:]))
else:
    from . import bootstrap   # noqa: F401  统一走 bootstrap（含异常落盘）

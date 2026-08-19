# -*- coding: utf-8 -*-
"""统一启动入口：异常打印并写入 启动日志.txt（bat 的 -c 与 python -m 共用）。"""
import os
import sys
import traceback


def _write_log(text):
    try:
        with open(os.path.join(os.getcwd(), '启动日志.txt'), 'w',
                  encoding='utf-8') as f:
            f.write(text)
    except Exception:
        pass


try:
    from .main import main
    main()
except SystemExit:
    raise
except Exception:
    err = traceback.format_exc()
    print('\n启动失败，详细信息如下（已写入 启动日志.txt）：')
    print(err)
    _write_log(err)
    sys.exit(1)

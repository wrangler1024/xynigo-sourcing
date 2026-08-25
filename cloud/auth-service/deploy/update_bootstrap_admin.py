#!/usr/bin/env python3
"""Replace the bootstrap super-admin Open ID from standard input without logging it."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
import tempfile


BOOTSTRAP_KEY = "XYNIGO_AUTH_BOOTSTRAP_SUPER_ADMIN_OPEN_IDS"
OPEN_ID_PATTERN = re.compile(r"^ou_[A-Za-z0-9_-]+$")


def update_env(path: Path, open_id: str) -> None:
    if not OPEN_ID_PATTERN.fullmatch(open_id):
        raise ValueError("invalid Feishu Open ID")

    lines = path.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    matches = 0
    for line in lines:
        key, separator, _current_value = line.partition("=")
        if separator and key == BOOTSTRAP_KEY:
            matches += 1
            updated.append(f"{BOOTSTRAP_KEY}={open_id}")
        else:
            updated.append(line)
    if matches != 1:
        raise ValueError("runtime env must contain the bootstrap key exactly once")

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".env.xynigo-bootstrap.", dir=path.parent
    )
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(updated) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    arguments = parser.parse_args()
    open_id = sys.stdin.read().strip()
    update_env(arguments.env_file, open_id)
    print("updated_bootstrap_super_admin=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

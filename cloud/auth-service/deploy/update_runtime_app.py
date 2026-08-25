#!/usr/bin/env python3
"""Replace the Feishu app credential in a runtime .env without logging it."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


APP_ID_KEY = "XYNIGO_AUTH_FEISHU_APP_ID"
APP_SECRET_KEY = "XYNIGO_AUTH_FEISHU_APP_SECRET"


def update_env(path: Path, app_id: str, app_secret: str) -> None:
    if not app_id.startswith("cli_"):
        raise ValueError("invalid Feishu App ID")
    if len(app_secret) < 8 or "\n" in app_secret or "\r" in app_secret:
        raise ValueError("invalid Feishu App Secret")

    lines = path.read_text(encoding="utf-8").splitlines()
    replacements = {APP_ID_KEY: app_id, APP_SECRET_KEY: app_secret}
    counts = {key: 0 for key in replacements}
    updated: list[str] = []
    for line in lines:
        key, separator, _value = line.partition("=")
        if separator and key in replacements:
            counts[key] += 1
            updated.append(f"{key}={replacements[key]}")
        else:
            updated.append(line)

    if any(count != 1 for count in counts.values()):
        raise ValueError("runtime env must contain each Feishu credential key exactly once")

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".env.xynigo-app.", dir=path.parent
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
    payload = json.load(sys.stdin)
    app_id = str(payload.get("app_id") or "").strip()
    app_secret = str(payload.get("app_secret") or "").strip()
    update_env(arguments.env_file, app_id, app_secret)
    print(f"updated_app_id={app_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

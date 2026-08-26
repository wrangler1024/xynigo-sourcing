#!/usr/bin/env python3
"""Copy the canonical local Web UI into the cloud service package.

The product UI lives in ``src/purchase_tool/web``.  Cloud deployment must use
that exact HTML and brand assets; only the runtime API adapter inside the HTML
selects between loopback and same-origin cloud endpoints.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "purchase_tool" / "web"
DESTINATION_ROOT = Path(__file__).resolve().parents[1] / "src" / "xynigo_auth" / "web"
FILES = (
    ("index.html", "index.html"),
    ("xynigo-logo.png", "xynigo-logo.png"),
    ("xynigo-x.png", "xynigo-x.png"),
    ("xynigo-x.ico", "xynigo-x.ico"),
    # The local server exposes the X icon at /favicon.ico. Keep the cloud
    # alias byte-for-byte identical so browsers do not fall back to the
    # separate mascot artwork stored as xynigo-favicon.ico.
    ("xynigo-x.ico", "favicon.ico"),
    ("preview-product-a.svg", "preview-product-a.svg"),
    ("preview-product-b.svg", "preview-product-b.svg"),
    ("preview-product-c.svg", "preview-product-c.svg"),
)


def sync() -> list[tuple[str, str]]:
    DESTINATION_ROOT.mkdir(parents=True, exist_ok=True)
    copied: list[tuple[str, str]] = []
    for source_name, destination_name in FILES:
        source = SOURCE_ROOT / source_name
        destination = DESTINATION_ROOT / destination_name
        payload = source.read_bytes()
        destination.write_bytes(payload)
        copied.append((destination_name, hashlib.sha256(payload).hexdigest()))
    return copied


def main() -> int:
    for filename, digest in sync():
        print(f"synced={filename} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

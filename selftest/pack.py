#!/usr/bin/env python3
"""Pack this repository's own schemas into a deterministic release artifact.

Upstream projects need a reference implementation of the artifact contract; this is
it, and it is also the publisher's self-test.  The archive is byte-reproducible so the
SHA-256 recorded in manifest.lock is stable across machines and rebuilds.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = "columnzero-schemas"
RELEASE = "1.0.0"
COMPAT = "1"
DIALECT = "https://json-schema.org/draft/2020-12/schema"
MEMBERS = ["schema-index.schema.json"]


def build_index() -> bytes:
    index = {
        "schema_index": 1,
        "project": PROJECT,
        "release": RELEASE,
        "schemas": [
            {"path": name, "compat": COMPAT, "dialect": DIALECT} for name in sorted(MEMBERS)
        ],
    }
    return json.dumps(index, indent=2, sort_keys=True).encode() + b"\n"


def pack(members: list[tuple[str, bytes]]) -> bytes:
    """Deterministic tar.gz: fixed mtimes, ownership, and modes, members in name order."""
    raw = io.BytesIO()
    # mtime=0 keeps the gzip header stable; a BytesIO fileobj stores no source filename.
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, content in sorted(members):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mtime = 0
                info.mode = 0o644
                info.type = tarfile.REGTYPE
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(content))
    return raw.getvalue()


def main() -> None:
    index = build_index()
    members = [("index.json", index)]
    for name in MEMBERS:
        members.append((name, (HERE / name).read_bytes()))

    payload = pack(members)
    if pack(members) != payload:
        raise SystemExit("archive is not reproducible")

    out = HERE / "schemas.tar.gz"
    out.write_bytes(payload)
    print(json.dumps({
        "asset": out.name,
        "project": PROJECT,
        "release": RELEASE,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Download the Modelica Association Reference-FMUs (pinned, checksum-verified) for the importer tests.

    python tools/fmi/fetch_reference_fmus.py [target_dir]   # default: .cache/reference-fmus
    export EMBEDDYN_REFERENCE_FMUS=<target_dir>              # then: pytest tests/mechatronics/test_reference_fmus.py

The archive contains native binaries; they run only in the tests that opt in
through EMBEDDYN_REFERENCE_FMUS.
"""
from __future__ import annotations

import hashlib
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = "0.0.42"
URL = f"https://github.com/modelica/Reference-FMUs/releases/download/v{VERSION}/Reference-FMUs.zip"
SHA256 = "b6b4a195b9c0f6bdd3459cd949cbb35fc21ee2049d6dab4cb7a3ba7da63b1e97"


def fetch(target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    archive = target / f"Reference-FMUs-{VERSION}.zip"
    if not archive.exists():
        with urllib.request.urlopen(URL, timeout=120) as response:
            data = response.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != SHA256:
            raise SystemExit(f"checksum mismatch for {URL}: {digest}")
        archive.write_bytes(data)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.namelist():
            if member.endswith(".fmu") and not (target / member).exists():
                if ".." in Path(member).parts:
                    raise SystemExit(f"unsafe path in archive: {member}")
                bundle.extract(member, target)
    return target


if __name__ == "__main__":
    print(fetch(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".cache/reference-fmus")))

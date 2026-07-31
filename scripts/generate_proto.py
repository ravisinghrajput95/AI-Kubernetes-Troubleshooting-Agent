#!/usr/bin/env python3
"""Regenerate the Python bindings for `/proto/`.

    python scripts/generate_proto.py           # regenerate in place
    python scripts/generate_proto.py --check   # fail if the checked-in code is stale

Generated code is committed. That is deliberate: it keeps `pip install -r
requirements.txt` sufficient to run the backend, and it makes a schema change
visible in review as a diff rather than as an invisible build-time effect. CI
runs `--check`, so the two can never drift.

protoc emits imports rooted at the proto path (`from agent.v1 import ...`),
which only resolves if the generated tree is itself on `sys.path`. Rewriting
them to the real package is the one post-processing step; doing it here keeps it
deterministic rather than leaving it to whoever runs protoc next.
"""

import argparse
import filecmp
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROTO_ROOT = REPO_ROOT / "proto"
OUTPUT_PACKAGE = "app.wire.gen"
OUTPUT_ROOT = REPO_ROOT / "backend" / "app" / "wire" / "gen"

PACKAGE_HEADER = '"""Generated from /proto. Do not edit; run scripts/generate_proto.py."""\n'


def proto_files() -> list[Path]:
    return sorted(PROTO_ROOT.rglob("*.proto"))


def generate(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"--proto_path={PROTO_ROOT}",
        f"--python_out={destination}",
        f"--pyi_out={destination}",
        # Service stubs land with M4, which is when `grpc` becomes a real
        # dependency. Until then they were deliberately omitted so the schema
        # could be reviewed without the transport being installed.
        f"--grpc_python_out={destination}",
        *[str(path) for path in proto_files()],
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"protoc failed:\n{result.stderr}")

    _rewrite_imports(destination)
    _add_package_files(destination)


def _rewrite_imports(destination: Path) -> None:
    """Point generated imports at the package the files actually live in."""
    pattern = re.compile(r"^from (agent\.[\w.]*) import ", re.MULTILINE)
    # grpc stubs additionally import their own _pb2 module by bare name.
    for path in destination.rglob("*.py*"):
        text = path.read_text()
        rewritten = pattern.sub(rf"from {OUTPUT_PACKAGE}.\1 import ", text)
        if rewritten != text:
            path.write_text(rewritten)


def _add_package_files(destination: Path) -> None:
    for directory in [destination, *[p for p in destination.rglob("*") if p.is_dir()]]:
        (directory / "__init__.py").write_text(PACKAGE_HEADER)


def differences(left: Path, right: Path) -> list[str]:
    comparison = filecmp.dircmp(left, right)
    found = [
        *comparison.left_only,
        *comparison.right_only,
        *comparison.diff_files,
    ]
    for subdirectory in comparison.common_dirs:
        found += [
            f"{subdirectory}/{name}"
            for name in differences(left / subdirectory, right / subdirectory)
        ]
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify, do not write")
    arguments = parser.parse_args()

    if not arguments.check:
        shutil.rmtree(OUTPUT_ROOT, ignore_errors=True)
        generate(OUTPUT_ROOT)
        print(f"Generated {len(proto_files())} proto file(s) into {OUTPUT_ROOT}")
        return 0

    with tempfile.TemporaryDirectory() as temporary:
        expected = Path(temporary) / "gen"
        generate(expected)
        stale = differences(expected, OUTPUT_ROOT)

    if stale:
        print("Generated protobuf code is out of date:")
        for name in sorted(stale):
            print(f"  {name}")
        print("\nRun: python scripts/generate_proto.py")
        return 1

    print("Generated protobuf code is up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

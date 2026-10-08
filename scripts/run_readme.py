#!/usr/bin/env python3
"""Run the README quickstart against the pinned stub (SC4).

Extracts the single fenced Python block between the ``docql:quickstart``
markers, writes ``invoice.pdf`` from the golden contract kit into a fresh
temporary directory, and runs the block there with the interpreter that runs
this script. Stdlib only, so a clean release venv can run it unchanged.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
START_MARKER = "<!-- docql:quickstart:start -->"
END_MARKER = "<!-- docql:quickstart:end -->"
FENCED_BLOCK = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
VERSION_PROBE = "import importlib.metadata; print(importlib.metadata.version('docql'))"
RUN_TIMEOUT_SECONDS = 120


def extract_quickstart(readme_text: str) -> str | None:
    """The one fenced python block between the markers, or None when unusable."""
    if readme_text.count(START_MARKER) != 1 or readme_text.count(END_MARKER) != 1:
        return None
    start = readme_text.find(START_MARKER)
    end = readme_text.find(END_MARKER)
    if start == -1 or end == -1 or start > end:
        return None
    blocks = FENCED_BLOCK.findall(readme_text[start + len(START_MARKER) : end])
    if len(blocks) != 1:
        return None
    block: tuple[str, str] = blocks[0]
    info, code = block
    if info.strip() != "python" or not code.strip():
        return None
    return code


def run_child(
    command: list[str], cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run a child interpreter; a timeout ends this script with exit 1."""
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"README_QUICKSTART_TIMEOUT after {RUN_TIMEOUT_SECONDS}s", file=sys.stderr)
        raise SystemExit(1) from None


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute the README quickstart.")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    parser.add_argument("--expect-version")
    args = parser.parse_args()
    readme: Path = args.readme
    expect_version: str | None = args.expect_version

    stub_url = os.environ.get("DOCQL_STUB_URL", "").rstrip("/")
    if not stub_url:
        print(
            "DOCQL_STUB_URL is not set: boot the pinned stub (scripts/stub-up.sh)", file=sys.stderr
        )
        return 1

    code = extract_quickstart(readme.read_text(encoding="utf-8"))
    if code is None:
        print("README_QUICKSTART_MISSING")
        return 1

    kit: dict[str, Any] = json.loads((ROOT / "contract" / "sdk-cases.json").read_bytes())
    golden: dict[str, Any] = json.loads((ROOT / "contract" / "golden.json").read_bytes())
    file_base64 = next(c["fileBase64"] for c in golden["cases"] if c["id"] == "ascii-query")

    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    env["DOCQL_API_KEY"] = kit["keys"]["valid"]
    env["DOCQL_API_URL"] = stub_url

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "invoice.pdf").write_bytes(base64.b64decode(file_base64))
        (work / "quickstart.py").write_text(code, encoding="utf-8")
        if expect_version is not None:
            probe = run_child([sys.executable, "-I", "-c", VERSION_PROBE], work, env)
            installed = probe.stdout.strip()
            if probe.returncode != 0 or installed != expect_version:
                sys.stderr.write(probe.stderr)
                print(f"README_QUICKSTART_VERSION_MISMATCH expected {expect_version!r}")
                print(f"installed version: {installed!r}")
                return 1
        run = run_child([sys.executable, "-I", "quickstart.py"], work, env)

    if run.returncode != 0:
        sys.stderr.write(run.stderr)
        return 1
    sys.stdout.write(run.stdout)
    if not run.stdout.endswith("\n"):
        print()
    print("README_QUICKSTART_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

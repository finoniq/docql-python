#!/usr/bin/env python3
"""Run a README example against the pinned stub (SC4, PY-06).

Extracts the single fenced Python block between the ``docql:<block>`` markers
(``--block quickstart``, the default, ``--block jobs``, ``--block spreadsheet``
or ``--block webhook``), writes ``invoice.pdf`` from the golden contract kit and
``sales.csv`` from the kit's ``sheets/plain.csv`` into a fresh temporary
directory, and runs the block there with the interpreter that runs this script.
Stdlib only, so a clean release venv can run it unchanged.

``--sheet-name NAME`` (spreadsheet block only, WR-04) writes the plain.csv bytes
as ``NAME`` instead and points both ``"sales.csv"`` literals of the block at it,
so a stub fixture name such as ``toolarge-cells-558-557.csv`` runs the refusal
branch; the tag becomes ``README_SPREADSHEET_REFUSAL``. ``--expect-line TEXT``
requires that exact line in the example's output before the OK tag prints.

The webhook block (D-23, D-09) is a receiver that serves until it is stopped:
the runner starts it on a free loopback port with a secret made for this run
(never printed) and runs five cases:

- signed: one delivery signed at run time gets 2xx plus the line
  ``event job.succeeded <id>``;
- tampered: the same headers with one body byte changed get 400 and no line;
- decoy-then-real: the same delivery with a well-formed decoy
  ``webhook-signature`` line before the real one gets 2xx plus the line;
- malformed secret and empty secret: a separate receiver started with a fixed
  dummy malformed secret, then with a set-but-empty one, answers a signed
  delivery with 500, prints no line, keeps running and leaves no Traceback on
  its stderr.

A dropped connection is reported as ``no HTTP answer``, not a crash.
"""

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

ROOT = Path(__file__).resolve().parents[1]
BLOCKS = ("quickstart", "jobs", "spreadsheet", "webhook")
FENCED_BLOCK = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
VERSION_PROBE = "import importlib.metadata; print(importlib.metadata.version('docql'))"
RUN_TIMEOUT_SECONDS = 120
SHEET_LITERAL = '"sales.csv"'
SHEET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
WEBHOOK_READY_SECONDS = 30
WEBHOOK_LINE_SECONDS = 10
WEBHOOK_EVENT_ID = "5f0c3a1e-45a1-4d1e-9b00-000000000451"
# A fixed dummy, never a real secret: well formed (whsec_ + canonical base64) but
# only 8 bytes, so verify_webhook raises a ValueError that names position 1.
WEBHOOK_MALFORMED_SECRET = "whsec_AAAAAAAAAAA="


def extract_block(readme_text: str, name: str) -> str | None:
    """The one fenced python block between the ``name`` markers, or None when unusable."""
    start_marker = f"<!-- docql:{name}:start -->"
    end_marker = f"<!-- docql:{name}:end -->"
    if readme_text.count(start_marker) != 1 or readme_text.count(end_marker) != 1:
        return None
    start = readme_text.find(start_marker)
    end = readme_text.find(end_marker)
    if start == -1 or end == -1 or start > end:
        return None
    blocks = FENCED_BLOCK.findall(readme_text[start + len(start_marker) : end])
    if len(blocks) != 1:
        return None
    block: tuple[str, str] = blocks[0]
    info, code = block
    if info.strip() != "python" or not code.strip():
        return None
    return code


def run_child(
    command: list[str], cwd: Path, env: dict[str, str], tag: str
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
        print(f"{tag}_TIMEOUT after {RUN_TIMEOUT_SECONDS}s", file=sys.stderr)
        raise SystemExit(1) from None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def post(port: int, body: bytes, headers: dict[str, str]) -> int:
    """POST to the receiver on loopback, never through a proxy; the HTTP status."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/", data=body, headers=headers, method="POST"
    )
    try:
        with opener.open(request, timeout=WEBHOOK_LINE_SECONDS) as response:
            status: int = response.status
            return status
    except urllib.error.HTTPError as err:
        return err.code
    except OSError:  # a dropped connection (http.client.RemoteDisconnected) or a refusal
        return 0


def post_raw(port: int, body: bytes, header_pairs: list[tuple[str, str]]) -> int:
    """POST with the header lines in order, repeats kept; the HTTP status, 0 for none."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=WEBHOOK_LINE_SECONDS)
    try:
        connection.putrequest("POST", "/")
        for name, value in header_pairs:
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        status: int = connection.getresponse().status
        return status
    except OSError:
        return 0
    finally:
        connection.close()


def describe_status(status: int) -> str:
    return "no HTTP answer" if status == 0 else f"HTTP {status}"


def signed_delivery(secret: str) -> tuple[bytes, dict[str, str]]:
    """A job.succeeded body in the frozen shape and its headers, signed now."""
    timestamp = int(time.time())
    sent_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    body = json.dumps(
        {
            "id": WEBHOOK_EVENT_ID,
            "type": "job.succeeded",
            "timestamp": sent_at,
            "data": {"job_id": "job_readme_7Hk2mQ9x", "status": "succeeded"},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    key = base64.b64decode(secret.removeprefix("whsec_"))
    content = f"{WEBHOOK_EVENT_ID}.{timestamp}.".encode("ascii") + body
    signature = base64.b64encode(hmac.new(key, content, hashlib.sha256).digest()).decode("ascii")
    headers = {
        "Content-Type": "application/json",
        "webhook-id": WEBHOOK_EVENT_ID,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": f"v1,{signature}",
    }
    return body, headers


class Receiver:
    """The README receiver as a child process on a free loopback port."""

    def __init__(self, work: Path, env: dict[str, str], secret: str, stderr_path: Path) -> None:
        self.port = free_port()
        self.seen: list[str] = []
        self._lines: queue.Queue[str] = queue.Queue()
        child_env = dict(env, DOCQL_WEBHOOK_SECRET=secret, PORT=str(self.port))
        with stderr_path.open("w", encoding="utf-8") as stderr_file:
            # -u: -I implies -E, so PYTHONUNBUFFERED would be ignored and print buffered.
            self.child = subprocess.Popen(
                [sys.executable, "-I", "-u", "webhook.py"],
                cwd=work,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
            )
        assert self.child.stdout is not None
        threading.Thread(target=self._pump, args=(self.child.stdout,), daemon=True).start()

    def _pump(self, stdout: IO[str]) -> None:
        for line in stdout:
            self._lines.put(line.rstrip("\n"))

    def next_line(self, wait: float) -> str | None:
        try:
            line = self._lines.get(timeout=wait)
        except queue.Empty:
            return None
        self.seen.append(line)
        return line

    def wait_ready(self) -> str | None:
        """None once the port accepts connections; else the problem."""
        deadline = time.monotonic() + WEBHOOK_READY_SECONDS
        while True:
            if self.child.poll() is not None:
                return f"the receiver exited with {self.child.returncode}"
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return None
            except OSError:
                if time.monotonic() > deadline:
                    return f"no listener after {WEBHOOK_READY_SECONDS}s"
                time.sleep(0.2)

    def stop(self) -> None:
        self.child.terminate()
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()


def run_good_secret_cases(receiver: Receiver, secret: str) -> str | None:
    """The signed, tampered and decoy-then-real cases; None or the problem."""
    problem = receiver.wait_ready()
    if problem is not None:
        return problem
    body, headers = signed_delivery(secret)
    status = post(receiver.port, body, headers)
    expected = f"event job.succeeded {WEBHOOK_EVENT_ID}"
    if not 200 <= status < 300:
        return f"the signed delivery got {describe_status(status)}"
    if receiver.next_line(WEBHOOK_LINE_SECONDS) != expected:
        return f"no line {expected!r} for the signed delivery"

    tampered = bytearray(body)
    tampered[-5] ^= 0x01  # one byte of "succeeded" changes; the JSON stays valid
    status = post(receiver.port, bytes(tampered), headers)
    if status != 400:
        return f"the tampered delivery got {describe_status(status)}"
    if receiver.next_line(1.0) is not None:
        return "the tampered delivery printed a line"

    # A well-formed v1 signature of the same content under another key, sent before the
    # real one: the receiver must pass every webhook-signature line to the helper.
    content = f"{WEBHOOK_EVENT_ID}.{headers['webhook-timestamp']}.".encode("ascii") + body
    decoy = hmac.new(os.urandom(32), content, hashlib.sha256).digest()
    pairs = [(name, headers[name]) for name in ("Content-Type", "webhook-id", "webhook-timestamp")]
    pairs.append(("webhook-signature", "v1," + base64.b64encode(decoy).decode("ascii")))
    pairs.append(("webhook-signature", headers["webhook-signature"]))
    status = post_raw(receiver.port, body, pairs)
    if not 200 <= status < 300:
        return f"the decoy-then-real delivery got {describe_status(status)}"
    if receiver.next_line(WEBHOOK_LINE_SECONDS) != expected:
        return f"no line {expected!r} for the decoy-then-real delivery"
    return None


def run_bad_secret_case(
    receiver: Receiver, label: str, secret: str, stderr_path: Path
) -> str | None:
    """A receiver with a bad secret answers 500, prints nothing and keeps running."""
    problem = receiver.wait_ready()
    if problem is not None:
        return f"{problem} (the {label} secret)"
    body, headers = signed_delivery(secret)
    status = post(receiver.port, body, headers)
    if status != 500:
        return f"the {label} secret got {describe_status(status)}"
    if receiver.next_line(1.0) is not None:
        return f"the {label} secret printed a line"
    if receiver.child.poll() is not None:
        return f"the receiver exited after the {label} secret"
    if "Traceback" in stderr_path.read_text(encoding="utf-8"):
        return f"the {label} secret left a Traceback on the receiver's stderr"
    return None


def run_webhook(work: Path, env: dict[str, str]) -> tuple[bool, str, str]:
    """Run the five receiver cases; (ok, stdout, problem)."""
    secret = "whsec_" + base64.b64encode(os.urandom(32)).decode("ascii")
    receiver = Receiver(work, env, secret, work / "webhook.stderr")
    try:
        problem = run_good_secret_cases(receiver, secret)
    finally:
        receiver.stop()
    seen = receiver.seen
    if problem is not None:
        return False, "\n".join(seen), problem

    for label, bad_secret in (("malformed", WEBHOOK_MALFORMED_SECRET), ("empty", "")):
        stderr_path = work / f"webhook-{label}.stderr"
        bad = Receiver(work, env, bad_secret, stderr_path)
        try:
            problem = run_bad_secret_case(bad, label, secret, stderr_path)
        finally:
            bad.stop()
        seen = seen + bad.seen
        if problem is not None:
            return False, "\n".join(seen), problem
    return True, "\n".join(seen) + "\n", ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute a README example.")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    parser.add_argument("--expect-version")
    parser.add_argument("--block", choices=BLOCKS, default="quickstart")
    parser.add_argument("--sheet-name", help="spreadsheet block: the CSV file name to send")
    parser.add_argument("--expect-line", help="an exact line the example must print")
    args = parser.parse_args()
    readme: Path = args.readme
    expect_version: str | None = args.expect_version
    block: str = args.block
    sheet_name: str | None = args.sheet_name
    expect_line: str | None = args.expect_line
    tag = f"README_{block.upper()}"
    if sheet_name is not None:
        if block != "spreadsheet":
            parser.error("--sheet-name needs --block spreadsheet")
        if not SHEET_NAME.fullmatch(sheet_name) or not sheet_name.lower().endswith(".csv"):
            parser.error("--sheet-name must be a plain file name ending in .csv")
        tag = "README_SPREADSHEET_REFUSAL"

    stub_url = os.environ.get("DOCQL_STUB_URL", "").rstrip("/")
    if not stub_url:
        print(
            "DOCQL_STUB_URL is not set: boot the pinned stub (scripts/stub-up.sh)", file=sys.stderr
        )
        return 1

    code = extract_block(readme.read_text(encoding="utf-8"), block)
    if code is None:
        print(f"{tag}_MISSING")
        return 1
    csv_name = "sales.csv"
    if sheet_name is not None:
        if code.count(SHEET_LITERAL) != 2:
            print(f"{tag}_UNEXPECTED_BLOCK: want exactly two {SHEET_LITERAL} literals")
            return 1
        code = code.replace(SHEET_LITERAL, json.dumps(sheet_name))
        csv_name = sheet_name

    kit: dict[str, Any] = json.loads((ROOT / "contract" / "sdk-cases.json").read_bytes())
    golden: dict[str, Any] = json.loads((ROOT / "contract" / "golden.json").read_bytes())
    file_base64 = next(c["fileBase64"] for c in golden["cases"] if c["id"] == "ascii-query")

    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    env["DOCQL_API_KEY"] = kit["keys"]["valid"]
    env["DOCQL_API_URL"] = stub_url

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "invoice.pdf").write_bytes(base64.b64decode(file_base64))
        (work / csv_name).write_bytes((ROOT / "contract" / "sheets" / "plain.csv").read_bytes())
        (work / f"{block}.py").write_text(code, encoding="utf-8")
        if expect_version is not None:
            probe = run_child([sys.executable, "-I", "-c", VERSION_PROBE], work, env, tag)
            installed = probe.stdout.strip()
            if probe.returncode != 0 or installed != expect_version:
                sys.stderr.write(probe.stderr)
                print(f"{tag}_VERSION_MISMATCH expected {expect_version!r}")
                print(f"installed version: {installed!r}")
                return 1
        if block == "webhook":
            ok, stdout, problem = run_webhook(work, env)
            if not ok:
                sys.stdout.write(stdout + ("\n" if stdout else ""))
                sys.stderr.write((work / "webhook.stderr").read_text(encoding="utf-8"))
                print(f"{tag}_FAIL {problem}")
                return 1
        else:
            run = run_child([sys.executable, "-I", f"{block}.py"], work, env, tag)
            if run.returncode != 0:
                sys.stderr.write(run.stderr)
                return 1
            stdout = run.stdout

    sys.stdout.write(stdout)
    if not stdout.endswith("\n"):
        print()
    if expect_line is not None and expect_line not in stdout.splitlines():
        print(f"{tag}_EXPECTED_LINE_MISSING {expect_line!r}")
        return 1
    print(f"{tag}_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Every stub-target kit v3 job case through the SDK's own wire layer (JSDK-04).

This is a port of the kit's reference runners (docql-console
``deploy/scripts/job-cases-edge.mjs`` ``runCase`` and ``apps/docql-stub/test/job-kit.ts``
``runJobCase``), op by op, not a new interpretation. Two layers (research Pattern 5):

* ``submit``, ``join``, ``poll`` and ``cancel`` steps call the private single-attempt
  primitive ``docql._jobs._send_once`` exactly once per request, never the retrying
  public ``submit_document()`` (research F8: j16, j28 and j28-unknown assert the raw
  first answer). Each answer is checked raw (every wire field the kit names) and through
  the SDK's decoders (``parse_job`` for a 2xx, ``decode_error`` otherwise).
* ``wait`` steps call the public ``wait()``; ``sync`` steps call the public
  ``query_document()``.

Step semantics, as in the reference runners: ``{run}`` is replaced in query, prompt and
idempotency-key strings with one 8-character base62 token per module run; ``as`` binds the
job id of a 2xx submit and ``job`` names it; ``key`` picks the kit's ``valid``,
``validOther`` or ``invalid`` API key; an ``async`` submit runs on its own thread until its
``join``; ``delayBeforeMs`` and ``sleep`` use real time (the kit's stubEnv keeps a job for
5 s); a step's ``boundary`` (else the kit's default) goes through v0.1's boundary seam
``docql._request.new_boundary``; an ``idempotencyKey`` is one string, a ``lines`` list sent
as several raw header lines, or a ``repeat`` x ``length`` string. Every answer from a job
route, including each GET inside ``wait()``, must meet the kit's job-route invariants.

Cases are independent: each one builds its own clients, transport, bindings and captures,
and every fingerprint and key embeds the case id plus the run token, so the order the
cases run in never changes an outcome.

Edge mode (``DOCQL_CONTRACT_EDGE=1``, 26-11): docql-console ``deploy/scripts/sdk-jobs-edge.sh``
runs this module through Caddy -> gateway -> stub. Only the case ids in
``DOCQL_EDGE_CASE_IDS`` run (the ones the console's Node edge runner replayed); keys
``valid`` and ``validOther`` become the gateway keys in ``DOCQL_EDGE_KEY`` (test org 1) and
``DOCQL_EDGE_KEY_OTHER`` (test org 2), so the cross-org cases prove the gateway's tenant;
``invalid`` stays the kit's never-issued key; TLS trusts only the Caddy root CA in
``DOCQL_EDGE_CA``. Outside edge mode nothing changes.
"""

import base64
import json
import os
import re
import secrets
import ssl
import string
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from docql import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_SUCCEEDED,
    DocQL,
    DocQLError,
    DocQLWaitTimeoutError,
    Job,
    QueryResult,
    _jobs,
    _request,
)
from docql._constants import CONNECT_TIMEOUT, JOBS_PATH, POLL_READ_TIMEOUT
from docql._jobs import _send_once, job_path, parse_job
from docql._request import prepare_upload
from docql._response import decode_error

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
KIT: dict[str, Any] = json.loads((ROOT / "contract" / "job-cases.json").read_bytes())
API_KEYS: dict[str, str] = {name: KIT["keys"][name] for name in ("valid", "validOther", "invalid")}
EDGE = os.environ.get("DOCQL_CONTRACT_EDGE") == "1"


def edge_env(name: str) -> str:
    """An edge-mode input; a missing one fails collection loudly, never a silent skip."""
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"edge mode (DOCQL_CONTRACT_EDGE=1) needs env {name}")
    return value


if EDGE:
    API_KEYS["valid"] = edge_env("DOCQL_EDGE_KEY")
    API_KEYS["validOther"] = edge_env("DOCQL_EDGE_KEY_OTHER")
KNOWN_VIAS = ("submit", "poll", "cancel", "wait", "sync", "infra")
RUN_VIAS = ("submit", "poll", "cancel", "wait")

# job-kit.ts DEFAULT_BOUNDARY: the multipart boundary of a step that names none.
DEFAULT_BOUNDARY = "docqlsdktestboundary0000000001"
# A poll-until gives up after this many seconds of real sleeping (job-cases-edge.mjs).
POLL_UNTIL_SECONDS = 120
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})

# One token per module run: 8 base62 characters (kit invariant 8).
RUN = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
assert re.fullmatch(r"[A-Za-z0-9]{8}", RUN)

# Every case this module runs: a stub case replayed through the SDK (44 in kit v3).
JOB_CASES: list[dict[str, Any]] = [
    c for c in KIT["cases"] if c["target"] == "stub" and c["via"] in RUN_VIAS
]


def edge_cases() -> list[dict[str, Any]]:
    """The cases DOCQL_EDGE_CASE_IDS names; an id this module does not run fails collection."""
    ids = {i for i in edge_env("DOCQL_EDGE_CASE_IDS").split(",") if i}
    unknown = sorted(ids - {c["id"] for c in JOB_CASES})
    if unknown:
        raise RuntimeError(f"DOCQL_EDGE_CASE_IDS names cases this runner does not run: {unknown}")
    return [c for c in JOB_CASES if c["id"] in ids]


# The cases test_job_case runs: every one, or in edge mode the edge-replayed subset.
RUN_CASES = edge_cases() if EDGE else JOB_CASES

# Every other case is excluded by exactly one named rule (the first that matches).
EXCLUSIONS: tuple[tuple[str, Callable[[dict[str, Any]], bool]], ...] = (
    (
        "target real: runs only against the real DocQL (kit invariant 7)",
        lambda c: c["target"] == "real",
    ),
    ("target console: needs the trusted key and tenant header", lambda c: c["target"] == "console"),
    ("via sync: sdk-cases.json, run by test_shared_cases.py", lambda c: c["via"] == "sync"),
    ("via infra: deployment checks, no SDK call", lambda c: c["via"] == "infra"),
)

# The expect fields this runner checks: every field job-kit.ts checks.
IMPLEMENTED_EXPECT = frozenset(
    {
        "status",
        "reason",
        "retryAfterSeconds",
        "location",
        "replayed",
        "jobStatus",
        "scope",
        "header",
        "pageCount",
        "resultPageCount",
        "embeddedReason",
        "embeddedStatus",
        "sameJobAs",
        "differentJobFrom",
        "bodyEquals",
        "bodyEqualsExceptRequestId",
        "requestIdDiffersFrom",
        "resultEqualsSync",
        "errorEqualsSync",
        "states",
    }
)
WAIT_EXPECT = frozenset({"outcome", "reason"})

JOB_KEYS = ["job_id", "status", "metadata", "created_at", "started_at", "finished_at", "expires_at"]
JOB_METADATA_KEYS = ["page_count", "pages_done", "request_id"]
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
WHOLE_SECONDS = re.compile(r"^[1-9][0-9]*$")


# --- selection ------------------------------------------------------------------------------


def classify(case: dict[str, Any]) -> str | None:
    if case in JOB_CASES:
        return "run"
    for name, rule in EXCLUSIONS:
        if rule(case):
            return name
    return None


def test_every_job_case_uses_a_known_via() -> None:
    """A case with an unknown via must fail the run, never silently disappear."""
    unknown = [c["id"] for c in KIT["cases"] if c.get("via") not in KNOWN_VIAS]
    assert not unknown, f"job-cases.json carries cases with an unknown via: {unknown}"


def test_every_job_case_is_classified() -> None:
    """Each kit case is run here or excluded by one named rule; a new kind fails loudly."""
    unclassified = [c["id"] for c in KIT["cases"] if classify(c) is None]
    assert not unclassified, f"job cases neither run nor excluded by a named rule: {unclassified}"
    for case in JOB_CASES:
        assert case.get("ref") is None and case.get("steps"), case["id"]
    assert JOB_CASES, "no stub job case selected"


# --- wire capture ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Wire:
    """One answer as it came off the wire."""

    status: int
    headers: httpx.Headers
    body: bytes


class CapturingTransport(httpx.BaseTransport):
    """The real transport, plus a record of every answer (its body read in full).

    Given an edge TLS context, the transport verifies against it and dials 127.0.0.1 at the
    URL's port while the URL host stays the TLS server name, the certificate name and the
    Host header (curl --resolve): the console never relies on ``*.localhost`` resolving.
    """

    def __init__(self, edge_ssl: ssl.SSLContext | None = None) -> None:
        self._edge = edge_ssl is not None
        self._inner = (
            httpx.HTTPTransport() if edge_ssl is None else httpx.HTTPTransport(verify=edge_ssl)
        )
        self._lock = threading.Lock()
        self.seen: list[tuple[str, str, Wire]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self._edge:
            request.extensions = {**request.extensions, "sni_hostname": request.url.host}
            request.url = request.url.copy_with(host="127.0.0.1")
        response = self._inner.handle_request(request)
        body = response.read()  # also closes the stream; the client reads the cached bytes
        with self._lock:
            self.seen.append(
                (
                    request.method,
                    request.url.path,
                    Wire(response.status_code, response.headers, body),
                )
            )
        return response

    def close(self) -> None:
        self._inner.close()


@pytest.fixture
def transport(edge_ssl_context: ssl.SSLContext | None) -> Iterator[CapturingTransport]:
    capture = CapturingTransport(edge_ssl_context)
    yield capture
    capture.close()


# --- helpers ported from job-kit.ts ---------------------------------------------------------


class CaseError(AssertionError):
    pass


def body_json(wire: Wire) -> Any:
    try:
        return json.loads(wire.body)
    except ValueError:
        return None


def field_of(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def same_json(a: Any, b: Any) -> bool:
    """Deep JSON equality that, like node's isDeepStrictEqual, never equates true with 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict):
        return (
            isinstance(b, dict) and a.keys() == b.keys() and all(same_json(a[k], b[k]) for k in a)
        )
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(map(same_json, a, b))
    return bool(a == b) and type(a) is type(b)


def without_request_id(value: Any, drop_extraction_status: bool = False) -> Any:
    """A deep copy without metadata.request_id (and, when asked, metadata.extraction_status)."""
    copy = json.loads(json.dumps(value))
    metadata = field_of(copy, "metadata")
    if isinstance(metadata, dict):
        metadata.pop("request_id", None)
        if drop_extraction_status:
            metadata.pop("extraction_status", None)
    return copy


def file_bytes(file: dict[str, Any]) -> bytes:
    if "base64" in file:
        decoded: bytes = base64.b64decode(file["base64"])
        return decoded
    repeated: bytes = file["repeatByte"].encode("ascii") * int(file["length"])
    return repeated


def check_job_invariants(where: str, wire: Wire) -> None:
    """Kit invariants 1-5 on one job-route answer (job-kit.ts checkJobInvariants)."""

    def fail(message: str) -> None:
        raise CaseError(f"{where}: invariant: {message}")

    if wire.headers.get("cache-control") != "no-store":
        fail(f"Cache-Control is {wire.headers.get('cache-control')!r}, not no-store")
    request_id = wire.headers.get("x-request-id")
    if not request_id:
        fail("no X-Request-Id")
    body = body_json(wire)
    if isinstance(body, dict) and "error_info" in body:
        if field_of(body, "metadata", "request_id") != request_id:
            fail("envelope metadata.request_id differs from X-Request-Id")
    if isinstance(body, dict) and "job_id" in body:
        expected = list(JOB_KEYS)
        if body.get("status") == "succeeded":
            expected.append("result")
        if body.get("status") == "failed":
            expected.append("error")
        if list(body) != expected:
            fail(f"job keys {','.join(body)}")
        metadata = body.get("metadata")
        if not isinstance(metadata, dict) or list(metadata) != JOB_METADATA_KEYS:
            fail("job metadata keys out of order")
        for key in ("created_at", "started_at", "finished_at", "expires_at"):
            value = body.get(key)
            if value is not None and (not isinstance(value, str) or not TIMESTAMP.match(value)):
                fail(f"{key} {value!r} is not second-precision RFC 3339 UTC")
        if not isinstance(body.get("created_at"), str):
            fail("created_at is null")
    retry_after = wire.headers.get("retry-after")
    if retry_after is not None and not WHOLE_SECONDS.match(retry_after):
        fail(f"Retry-After {retry_after} is not a whole number >= 1")
    location = wire.headers.get("location")
    if location is not None:
        job_id = field_of(body, "job_id")
        if not isinstance(job_id, str) or location != job_path(job_id):
            fail(f"Location {location} is not /v1/jobs/{{job_id}}")


def dedupe(states: list[str]) -> list[str]:
    return [s for i, s in enumerate(states) if i == 0 or states[i - 1] != s]


def is_subsequence(observed: list[str], allowed: list[str]) -> bool:
    j = 0
    for state in observed:
        while j < len(allowed) and allowed[j] != state:
            j += 1
        if j == len(allowed):
            return False
        j += 1
    return True


# --- the runner -----------------------------------------------------------------------------


@dataclass
class Pending:
    thread: threading.Thread
    name: str | None
    result: list[Wire] = field(default_factory=list)
    error: list[BaseException] = field(default_factory=list)


class CaseRunner:
    def __init__(
        self,
        case: dict[str, Any],
        stub_url: str,
        transport: CapturingTransport,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.case = case
        self.transport = transport
        self.monkeypatch = monkeypatch
        self.http = httpx.Client(transport=transport)
        self.clients = {
            name: DocQL(api_key=key, api_url=stub_url, http_client=self.http)
            for name, key in API_KEYS.items()
        }
        self.jobs: dict[str, str] = {}
        self.pending: dict[str, Pending] = {}
        self.captured: dict[str, Wire] = {}
        self.checked = 0  # transport answers already checked against the invariants
        # The boundary seam is module state; an async submit builds its upload on its own
        # thread, so patching the seam and framing the upload happen under one lock.
        self.seam_lock = threading.Lock()

    # step inputs

    def sub(self, text: str) -> str:
        out = text.replace("{run}", RUN)
        assert "{run}" not in out
        return out

    def client(self, step: dict[str, Any], where: str) -> DocQL:
        key = step.get("key", "valid")
        if key not in self.clients:
            raise CaseError(f"{where}: key {key!r} is not one the SDK can send")
        return self.clients[key]

    def idempotency_keys(self, step: dict[str, Any]) -> tuple[str, ...]:
        idem = step.get("idempotencyKey")
        if idem is None:
            return ()
        if isinstance(idem, str):
            return (self.sub(idem),)
        if "lines" in idem:
            return tuple(self.sub(line) for line in idem["lines"])
        return (idem["repeat"] * int(idem["length"]),)

    def path_of(self, step: dict[str, Any], where: str) -> str:
        if "id" in step:
            return job_path(step["id"])
        job_id = self.jobs.get(step.get("job", ""))
        if job_id is None:
            raise CaseError(f"{where}: job {step.get('job')} was never bound by a 2xx submit")
        return job_path(job_id)

    def upload(self, step: dict[str, Any]) -> _request.Upload:
        request = step["request"]
        boundary = step.get("boundary", DEFAULT_BOUNDARY)
        with self.seam_lock:
            self.monkeypatch.setattr(_request, "new_boundary", lambda: boundary)
            return prepare_upload(
                file_bytes(request["file"]),
                query=self.sub(request["query"]) if "query" in request else None,
                prompt=self.sub(request["prompt"]) if "prompt" in request else None,
                mode=request.get("mode"),
                filename=request["file"]["name"],
            )

    # one request through the single-attempt primitive

    def send_submit(self, step: dict[str, Any], where: str) -> Wire:
        client = self.client(step, where)
        keys = self.idempotency_keys(step)
        refused = []
        for k in keys:
            try:
                _jobs.validate_idempotency_key(k)
            except ValueError:
                refused.append(k)
        with self.upload(step) as upload, self.monkeypatch.context() as patch:
            if refused:
                # The SDK refuses this key before any I/O (proved just above); the case
                # asserts the server's own answer to it, so the check is bypassed for
                # this one request only.
                patch.setattr(_jobs, "validate_idempotency_key", lambda key: key)
            status, headers, body = _send_once(
                client,
                "POST",
                JOBS_PATH,
                upload=upload,
                idempotency_keys=keys,
                timeout=client._timeout,
            )
        return Wire(status, headers, body)

    def send_job(self, step: dict[str, Any], where: str, method: str, path: str) -> Wire:
        client = self.client(step, where)
        status, headers, body = _send_once(
            client, method, path, timeout=httpx.Timeout(POLL_READ_TIMEOUT, connect=CONNECT_TIMEOUT)
        )
        return Wire(status, headers, body)

    # checks

    def check_new_answers(self, where: str) -> None:
        """Invariants on every job-route answer the transport saw since the last check."""
        seen = self.transport.seen
        for method, path, wire in seen[self.checked :]:
            if path == JOBS_PATH or path.startswith(JOBS_PATH + "/"):
                check_job_invariants(f"{where} {method} {path[:60]}", wire)
        self.checked = len(seen)

    def capture(self, name: str, where: str) -> Wire:
        if name not in self.captured:
            raise CaseError(f"{where}: capture {name} is missing")
        return self.captured[name]

    def check_expect(
        self, where: str, expect: dict[str, Any], wire: Wire, api_key: str, *, job_route: bool
    ) -> None:
        unknown = set(expect) - IMPLEMENTED_EXPECT
        if unknown:
            raise CaseError(f"{where}: expect fields not implemented: {sorted(unknown)}")

        def eq(what: str, actual: Any, expected: Any) -> None:
            if not same_json(actual, expected):
                raise CaseError(f"{where}: {what}: expected {expected!r}, got {actual!r}")

        body = body_json(wire)
        ok = 200 <= wire.status < 300
        # The SDK's own decode of the same answer: a Job for a 2xx job answer, else DocQLError.
        job: Job | None = None
        error: DocQLError | None = None
        if job_route and ok:
            job = parse_job(wire.status, wire.headers, wire.body, api_key)
        elif not ok:
            error = decode_error(wire.status, wire.headers, wire.body, api_key)

        if "status" in expect:
            eq("status", wire.status, expect["status"])
            if error is not None:
                eq("decoded status", error.status, expect["status"])
        if "reason" in expect:
            eq("reason", string_or_none(field_of(body, "metadata", "reason")), expect["reason"])
            if error is not None:
                eq("decoded reason", error.reason, expect["reason"])
        if "retryAfterSeconds" in expect:
            raw = wire.headers.get("retry-after")
            eq("Retry-After", None if raw is None else int(raw), expect["retryAfterSeconds"])
            decoded = job.retry_after if job is not None else error.retry_after if error else None
            eq("decoded retry_after", decoded, expect["retryAfterSeconds"])
        if "location" in expect:
            location = wire.headers.get("location")
            if expect["location"]:
                job_id = field_of(body, "job_id")
                eq("Location", location, job_path(job_id) if isinstance(job_id, str) else None)
            else:
                eq("Location", location, None)
        if "replayed" in expect:
            eq(
                "Idempotent-Replayed",
                wire.headers.get("idempotent-replayed"),
                "true" if expect["replayed"] else None,
            )
        if "jobStatus" in expect:
            eq("job status", field_of(body, "status"), expect["jobStatus"])
            eq("decoded job status", job.status if job else None, expect["jobStatus"])
        if "scope" in expect:
            eq("scope", field_of(body, "metadata", "scope"), expect["scope"])
        if "header" in expect:
            eq("metadata.header", field_of(body, "metadata", "header"), expect["header"])
        if "pageCount" in expect:
            eq("page_count", field_of(body, "metadata", "page_count"), expect["pageCount"])
            eq("decoded page_count", job.page_count if job else None, expect["pageCount"])
        if "resultPageCount" in expect:
            eq(
                "result page_count",
                field_of(body, "result", "metadata", "page_count"),
                expect["resultPageCount"],
            )
            result = job.result if job else None
            eq(
                "decoded result page_count",
                result.page_count if result else None,
                expect["resultPageCount"],
            )
        if "embeddedReason" in expect:
            eq(
                "embedded reason",
                field_of(body, "error", "metadata", "reason"),
                expect["embeddedReason"],
            )
            embedded = job.error if job else None
            eq(
                "decoded embedded reason",
                embedded.reason if embedded else None,
                expect["embeddedReason"],
            )
            assert embedded is not None and embedded.retryable is False, (
                f"{where}: embedded retryable"
            )
        if "embeddedStatus" in expect:
            eq("embedded status", field_of(body, "error", "status_code"), expect["embeddedStatus"])
            embedded = job.error if job else None
            eq(
                "decoded embedded status",
                embedded.status if embedded else None,
                expect["embeddedStatus"],
            )
        if "sameJobAs" in expect:
            eq("job_id", field_of(body, "job_id"), self.jobs.get(expect["sameJobAs"]))
            eq("decoded job_id", job.job_id if job else None, self.jobs.get(expect["sameJobAs"]))
        if "differentJobFrom" in expect:
            other = self.jobs.get(expect["differentJobFrom"])
            if other is None or field_of(body, "job_id") == other:
                raise CaseError(
                    f"{where}: job_id should differ from job {expect['differentJobFrom']}"
                )
        if "bodyEquals" in expect and wire.body != self.capture(expect["bodyEquals"], where).body:
            raise CaseError(f"{where}: body differs from {expect['bodyEquals']} byte for byte")
        if "bodyEqualsExceptRequestId" in expect:
            other_body = body_json(self.capture(expect["bodyEqualsExceptRequestId"], where))
            eq(
                f"body (except request_id) vs {expect['bodyEqualsExceptRequestId']}",
                without_request_id(body),
                without_request_id(other_body),
            )
        if "requestIdDiffersFrom" in expect:
            mine = wire.headers.get("x-request-id")
            if mine == self.capture(expect["requestIdDiffersFrom"], where).headers.get(
                "x-request-id"
            ):
                raise CaseError(f"{where}: X-Request-Id equals {expect['requestIdDiffersFrom']}'s")
        # Handoff §7: a job's result (or error) equals the sync body for the same input,
        # except metadata.request_id and, on success, metadata.extraction_status.
        if "resultEqualsSync" in expect:
            sync = body_json(self.capture(expect["resultEqualsSync"], where))
            eq(
                f"result vs sync {expect['resultEqualsSync']} (§7)",
                without_request_id(field_of(body, "result"), True),
                without_request_id(sync, True),
            )
        if "errorEqualsSync" in expect:
            sync = body_json(self.capture(expect["errorEqualsSync"], where))
            eq(
                f"error vs sync {expect['errorEqualsSync']} (§7)",
                without_request_id(field_of(body, "error")),
                without_request_id(sync),
            )

    def finish(
        self, step: dict[str, Any], where: str, wire: Wire, *, job_route: bool, bind: str | None
    ) -> None:
        bound = field_of(body_json(wire), "job_id")
        if bind is not None and isinstance(bound, str) and wire.status < 300:
            self.jobs[bind] = bound
        if "expect" in step:
            api_key = self.client(step, where)._api_key
            expect = {k: v for k, v in step["expect"].items() if k != "states"}
            self.check_expect(where, expect, wire, api_key, job_route=job_route)

    # ops

    def run(self) -> None:
        for index, step in enumerate(self.case["steps"]):
            where = f"{self.case['id']} step {index} ({step['op']})"
            if "delayBeforeMs" in step:
                time.sleep(step["delayBeforeMs"] / 1000)
            op = step["op"]
            wire: Wire | None = None
            if op == "sleep":
                time.sleep(step["seconds"])
            elif op == "submit":
                if step.get("async") is True:
                    self.start_async(step, where)
                else:
                    wire = self.send_submit(step, where)
                    self.finish(step, where, wire, job_route=True, bind=step.get("as"))
            elif op == "join":
                wire = self.join(step, where)
            elif op == "sync":
                wire = self.sync(step, where)
            elif op == "cancel":
                wire = self.send_job(step, where, "DELETE", self.path_of(step, where))
                self.finish(step, where, wire, job_route=True, bind=None)
            elif op == "poll":
                wire = self.poll(step, where)
            elif op == "wait":
                self.wait(step, where)
            else:
                raise CaseError(f"{where}: unsupported op {op!r}")
            self.check_new_answers(where)
            if wire is not None and "name" in step and not (op == "submit" and step.get("async")):
                self.captured[step["name"]] = wire
        if self.pending:
            for pending in self.pending.values():
                pending.thread.join()
            raise CaseError(f"async submit never joined: {sorted(self.pending)}")

    def start_async(self, step: dict[str, Any], where: str) -> None:
        if "as" not in step:
            raise CaseError(f"{where}: async without as")
        pending = Pending(thread=threading.Thread(target=lambda: None), name=step.get("name"))

        def target() -> None:
            try:
                pending.result.append(self.send_submit(step, where))
            except BaseException as exc:  # re-raised by the join
                pending.error.append(exc)

        pending.thread = threading.Thread(target=target, daemon=True)
        self.pending[step["as"]] = pending
        pending.thread.start()

    def join(self, step: dict[str, Any], where: str) -> Wire:
        pending = self.pending.pop(step.get("job", ""), None)
        if pending is None:
            raise CaseError(f"{where}: nothing to join")
        pending.thread.join(timeout=60)
        if pending.error:
            raise pending.error[0]
        if not pending.result:
            raise CaseError(f"{where}: the async submit did not finish within 60 s")
        wire = pending.result[0]
        self.finish(step, where, wire, job_route=True, bind=step["job"])
        if pending.name is not None:
            self.captured[pending.name] = wire
        return wire

    def sync(self, step: dict[str, Any], where: str) -> Wire:
        """The public query_document(); the transport supplies the raw answer for §7."""
        client = self.client(step, where)
        request = step["request"]
        before = len(self.transport.seen)
        boundary = step.get("boundary", DEFAULT_BOUNDARY)
        with self.seam_lock:
            self.monkeypatch.setattr(_request, "new_boundary", lambda: boundary)
        kwargs: dict[str, Any] = {"filename": request["file"]["name"]}
        for name in ("query", "prompt"):
            if name in request:
                kwargs[name] = self.sub(request[name])
        if "mode" in request:
            kwargs["mode"] = request["mode"]
        result: QueryResult | None = None
        error: DocQLError | None = None
        try:
            result = client.query_document(file_bytes(request["file"]), **kwargs)
        except DocQLError as exc:
            error = exc
        answers = self.transport.seen[before:]
        if len(answers) != 1:
            raise CaseError(f"{where}: query_document made {len(answers)} requests, not 1")
        wire = answers[0][2]
        expect = step.get("expect", {})
        if "status" in expect and expect["status"] == 200:
            assert result is not None, f"{where}: query_document raised {error!r}"
        elif "status" in expect:
            assert error is not None and error.status == expect["status"], f"{where}: {error!r}"
        self.finish(step, where, wire, job_route=False, bind=None)
        return wire

    def poll(self, step: dict[str, Any], where: str) -> Wire:
        path = self.path_of(step, where)
        until = step.get("until")
        if until is None:
            wire = self.send_job(step, where, "GET", path)
            self.finish(step, where, wire, job_route=True, bind=None)
            return wire
        # Poll until the named status, sleeping each Retry-After in real time.
        observed: list[str] = []
        slept = 0
        index = 0
        while True:
            wire = self.send_job(step, where, "GET", path)
            check_job_invariants(f"{where} poll {index}", wire)
            status = field_of(body_json(wire), "status")
            if wire.status != 200 or not isinstance(status, str):
                break
            observed.append(status)
            if status == until:
                break
            if status in TERMINAL:
                raise CaseError(f"{where}: reached {status} before {until}")
            retry_after = wire.headers.get("retry-after")
            if retry_after is None:
                raise CaseError(f"{where}: a {status} poll without Retry-After")
            if slept + int(retry_after) > POLL_UNTIL_SECONDS:
                raise CaseError(f"{where}: {until} not reached within {POLL_UNTIL_SECONDS} s")
            time.sleep(int(retry_after))
            slept += int(retry_after)
            index += 1
        self.finish(step, where, wire, job_route=True, bind=None)
        states = dedupe(observed)
        allowed = step.get("expect", {}).get("states")
        if allowed is not None and not is_subsequence(states, allowed):
            raise CaseError(f"{where}: observed states {states} are not a subsequence of {allowed}")
        return wire

    def wait(self, step: dict[str, Any], where: str) -> None:
        """The public wait(); the outcome is read the way the kit's waitForJob reports it."""
        expect = step.get("expect", {})
        unknown = set(expect) - WAIT_EXPECT
        if unknown:
            raise CaseError(
                f"{where}: a wait step checks only outcome and reason, not {sorted(unknown)}"
            )
        client = self.client(step, where)
        job_id = self.jobs.get(step["job"])
        if job_id is None:
            raise CaseError(f"{where}: job {step['job']} was never bound by a 2xx submit")
        kwargs: dict[str, Any] = {}
        if "timeoutSeconds" in step:
            kwargs["timeout"] = float(step["timeoutSeconds"])
        before = len(self.transport.seen)
        outcome: str
        reason: str | None = None
        try:
            done = client.wait(job_id, **kwargs)
        except DocQLWaitTimeoutError:
            outcome = "timeout"
        except DocQLError as exc:
            assert exc.job_id == job_id, f"{where}: wait error without the job id"
            last = self.transport.seen[-1][2]
            failed_job = last.status == 200 and field_of(body_json(last), "status") == "failed"
            outcome = "failed" if failed_job else "error"
            if failed_job:
                assert exc.retryable is False, f"{where}: a failed job's error is retryable"
            reason = exc.reason
        else:
            assert done.job_id == job_id
            if done.status == JOB_STATUS_SUCCEEDED:
                assert isinstance(done.result, QueryResult), f"{where}: succeeded without a result"
            elif done.status == JOB_STATUS_CANCELLED:
                assert done.result is None
            outcome = done.status
        methods = {method for method, _path, _wire in self.transport.seen[before:]}
        assert methods == {"GET"}, f"{where}: wait() sent {sorted(methods)}, never a DELETE"
        if "outcome" in expect and outcome != expect["outcome"]:
            raise CaseError(f"{where}: outcome {outcome}, expected {expect['outcome']}")
        if "reason" in expect and reason != expect["reason"]:
            raise CaseError(f"{where}: reason {reason!r}, expected {expect['reason']!r}")


@pytest.mark.parametrize("case", RUN_CASES, ids=[c["id"] for c in RUN_CASES])
def test_job_case(
    case: dict[str, Any],
    stub_url: str,
    transport: CapturingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CaseRunner(case, stub_url, transport, monkeypatch)
    try:
        runner.run()
    finally:
        runner.http.close()

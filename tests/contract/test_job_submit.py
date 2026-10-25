"""The public submit's same-key retry against the stub's persist and hold knobs (JSDK-02, D-07).

These go through the public ``submit_document()`` (the retrying method), unlike the kit
wire steps in ``test_job_cases.py``, which use the single-attempt primitive (research F8).
A counting transport wraps the real one, so each test asserts how many requests reached
the stub and which key each one carried. Every query and key embeds a per-run token,
so a rerun against a long-lived stub never replays an earlier run's job.

The stub knobs are filename-based and live because the kit's stubEnv sets
STUB_TEST_KNOBS=true: ``persist-unknown`` creates the job and still answers 503
Retry-After 5 (a same-key retry replays the 202), ``persist-503`` answers 503
Retry-After 5 without a job, ``hold-1500`` keeps the key reserved for 1.5 s so a
concurrent same-key submit gets 409 ``idempotency_in_progress`` Retry-After 1.
"""

import base64
import json
import os
import re
import secrets
import string
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from docql import JOB_STATUS_QUEUED, DocQL, DocQLError, Job, _jobs

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
KIT: dict[str, Any] = json.loads((ROOT / "contract" / "job-cases.json").read_bytes())
API_KEY: str = KIT["keys"]["valid"]
# The kit's own PDF bytes (j16 / j28 use the same document).
PDF: bytes = base64.b64decode(
    next(c for c in KIT["cases"] if c["id"] == "j28-unknown")["steps"][0]["request"]["file"][
        "base64"
    ]
)

RUN = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
assert re.fullmatch(r"[A-Za-z0-9]{8}", RUN)


@dataclass
class Seen:
    method: str
    keys: list[str]
    status: int
    replayed: bool
    retry_after: str | None


class CountingTransport(httpx.BaseTransport):
    """Pass every request to the real transport and record what went out and came back."""

    def __init__(self) -> None:
        self._inner = httpx.HTTPTransport()
        self._lock = threading.Lock()
        self.seen: list[Seen] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        with self._lock:
            self.seen.append(
                Seen(
                    method=request.method,
                    keys=request.headers.get_list("idempotency-key"),
                    status=response.status_code,
                    replayed=response.headers.get("idempotent-replayed") == "true",
                    retry_after=response.headers.get("retry-after"),
                )
            )
        return response

    def close(self) -> None:
        self._inner.close()


@pytest.fixture
def counting() -> Iterator[CountingTransport]:
    transport = CountingTransport()
    yield transport
    transport.close()


@pytest.fixture
def client(stub_url: str, counting: CountingTransport) -> Iterator[DocQL]:
    with httpx.Client(transport=counting) as http:
        yield DocQL(api_key=API_KEY, api_url=stub_url, http_client=http)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every retry sleep and really sleep it (the hold knob runs on real time)."""
    recorded: list[float] = []

    def sleep(seconds: float) -> None:
        recorded.append(seconds)
        time.sleep(seconds)

    monkeypatch.setattr(_jobs, "_sleep", sleep)
    return recorded


def query(name: str) -> str:
    return f"What is the total amount? (p26-06-{name}-{RUN})"


def key(name: str) -> str:
    return f"p26-06-{name}-{RUN}"


def test_persist_unknown_is_retried_once_with_the_same_key_and_returns_the_replayed_job(
    client: DocQL, counting: CountingTransport, sleeps: list[float]
) -> None:
    k = key("persist-unknown")
    job = client.submit_document(
        PDF, filename="persist-unknown.pdf", query=query("persist-unknown"), idempotency_key=k
    )
    assert job.status == JOB_STATUS_QUEUED
    assert job.idempotency_key == k
    assert job.job_id
    assert [s.status for s in counting.seen] == [503, 202]
    assert [s.keys for s in counting.seen] == [[k], [k]]
    assert counting.seen[0].retry_after == "5"
    assert counting.seen[1].replayed is True
    assert sleeps == [5]
    # The replayed job is the one DocQL created on the first (503) attempt, so it exists.
    # The kit's stubEnv keeps a job for 5 s (STUB_JOBS_RESULT_TTL_SECONDS) and the retry
    # slept 5 s, so the owner may already see job_expired; job_not_found would mean the
    # replay pointed at a job that never existed.
    try:
        assert client.get_job(job.job_id).job_id == job.job_id
    except DocQLError as exc:
        assert exc.reason == "job_expired", exc


def test_persist_503_raises_the_503_with_the_key_after_four_requests(
    client: DocQL, counting: CountingTransport, sleeps: list[float]
) -> None:
    k = key("persist-503")
    with pytest.raises(DocQLError) as excinfo:
        client.submit_document(
            PDF, filename="persist-503.pdf", query=query("persist-503"), idempotency_key=k
        )
    err = excinfo.value
    assert err.status == 503
    assert err.reason == "upstream_unavailable"
    assert err.idempotency_key == k
    assert err.retry_after == 5
    assert [s.status for s in counting.seen] == [503, 503, 503, 503]
    assert [s.keys for s in counting.seen] == [[k]] * 4
    assert sleeps == [5, 5, 5]


def test_same_key_twice_returns_the_same_job_id_and_the_second_is_replayed(
    client: DocQL, counting: CountingTransport
) -> None:
    k = key("same-key-twice")
    first = client.submit_document(
        PDF, filename="twice.pdf", query=query("same-key-twice"), idempotency_key=k
    )
    second = client.submit_document(
        PDF, filename="twice.pdf", query=query("same-key-twice"), idempotency_key=k
    )
    assert second.job_id == first.job_id
    assert [s.status for s in counting.seen] == [202, 202]
    assert [s.replayed for s in counting.seen] == [False, True]


def test_hold_concurrent_same_key_submits_return_the_same_job_one_after_a_409(
    client: DocQL, counting: CountingTransport, sleeps: list[float]
) -> None:
    k = key("hold-concurrent")
    results: dict[str, Job] = {}
    errors: list[BaseException] = []

    def run(name: str, delay: float) -> None:
        time.sleep(delay)
        try:
            results[name] = client.submit_document(
                PDF, filename="hold-1500.pdf", query=query("hold-concurrent"), idempotency_key=k
            )
        except BaseException as exc:  # surfaced to the main thread below
            errors.append(exc)

    # The kit's j16 starts the second submit 300 ms after the first.
    threads = [
        threading.Thread(target=run, args=("first", 0.0)),
        threading.Thread(target=run, args=("second", 0.3)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert results["first"].job_id == results["second"].job_id
    assert results["first"].idempotency_key == results["second"].idempotency_key == k
    statuses = [s.status for s in counting.seen]
    assert 409 in statuses
    conflicts = [s for s in counting.seen if s.status == 409]
    assert all(s.retry_after == "1" for s in conflicts)
    assert sleeps and all(slept == 1 for slept in sleeps)
    assert all(s.keys == [k] for s in counting.seen)
    # One fresh 202 (the first submit) and one replay (the second, after its 409s).
    accepted = [s for s in counting.seen if s.status == 202]
    assert sorted(s.replayed for s in accepted) == [False, True]


def test_non_replayable_stream_on_persist_unknown_raises_the_503_with_the_key_after_one_request(
    client: DocQL, counting: CountingTransport, sleeps: list[float]
) -> None:
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "wb") as writer:
        writer.write(PDF)
    k = key("non-replayable")
    with os.fdopen(read_fd, "rb") as reader:
        with pytest.raises(DocQLError) as excinfo:
            client.submit_document(
                reader,
                filename="persist-unknown.pdf",
                query=query("non-replayable"),
                idempotency_key=k,
            )
        assert not reader.closed
    assert excinfo.value.status == 503
    assert excinfo.value.idempotency_key == k
    assert [s.status for s in counting.seen] == [503]
    assert sleeps == []

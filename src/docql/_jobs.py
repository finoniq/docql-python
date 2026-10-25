"""Async jobs: the Job model, one-request primitive, the submit retry and the wait (JSDK-01/02).

The wait loop is a copy of the contract kit's reference ``waitForJob``
(docql-console ``apps/docql-stub/test/job-kit.ts``), so every SDK makes the same
requests and reaches the same outcome on the kit's ``waitCases``:

* D-03 (as adjusted to the kit, research F6): each sleep is the answer's
  ``Retry-After`` clamped to [1, 30] s, 1 s when a 200 carries none; a caller
  ``poll_interval`` raises the sleep to ``max(poll_interval, server value)``.
  There is no jitter.
* D-04: a network error, a per-poll timeout, a 503 and a 429 are retried inside
  ``wait()``; without ``Retry-After`` they back off 1, 2, 4, 8, 16, 30 s, reset
  after any 200. A 401, any 404 and every other status raise at once.
* D-05: ``succeeded`` and ``cancelled`` return the Job; ``failed`` raises the
  job's :class:`DocQLError` (``job_id`` set, ``retryable`` False).
* OD-5: the wait gives up when the next sleep would pass the deadline (900 s by
  default) and raises :class:`DocQLWaitTimeoutError`. The wait never cancels the
  job, on any path: cancelling is only the caller's explicit ``cancel_job()``.

Submits (:func:`submit_with_retry`, D-07) carry one ``Idempotency-Key`` and
retry a 503 or a 409 ``idempotency_in_progress`` with that same key, at most 4
attempts, and only when the input can be replayed from its start.

This module does not touch ``query_document``: its send block is copied, not
shared, so the v0.1 request path stays byte-identical (research Pitfall 12).
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, BinaryIO, cast

import httpx

from docql._constants import (
    CONNECT_TIMEOUT,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_SUCCEEDED,
    JOBS_PATH,
    POLL_READ_TIMEOUT,
    REASON_IDEMPOTENCY_IN_PROGRESS,
    RETRY_AFTER_ABSENT,
    RETRY_AFTER_CAP,
    RETRY_AFTER_FLOOR,
    SUBMIT_BACKOFF,
    SUBMIT_MAX_ATTEMPTS,
    TRANSIENT_BACKOFF_CAP,
)
from docql._errors import (
    DocQLConnectionError,
    DocQLError,
    DocQLTimeoutError,
    DocQLWaitTimeoutError,
)
from docql._request import (
    Upload,
    _is_binary_stream,
    _remaining_length,
    _ShortReadError,
    prepare_upload,
)
from docql._response import (
    QueryResult,
    _header,
    _int_or_none,
    _unusable_result,
    decode_error,
    parse_result,
    parse_retry_after,
)
from docql._version import __version__

if TYPE_CHECKING:
    from docql._client import DocQL

# Module-level seams, looked up at call time: tests replace both with a fake
# clock to replay the kit's waitCases without real sleeps.
_sleep = time.sleep
_monotonic = time.monotonic

# Same origin rule as the client: h11/httpcore failures map to a connection error.
_INTERNAL_MODULE_ROOTS = ("h11", "httpcore")

_IDEMPOTENCY_KEY_ERROR = "idempotency_key must be 1-255 printable ASCII characters (0x20-0x7E)"


@dataclass(frozen=True)
class Job:
    """One DocQL job as the API answered it.

    ``status`` is an open string (D-09): compare it with the ``JOB_STATUS_*``
    constants; an unknown value from a newer DocQL release is kept verbatim.
    ``result`` is set on a succeeded job and typed like ``query_document``'s
    answer; ``error`` is set on a failed job. ``request_id`` is the
    ``X-Request-Id`` of the answer that produced this Job. ``retry_after`` is
    the answer's ``Retry-After`` in seconds, for callers who poll
    ``get_job()`` themselves. ``idempotency_key`` is set only on the Job
    ``submit_document()`` returns; ``get_job()`` cannot know it.
    """

    job_id: str
    status: str
    page_count: int | None
    pages_done: int | None
    request_id: str | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    expires_at: datetime | None
    result: QueryResult | None = field(repr=False)
    error: DocQLError | None
    retry_after: int | None
    idempotency_key: str | None = field(repr=False)


def job_path(job_id: str) -> str:
    """The job URL path: the id as one percent-encoded segment, every ``.`` as %2E.

    This is the gateway's own encoder (``job-routes.ts`` encodeJobIdSegment), so
    ``.`` and ``..`` ids are never normalised away. The id is opaque: only an
    empty or non-str id is refused, its format is never checked.
    """
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id must be a non-empty string")
    return JOBS_PATH + "/" + urllib.parse.quote(job_id, safe="").replace(".", "%2E")


def new_idempotency_key() -> str:
    """A fresh Idempotency-Key: 256 bits from the stdlib CSPRNG, URL-safe (D-07)."""
    return secrets.token_urlsafe(32)


def validate_idempotency_key(key: str) -> str:
    """Return ``key`` unchanged when DocQL accepts it, else raise ValueError.

    DocQL's rule: 1-255 bytes, every byte 0x20-0x7E. The message never echoes
    the value, which DocQL and the gateway treat as sensitive (Pitfall 9).
    """
    if not isinstance(key, str):
        raise ValueError(_IDEMPOTENCY_KEY_ERROR)
    raw = key.encode("utf-8")
    if not 1 <= len(raw) <= 255 or any(byte < 0x20 or byte > 0x7E for byte in raw):
        raise ValueError(_IDEMPOTENCY_KEY_ERROR)
    return key


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_job(
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    api_key: str,
    *,
    idempotency_key: str | None = None,
) -> Job:
    """Decode a 2xx job answer (submit 202, poll 200, cancel 200/202) into a Job.

    A body that is not a JSON object with a string ``job_id`` and ``status`` is
    unusable and raises the same :class:`DocQLError` shape ``parse_result``
    uses for an unusable 2xx. A succeeded job's ``result`` decodes through
    ``parse_result``; a failed job's ``error`` through ``decode_error`` with
    the embedded ``status_code``, then ``job_id`` set and ``retryable`` False
    (a failed job is final: retrying it replays the same failure).
    """
    try:
        doc: Any = json.loads(body)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise _unusable_result(status, headers, body, api_key, "unexpected non-JSON response")
    job_id = doc.get("job_id")
    job_status = doc.get("status")
    if not isinstance(job_id, str) or not isinstance(job_status, str):
        raise _unusable_result(
            status, headers, body, api_key, "unexpected response: not a DocQL job object"
        )
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    result: QueryResult | None = None
    raw_result = doc.get("result")
    if job_status == JOB_STATUS_SUCCEEDED and isinstance(raw_result, dict):
        result = parse_result(200, headers, json.dumps(raw_result).encode("utf-8"), api_key)

    error: DocQLError | None = None
    raw_error = doc.get("error")
    if job_status == JOB_STATUS_FAILED and isinstance(raw_error, dict):
        code = raw_error.get("status_code")
        error = decode_error(
            code if type(code) is int else 500,
            {},
            json.dumps(raw_error).encode("utf-8"),
            api_key,
        )
        error.job_id = job_id
        error.retryable = False

    return Job(
        job_id=job_id,
        status=job_status,
        page_count=_int_or_none(metadata.get("page_count")),
        pages_done=_int_or_none(metadata.get("pages_done")),
        request_id=_header(headers, "x-request-id"),
        created_at=_timestamp(doc.get("created_at")),
        started_at=_timestamp(doc.get("started_at")),
        finished_at=_timestamp(doc.get("finished_at")),
        expires_at=_timestamp(doc.get("expires_at")),
        result=result,
        error=error,
        retry_after=parse_retry_after(_header(headers, "retry-after")),
        idempotency_key=idempotency_key,
    )


def _send_once(
    client: DocQL,
    method: str,
    path: str,
    *,
    upload: Upload | None = None,
    idempotency_keys: tuple[str, ...] = (),
    timeout: httpx.Timeout,
) -> tuple[int, httpx.Headers, bytes]:
    """Send exactly one job request and return ``(status, headers, body)``.

    The query_document send block, copied: X-API-Key and User-Agent on every
    request, Content-Type (and Content-Length when known) plus one
    ``Idempotency-Key`` line per given key on an upload; any X-Request-Id or
    ``x-docql-*`` header (an injected client's defaults included) is removed;
    redirects are never followed. It never decodes and never retries.
    """
    # The client's bounded error read, reused as-is (imported here: _client imports this module).
    from docql._client import _read_error_body

    for key in idempotency_keys:
        validate_idempotency_key(key)
    one_key = idempotency_keys[0] if len(idempotency_keys) == 1 else None
    headers: list[tuple[str, str]] = [
        ("X-API-Key", client._api_key),
        (
            "User-Agent",
            f"docql-python/{__version__} "
            f"(python {platform.python_version()}; httpx {httpx.__version__})",
        ),
    ]
    if upload is not None:
        headers.append(("Content-Type", upload.content_type))
        if upload.content_length is not None:
            headers.append(("Content-Length", str(upload.content_length)))
    for key in idempotency_keys:
        headers.append(("Idempotency-Key", key))
    request = client._client.build_request(
        method,
        client._api_url + path,
        content=upload.iter_bytes() if upload is not None else None,
        headers=headers,
        timeout=timeout,
    )
    for header_name in list(request.headers.keys()):
        lowered = header_name.lower()
        if lowered == "x-request-id" or lowered.startswith("x-docql-"):
            del request.headers[header_name]

    def connection_message(exc: Exception) -> str:
        if upload is not None:
            return client._connection_message(exc, upload)
        return client._scrub(f"connection to {client._api_url} failed: {type(exc).__name__}")

    response: httpx.Response | None = None
    body: bytes
    try:
        response = client._client.send(request, follow_redirects=False, stream=True)
        body = response.read() if 200 <= response.status_code < 300 else _read_error_body(response)
    except httpx.TimeoutException as exc:
        raise DocQLTimeoutError(
            client._scrub(f"DocQL request timed out after {timeout}"), idempotency_key=one_key
        ) from exc
    except (_ShortReadError, httpx.RequestError) as exc:
        raise DocQLConnectionError(connection_message(exc), idempotency_key=one_key) from exc
    except Exception as exc:
        if type(exc).__module__.split(".", 1)[0] in _INTERNAL_MODULE_ROOTS:
            raise DocQLConnectionError(connection_message(exc), idempotency_key=one_key) from exc
        raise
    finally:
        if response is not None:
            response.close()
    return response.status_code, response.headers, body


def _job_error(
    status: int, headers: Mapping[str, str], body: bytes, api_key: str, job_id: str
) -> DocQLError:
    error = decode_error(status, headers, body, api_key)
    error.job_id = job_id
    return error


def _is_replayable(file: object) -> bool:
    """True when the submit body can be rebuilt byte for byte from its start (D-07).

    A path (``str`` / ``os.PathLike``) is re-opened and ``bytes`` are kept, so
    both replay. A binary file object replays only when ``seekable()`` is True
    and ``tell()``/``seek()`` work (v0.1's ``_remaining_length`` measures it):
    the retry rewinds it to the position recorded before the first attempt.
    Anything else (a pipe, a socket, ``sys.stdin.buffer``, an HTTP response
    stream) cannot be replayed, so its submit is never retried.
    """
    if isinstance(file, (str, os.PathLike, bytes)):
        return True
    if not _is_binary_stream(file):
        return False
    seekable = getattr(file, "seekable", None)
    if not callable(seekable):
        return False
    try:
        if not seekable():
            return False
    except (OSError, ValueError):
        return False
    return _remaining_length(cast("BinaryIO", file)) is not None


def submit_with_retry(
    client: DocQL,
    file: str | os.PathLike[str] | bytes | BinaryIO,
    *,
    query: str | None,
    prompt: str | None,
    mode: str | None,
    filename: str | None,
    idempotency_key: str | None,
) -> Job:
    """Submit a job with one Idempotency-Key and a bounded same-key retry (JSDK-02, D-07, D-08).

    DocQL's handoff (Revision 2026-10-21) says a submit 503 may already have
    created the job: "treat every submit 503 as 'a job may exist' and retry
    with the same Idempotency-Key". So, with the same key on every attempt and
    at most ``SUBMIT_MAX_ATTEMPTS`` attempts:

    * any 503, and a 409 ``idempotency_in_progress`` (another submit with this
      key is still being persisted), is retried when the input is replayable
      (:func:`_is_replayable`); each sleep is ``Retry-After`` clamped to
      [1, 30] s, else 1, 2, 4 s (``SUBMIT_BACKOFF``);
    * a 429 (``queue_full``, ``rate_limit_exceeded``,
      ``concurrency_limit_exceeded``: no job exists), every other status, a
      transport error and a timeout are raised at once;
    * a key is never regenerated for a retry (a new key could create a second
      billable job), and a stream is only ever rewound to its recorded start.

    Every error raised here carries ``idempotency_key`` so the caller can retry
    safely with it. Go (``SubmitDocument``) and Java (``submitDocument``) copy
    this loop: same cap, same backoff, same retryable set.
    """
    key = (
        validate_idempotency_key(idempotency_key)
        if idempotency_key is not None
        else new_idempotency_key()
    )
    replayable = _is_replayable(file)
    stream: BinaryIO | None = None
    start = 0
    if replayable and not isinstance(file, (str, os.PathLike, bytes)):
        stream = file
        start = stream.tell()
    attempt = 0
    while True:
        attempt += 1
        if attempt > 1 and stream is not None:
            stream.seek(start)
        with prepare_upload(
            file, query=query, prompt=prompt, mode=mode, filename=filename
        ) as upload:
            status, headers, body = _send_once(
                client,
                "POST",
                JOBS_PATH,
                upload=upload,
                idempotency_keys=(key,),
                timeout=client._timeout,
            )
        if 200 <= status < 300:
            try:
                return parse_job(status, headers, body, client._api_key, idempotency_key=key)
            except DocQLError as unusable:
                unusable.idempotency_key = key
                raise
        error = decode_error(status, headers, body, client._api_key)
        error.idempotency_key = key
        retryable = status == 503 or (
            status == 409 and error.reason == REASON_IDEMPOTENCY_IN_PROGRESS
        )
        if not (retryable and replayable and attempt < SUBMIT_MAX_ATTEMPTS):
            raise error
        server = parse_retry_after(_header(headers, "retry-after"))
        if server is not None:
            _sleep(min(max(server, RETRY_AFTER_FLOOR), RETRY_AFTER_CAP))
        else:
            _sleep(SUBMIT_BACKOFF[attempt - 1])


def fetch_job(client: DocQL, job_id: str) -> Job:
    """One GET of the job: a Job for any 2xx (a failed job included), else DocQLError."""
    path = job_path(job_id)
    status, headers, body = _send_once(
        client, "GET", path, timeout=httpx.Timeout(POLL_READ_TIMEOUT, connect=CONNECT_TIMEOUT)
    )
    if 200 <= status < 300:
        return parse_job(status, headers, body, client._api_key)
    raise _job_error(status, headers, body, client._api_key, job_id)


def cancel(client: DocQL, job_id: str) -> Job:
    """One DELETE of the job (OD-10): the Job as answered, else DocQLError with job_id.

    A 202 answers the job as it is right after the cancel request; a 200 answers
    a job that was already terminal. This is the only DELETE in the SDK, and only
    the caller's explicit ``cancel_job()`` reaches it.
    """
    path = job_path(job_id)
    status, headers, body = _send_once(
        client, "DELETE", path, timeout=httpx.Timeout(POLL_READ_TIMEOUT, connect=CONNECT_TIMEOUT)
    )
    if 200 <= status < 300:
        return parse_job(status, headers, body, client._api_key)
    raise _job_error(status, headers, body, client._api_key, job_id)


def wait_for(client: DocQL, job_id: str, *, timeout: float, poll_interval: float | None) -> Job:
    """Poll the job until it is succeeded or cancelled; the kit's waitForJob (module docstring)."""
    path = job_path(job_id)
    if not timeout > 0:
        raise ValueError("timeout must be greater than 0 seconds")
    if poll_interval is not None and not poll_interval > 0:
        raise ValueError("poll_interval must be greater than 0 seconds")
    deadline = _monotonic() + timeout
    backoff = 1
    while True:
        remaining = max(deadline - _monotonic(), 1.0)
        poll_timeout = httpx.Timeout(
            min(POLL_READ_TIMEOUT, remaining), connect=min(CONNECT_TIMEOUT, remaining)
        )
        server: int | None
        try:
            status, headers, body = _send_once(client, "GET", path, timeout=poll_timeout)
        except DocQLConnectionError:
            # A network error or a per-poll timeout: transient.
            transient = True
            server = None
        else:
            if 200 <= status < 300:
                job = parse_job(status, headers, body, client._api_key)
                if job.status in (JOB_STATUS_SUCCEEDED, JOB_STATUS_CANCELLED):
                    return job
                if job.status == JOB_STATUS_FAILED:
                    if job.error is not None:
                        raise job.error
                    raise _unusable_result(
                        status,
                        headers,
                        body,
                        client._api_key,
                        "unexpected response: failed job without an error object",
                    )
                # queued, running or a status this SDK does not know (D-09).
                transient = False
                backoff = 1
            elif status in (429, 503):
                transient = True
            else:
                raise _job_error(status, headers, body, client._api_key, job_id)
            server = parse_retry_after(_header(headers, "retry-after"))

        sleep: float
        if server is not None:
            sleep = min(max(server, RETRY_AFTER_FLOOR), RETRY_AFTER_CAP)
        elif transient:
            sleep = backoff
            backoff = min(backoff * 2, TRANSIENT_BACKOFF_CAP)
        else:
            sleep = RETRY_AFTER_ABSENT
        if poll_interval is not None:
            sleep = max(poll_interval, sleep)
        if _monotonic() + sleep > deadline:
            raise DocQLWaitTimeoutError(job_id=job_id, timeout=timeout)
        _sleep(sleep)

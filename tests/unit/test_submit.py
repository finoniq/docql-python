"""The bounded same-key submit retry and the Idempotency-Key rules (JSDK-02, D-07, D-08).

Every test answers through ``httpx.MockTransport`` and replaces ``docql._jobs._sleep``
with a recorder, so the retry pacing is asserted exactly and nothing really sleeps.
"""

import io
import json
import os
import string
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from docql import (
    JOB_STATUS_QUEUED,
    DocQL,
    DocQLConnectionError,
    DocQLError,
    DocQLTimeoutError,
    _jobs,
)
from docql._constants import SUBMIT_BACKOFF, SUBMIT_MAX_ATTEMPTS

ROOT = Path(__file__).resolve().parents[2]
DECODE_CASES: dict[str, dict[str, Any]] = {
    case["id"]: case
    for case in json.loads((ROOT / "contract" / "job-cases.json").read_bytes())["decodeCases"]
}
PDF = b"%PDF-1.4 submit unit test bytes\n"
URL_SAFE = set(string.ascii_letters + string.digits + "-_")


@pytest.fixture(autouse=True)
def _clean_docql_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCQL_API_KEY", raising=False)
    monkeypatch.delenv("DOCQL_API_URL", raising=False)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(_jobs, "_sleep", recorded.append)
    return recorded


Answer = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """Serve answers in order (the last one repeats) and record each request and its body."""

    def __init__(self, *answers: Answer) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.read())
        self.requests.append(request)
        index = min(len(self.requests), len(self.answers)) - 1
        return self.answers[index](request)


def kit(case_id: str) -> Answer:
    case = DECODE_CASES[case_id]
    return lambda _request: httpx.Response(
        case["status"], headers=case.get("headers") or {}, content=case["bodyUtf8"].encode()
    )


def envelope(status: int, reason: str, headers: dict[str, str] | None = None) -> Answer:
    body = json.dumps(
        {
            "status_code": status,
            "message": f"unit {reason}",
            "metadata": {"reason": reason, "request_id": "req_unit"},
        }
    ).encode()
    return lambda _request: httpx.Response(status, headers=headers or {}, content=body)


def raises(exc: type[httpx.RequestError]) -> Answer:
    def answer(request: httpx.Request) -> httpx.Response:
        raise exc("unit transport failure", request=request)

    return answer


QUEUED = kit("job-202-queued")
UNAVAILABLE = envelope(503, "upstream_unavailable", {"Retry-After": "5"})
UNAVAILABLE_BARE = envelope(503, "upstream_unavailable")
IN_PROGRESS = envelope(409, "idempotency_in_progress", {"Retry-After": "1"})


def client_for(recorder: Recorder) -> DocQL:
    return DocQL(
        api_key="unit-test-key",
        api_url="http://stub.test",
        http_client=httpx.Client(transport=httpx.MockTransport(recorder)),
    )


def submit(recorder: Recorder, file: Any = PDF, **kwargs: Any) -> Any:
    kwargs.setdefault("filename", "f.pdf")
    kwargs.setdefault("query", "q")
    return client_for(recorder).submit_document(file, **kwargs)


def keys_sent(recorder: Recorder) -> list[list[str]]:
    return [request.headers.get_list("idempotency-key") for request in recorder.requests]


def test_constants_are_the_d07_numbers() -> None:
    assert SUBMIT_MAX_ATTEMPTS == 4
    assert SUBMIT_BACKOFF == (1, 2, 4)


# --- retry rule -------------------------------------------------------------------------


def test_503_then_202_returns_the_job_with_the_same_key_on_both_attempts(
    sleeps: list[float],
) -> None:
    recorder = Recorder(UNAVAILABLE, QUEUED)
    job = submit(recorder, idempotency_key="order-1")
    assert job.status == JOB_STATUS_QUEUED
    assert job.idempotency_key == "order-1"
    assert keys_sent(recorder) == [["order-1"], ["order-1"]]
    assert sleeps == [5]


def test_503_four_times_raises_the_fourth_with_the_key(sleeps: list[float]) -> None:
    recorder = Recorder(UNAVAILABLE)
    with pytest.raises(DocQLError) as excinfo:
        submit(recorder, idempotency_key="order-2")
    assert excinfo.value.status == 503
    assert excinfo.value.idempotency_key == "order-2"
    assert len(recorder.requests) == SUBMIT_MAX_ATTEMPTS == 4
    assert keys_sent(recorder) == [["order-2"]] * 4
    assert sleeps == [5, 5, 5]


def test_503_without_retry_after_backs_off_1_2_4(sleeps: list[float]) -> None:
    recorder = Recorder(UNAVAILABLE_BARE)
    with pytest.raises(DocQLError) as excinfo:
        submit(recorder)
    assert excinfo.value.status == 503
    assert len(recorder.requests) == 4
    assert sleeps == [1, 2, 4]


def test_any_503_is_retried_whatever_its_reason(sleeps: list[float]) -> None:
    recorder = Recorder(envelope(503, "service_overloaded"), QUEUED)
    submit(recorder)
    assert len(recorder.requests) == 2
    assert sleeps == [1]


@pytest.mark.parametrize(
    ("retry_after", "slept"), [("0", 1), ("1", 1), ("7", 7), ("30", 30), ("3600", 30)]
)
def test_retry_after_is_clamped_to_1_and_30(
    sleeps: list[float], retry_after: str, slept: int
) -> None:
    recorder = Recorder(envelope(503, "upstream_unavailable", {"Retry-After": retry_after}), QUEUED)
    submit(recorder)
    assert sleeps == [slept]


def test_409_in_progress_then_202_is_retried_after_retry_after_1(sleeps: list[float]) -> None:
    recorder = Recorder(IN_PROGRESS, QUEUED)
    job = submit(recorder, idempotency_key="order-3")
    assert job.idempotency_key == "order-3"
    assert keys_sent(recorder) == [["order-3"], ["order-3"]]
    assert sleeps == [1]


def test_409_key_conflict_is_raised_after_one_request(sleeps: list[float]) -> None:
    recorder = Recorder(envelope(422, "idempotency_key_conflict"), QUEUED)
    with pytest.raises(DocQLError) as excinfo:
        submit(recorder, idempotency_key="order-4")
    assert excinfo.value.reason == "idempotency_key_conflict"
    assert excinfo.value.idempotency_key == "order-4"
    assert len(recorder.requests) == 1
    assert sleeps == []


def test_a_409_with_another_reason_is_not_retried(sleeps: list[float]) -> None:
    recorder = Recorder(envelope(409, "idempotency_key_conflict"), QUEUED)
    with pytest.raises(DocQLError):
        submit(recorder)
    assert len(recorder.requests) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "answer",
    [
        kit("job-429-queue-full-tenant"),
        envelope(429, "rate_limit_exceeded", {"Retry-After": "1"}),
        envelope(429, "concurrency_limit_exceeded", {"Retry-After": "1"}),
        envelope(400, "invalid_params"),
        envelope(401, "invalid_api_key"),
        envelope(413, "upload_too_large"),
        envelope(500, "internal_error"),
        envelope(502, "bad_gateway"),
    ],
    ids=["429-queue-full", "429-rate", "429-concurrency", "400", "401", "413", "500", "502"],
)
def test_other_errors_are_raised_after_one_request_with_the_key(
    sleeps: list[float], answer: Answer
) -> None:
    recorder = Recorder(answer, QUEUED)
    with pytest.raises(DocQLError) as excinfo:
        submit(recorder, idempotency_key="order-5")
    assert excinfo.value.idempotency_key == "order-5"
    assert len(recorder.requests) == 1
    assert sleeps == []


def test_a_connection_error_is_raised_with_the_key_after_one_request(
    sleeps: list[float],
) -> None:
    recorder = Recorder(raises(httpx.ConnectError), QUEUED)
    with pytest.raises(DocQLConnectionError) as excinfo:
        submit(recorder, idempotency_key="order-6")
    assert excinfo.value.idempotency_key == "order-6"
    assert len(recorder.requests) == 1
    assert sleeps == []


def test_a_read_timeout_is_raised_with_the_key_after_one_request(sleeps: list[float]) -> None:
    recorder = Recorder(raises(httpx.ReadTimeout), QUEUED)
    with pytest.raises(DocQLTimeoutError) as excinfo:
        submit(recorder, idempotency_key="order-7")
    assert excinfo.value.idempotency_key == "order-7"
    assert len(recorder.requests) == 1
    assert sleeps == []


def test_a_connection_error_after_a_503_is_raised_with_the_same_key(sleeps: list[float]) -> None:
    recorder = Recorder(UNAVAILABLE, raises(httpx.ConnectError))
    with pytest.raises(DocQLConnectionError) as excinfo:
        submit(recorder, idempotency_key="order-8")
    assert excinfo.value.idempotency_key == "order-8"
    assert keys_sent(recorder) == [["order-8"], ["order-8"]]


def test_an_unusable_2xx_carries_the_key() -> None:
    recorder = Recorder(lambda _request: httpx.Response(202, content=b"<html>"))
    with pytest.raises(DocQLError) as excinfo:
        submit(recorder, idempotency_key="order-9")
    assert excinfo.value.idempotency_key == "order-9"
    assert len(recorder.requests) == 1


# --- replayable input ---------------------------------------------------------------------


def test_bytes_are_replayed_byte_for_byte_with_a_fresh_boundary(sleeps: list[float]) -> None:
    recorder = Recorder(UNAVAILABLE, QUEUED)
    submit(recorder, PDF)
    first, second = recorder.bodies
    assert PDF in first
    assert PDF in second
    assert len(first) == len(second)


def test_a_path_is_reopened_for_the_retry(sleeps: list[float], tmp_path: Path) -> None:
    path = tmp_path / "doc.pdf"
    path.write_bytes(PDF)
    recorder = Recorder(UNAVAILABLE, QUEUED)
    submit(recorder, str(path), filename=None)
    assert len(recorder.requests) == 2
    assert all(PDF in body for body in recorder.bodies)
    recorder_pathlike = Recorder(UNAVAILABLE, QUEUED)
    submit(recorder_pathlike, path, filename=None)
    assert len(recorder_pathlike.requests) == 2
    assert recorder_pathlike.bodies[0] != b""


def test_a_stream_is_rewound_to_its_recorded_start_and_never_closed(
    sleeps: list[float],
) -> None:
    stream = io.BytesIO(b"0123456" + PDF)
    stream.seek(7)
    recorder = Recorder(UNAVAILABLE, QUEUED)
    submit(recorder, stream)
    first, second = recorder.bodies
    assert len(recorder.requests) == 2
    # Same body length means the second attempt started at 7 too (the boundary differs).
    assert len(first) == len(second)
    assert PDF in second
    assert b"0123456" not in first
    assert b"0123456" not in second
    assert not stream.closed
    # The stream ends where a single successful read would leave it.
    assert stream.tell() == len(b"0123456" + PDF)


def test_a_successful_stream_submit_is_never_rewound() -> None:
    stream = io.BytesIO(b"xx" + PDF)
    stream.seek(2)
    seeks: list[tuple[int, int]] = []
    original = stream.seek

    def tracking_seek(offset: int, whence: int = os.SEEK_SET) -> int:
        seeks.append((offset, whence))
        return original(offset, whence)

    stream.seek = tracking_seek  # type: ignore[method-assign]
    submit(Recorder(QUEUED), stream)
    # Only v0.1's own measurement moves it (to the end and back), never to the start.
    assert (2, os.SEEK_SET) in seeks
    assert all(offset != 0 or whence != os.SEEK_SET for offset, whence in seeks)
    assert not stream.closed


@pytest.fixture
def pipe_reader() -> Iterator[Any]:
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "wb") as writer:
        writer.write(PDF)
    reader = os.fdopen(read_fd, "rb")
    try:
        yield reader
    finally:
        reader.close()


def test_a_non_seekable_stream_is_sent_once_and_the_503_carries_the_key(
    sleeps: list[float], pipe_reader: Any
) -> None:
    recorder = Recorder(UNAVAILABLE, QUEUED)
    with pytest.raises(DocQLError) as excinfo:
        submit(recorder, pipe_reader, idempotency_key="order-10")
    assert excinfo.value.status == 503
    assert excinfo.value.idempotency_key == "order-10"
    assert len(recorder.requests) == 1
    assert sleeps == []
    assert not pipe_reader.closed


def test_a_non_seekable_stream_gets_a_409_in_progress_raised_once(
    sleeps: list[float], pipe_reader: Any
) -> None:
    recorder = Recorder(IN_PROGRESS, QUEUED)
    with pytest.raises(DocQLError) as excinfo:
        submit(recorder, pipe_reader)
    assert excinfo.value.reason == "idempotency_in_progress"
    assert excinfo.value.idempotency_key
    assert len(recorder.requests) == 1


class _TellBroken(io.BytesIO):
    def tell(self) -> int:
        raise OSError("tell not supported")


class _NotSeekable(io.BytesIO):
    def seekable(self) -> bool:
        return False


@pytest.mark.parametrize("stream_type", [_TellBroken, _NotSeekable])
def test_a_stream_whose_position_cannot_be_recorded_is_not_retried(
    sleeps: list[float], stream_type: type[io.BytesIO]
) -> None:
    recorder = Recorder(UNAVAILABLE, QUEUED)
    with pytest.raises(DocQLError):
        submit(recorder, stream_type(PDF))
    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        ("doc.pdf", True),
        (Path("doc.pdf"), True),
        (PDF, True),
        (io.BytesIO(PDF), True),
        (_NotSeekable(PDF), False),
        (_TellBroken(PDF), False),
        (io.StringIO("text"), False),
        (42, False),
    ],
    ids=["str", "pathlike", "bytes", "bytesio", "not-seekable", "tell-broken", "text", "int"],
)
def test_is_replayable(file: Any, expected: bool) -> None:
    assert _jobs._is_replayable(file) is expected


# --- key rules ----------------------------------------------------------------------------


def test_no_key_generates_one_43_char_url_safe_key_reused_on_every_attempt(
    sleeps: list[float],
) -> None:
    recorder = Recorder(UNAVAILABLE, IN_PROGRESS, QUEUED)
    job = submit(recorder)
    sent = keys_sent(recorder)
    assert len(sent) == 3
    key = sent[0][0]
    assert sent == [[key], [key], [key]]
    assert len(key) == 43
    assert set(key) <= URL_SAFE
    assert job.idempotency_key == key


def test_each_call_without_a_key_gets_a_new_key() -> None:
    recorder = Recorder(QUEUED)
    first = submit(recorder).idempotency_key
    second = submit(recorder).idempotency_key
    assert first != second


@pytest.mark.parametrize(
    "key",
    ["k" * 255, " ", "~", " leading and trailing ", "a~b c", "".join(map(chr, range(0x20, 0x7F)))],
    ids=["255-bytes", "0x20", "0x7E", "spaces", "mixed", "every-printable"],
)
def test_keys_at_the_edges_are_sent_unchanged(key: str) -> None:
    recorder = Recorder(QUEUED)
    job = submit(recorder, idempotency_key=key)
    assert keys_sent(recorder) == [[key]]
    assert job.idempotency_key == key


@pytest.mark.parametrize(
    "key",
    ["", "k" * 256, "a\x1fb", "a\x7fb", "café", "ก", "a\nb", "a\tb"],
    ids=["empty", "256-bytes", "0x1F", "0x7F", "e-acute", "thai", "newline", "tab"],
)
def test_bad_keys_raise_value_error_before_any_io_without_echoing(key: str) -> None:
    recorder = Recorder(QUEUED)
    with pytest.raises(ValueError) as excinfo:
        submit(recorder, idempotency_key=key)
    assert recorder.requests == []
    if key:
        assert key not in str(excinfo.value)
        assert key not in repr(excinfo.value)


def test_a_bad_key_is_refused_before_a_path_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("docql._request.open", lambda path: opened.append(path))
    with pytest.raises(ValueError):
        submit(Recorder(QUEUED), str(tmp_path / "x.pdf"), idempotency_key="\x00")
    assert opened == []


def test_the_key_is_never_in_an_error_message(sleeps: list[float]) -> None:
    recorder = Recorder(UNAVAILABLE)
    with pytest.raises(DocQLError) as excinfo:
        submit(recorder, idempotency_key="secret-order-key")
    assert "secret-order-key" not in str(excinfo.value)
    assert "secret-order-key" not in repr(excinfo.value)


def test_one_idempotency_key_line_per_request(sleeps: list[float]) -> None:
    recorder = Recorder(UNAVAILABLE, QUEUED)
    submit(recorder, idempotency_key="one-line")
    for request in recorder.requests:
        lines = [value for name, value in request.headers.raw if name.lower() == b"idempotency-key"]
        assert lines == [b"one-line"]

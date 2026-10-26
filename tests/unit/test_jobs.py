"""get_job / cancel_job, header hygiene, redirects and job-id encoding (T-E2, OD-10, D-09)."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from docql import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_SUCCEEDED,
    DocQL,
    DocQLError,
    Job,
)
from docql._jobs import job_path

ROOT = Path(__file__).resolve().parents[2]
DECODE_CASES: dict[str, dict[str, Any]] = {
    case["id"]: case
    for case in json.loads((ROOT / "contract" / "job-cases.json").read_bytes())["decodeCases"]
}
JOB_ID = "job_0123456789ABCDEFGHIJKL"


@pytest.fixture(autouse=True)
def _clean_docql_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCQL_API_KEY", raising=False)
    monkeypatch.delenv("DOCQL_API_URL", raising=False)


class Recorder:
    """A MockTransport handler serving answers in order and recording each request."""

    def __init__(self, *answers: Callable[[], httpx.Response]) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        index = min(len(self.requests), len(self.answers)) - 1
        return self.answers[index]()


def kit_answer(case_id: str) -> Callable[[], httpx.Response]:
    case = DECODE_CASES[case_id]
    return lambda: httpx.Response(
        case["status"], headers=case.get("headers") or {}, content=case["bodyUtf8"].encode()
    )


def answer(
    status: int, body: Any, headers: dict[str, str] | None = None
) -> Callable[[], httpx.Response]:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return lambda: httpx.Response(status, headers=headers or {}, content=content)


def client_for(recorder: Recorder, **client_kwargs: Any) -> DocQL:
    return DocQL(
        api_key="unit-test-key",
        api_url="http://stub.test",
        http_client=httpx.Client(transport=httpx.MockTransport(recorder), **client_kwargs),
    )


def test_get_job_on_a_failed_job_returns_it_without_raising() -> None:
    recorder = Recorder(kit_answer("job-200-failed-upstream"))
    job = client_for(recorder).get_job(JOB_ID)
    assert job.status == JOB_STATUS_FAILED
    assert isinstance(job.error, DocQLError)
    assert job.error.reason == "upstream_unavailable"
    assert job.error.job_id == JOB_ID
    assert job.error.retryable is False
    assert len(recorder.requests) == 1
    assert recorder.requests[0].method == "GET"
    assert recorder.requests[0].url.path == "/v1/jobs/" + JOB_ID


def test_get_job_404_raises_with_job_id_after_one_request() -> None:
    recorder = Recorder(kit_answer("job-404-not-found"))
    with pytest.raises(DocQLError) as excinfo:
        client_for(recorder).get_job(JOB_ID)
    assert excinfo.value.status == 404
    assert excinfo.value.reason == "job_not_found"
    assert excinfo.value.job_id == JOB_ID
    assert len(recorder.requests) == 1


def test_get_job_503_raises_after_one_request() -> None:
    recorder = Recorder(kit_answer("job-503-poll"), kit_answer("job-200-succeeded"))
    with pytest.raises(DocQLError) as excinfo:
        client_for(recorder).get_job(JOB_ID)
    assert excinfo.value.status == 503
    assert excinfo.value.retryable is True
    assert excinfo.value.job_id == JOB_ID
    assert len(recorder.requests) == 1


def test_cancel_job_202_returns_the_cancelled_job_after_one_delete() -> None:
    recorder = Recorder(kit_answer("job-delete-202-cancelled"))
    job = client_for(recorder).cancel_job(JOB_ID)
    assert job.status == JOB_STATUS_CANCELLED
    assert job.job_id == JOB_ID
    assert [r.method for r in recorder.requests] == ["DELETE"]
    assert recorder.requests[0].url.path == "/v1/jobs/" + JOB_ID


def test_cancel_job_on_a_terminal_job_returns_it_and_is_repeatable() -> None:
    recorder = Recorder(kit_answer("job-delete-200-terminal"))
    client = client_for(recorder)
    first = client.cancel_job(JOB_ID)
    second = client.cancel_job(JOB_ID)
    assert first.status == JOB_STATUS_SUCCEEDED
    assert first.result is not None
    assert second == first
    assert [r.method for r in recorder.requests] == ["DELETE", "DELETE"]


def test_cancel_job_404_raises_with_job_id() -> None:
    recorder = Recorder(kit_answer("job-404-expired"))
    with pytest.raises(DocQLError) as excinfo:
        client_for(recorder).cancel_job(JOB_ID)
    assert excinfo.value.reason == "job_expired"
    assert excinfo.value.job_id == JOB_ID
    assert len(recorder.requests) == 1


def test_no_forbidden_header_leaves_on_submit_poll_or_cancel() -> None:
    recorder = Recorder(
        kit_answer("job-202-queued"),
        kit_answer("job-200-running"),
        kit_answer("job-delete-202-cancelled"),
    )
    client = client_for(
        recorder, headers={"X-Request-Id": "spoofed", "X-DocQL-Tenant": "other-org"}
    )
    client.submit_document(b"%PDF-1.4 unit", filename="f.pdf", query="q")
    client.get_job(JOB_ID)
    client.cancel_job(JOB_ID)
    submit, poll, cancel = recorder.requests
    for request in recorder.requests:
        names = {name.lower() for name in request.headers}
        assert "x-request-id" not in names
        assert not any(name.startswith("x-docql-") for name in names)
        assert request.headers["x-api-key"] == "unit-test-key"
        assert request.headers["user-agent"].startswith("docql-python/")
    assert len(submit.headers.get_list("idempotency-key")) == 1
    assert submit.headers["content-type"].startswith("multipart/form-data; boundary=")
    for request in (poll, cancel):
        assert "idempotency-key" not in request.headers
        assert "content-type" not in request.headers


def test_submit_returns_the_job_with_the_key_it_sent() -> None:
    recorder = Recorder(kit_answer("job-202-queued"))
    job = client_for(recorder).submit_document(b"%PDF-1.4 unit", filename="f.pdf", query="q")
    sent = recorder.requests[0].headers["idempotency-key"]
    assert job.idempotency_key == sent
    assert len(sent) >= 43
    assert recorder.requests[0].method == "POST"
    assert recorder.requests[0].url.path == "/v1/jobs"


def test_submit_sends_a_caller_key_unchanged_and_puts_it_on_errors() -> None:
    recorder = Recorder(kit_answer("job-429-queue-full-tenant"))
    with pytest.raises(DocQLError) as excinfo:
        client_for(recorder).submit_document(
            b"%PDF-1.4 unit", filename="f.pdf", query="q", idempotency_key="order-42 retry"
        )
    assert recorder.requests[0].headers["idempotency-key"] == "order-42 retry"
    assert excinfo.value.idempotency_key == "order-42 retry"
    assert "order-42" not in str(excinfo.value)
    assert "order-42" not in repr(excinfo.value)


@pytest.mark.parametrize("key", ["", "kกey", "tab\there", "x" * 256])
def test_submit_refuses_a_bad_key_before_sending(key: str) -> None:
    recorder = Recorder(kit_answer("job-202-queued"))
    with pytest.raises(ValueError) as excinfo:
        client_for(recorder).submit_document(
            b"%PDF-1.4 unit", filename="f.pdf", query="q", idempotency_key=key
        )
    assert recorder.requests == []
    if key:
        assert key not in str(excinfo.value)


def test_a_307_is_decoded_as_an_error_and_never_followed() -> None:
    recorder = Recorder(
        answer(307, b"", {"location": "http://elsewhere.test/v1/jobs/x"}),
        kit_answer("job-200-succeeded"),
    )
    with pytest.raises(DocQLError) as excinfo:
        client_for(recorder).get_job(JOB_ID)
    assert excinfo.value.status == 307
    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    ("job_id", "path"),
    [
        ("a.b", "/v1/jobs/a%2Eb"),
        ("..", "/v1/jobs/%2E%2E"),
        ("a/b", "/v1/jobs/a%2Fb"),
        ("é", "/v1/jobs/%C3%A9"),
        (JOB_ID, "/v1/jobs/" + JOB_ID),
    ],
)
def test_job_path_is_one_encoded_segment(job_id: str, path: str) -> None:
    assert job_path(job_id) == path


def test_dotted_job_id_reaches_the_wire_encoded() -> None:
    recorder = Recorder(kit_answer("job-200-running"))
    client_for(recorder).get_job("..")
    assert recorder.requests[0].url.raw_path == b"/v1/jobs/%2E%2E"


@pytest.mark.parametrize("method", ["get_job", "wait", "cancel_job"])
def test_empty_job_id_raises_before_any_request(method: str) -> None:
    recorder = Recorder(kit_answer("job-200-running"))
    with pytest.raises(ValueError, match="job_id must be a non-empty string"):
        getattr(client_for(recorder), method)("")
    assert recorder.requests == []


def test_unknown_status_is_kept_verbatim() -> None:
    recorder = Recorder(answer(200, {"job_id": JOB_ID, "status": "paused", "metadata": {}}))
    job = client_for(recorder).get_job(JOB_ID)
    assert job.status == "paused"
    assert job.result is None
    assert job.error is None


@pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"job_id": 7, "status": "queued"}'])
def test_unusable_2xx_job_body_raises(body: bytes) -> None:
    recorder = Recorder(answer(200, body))
    with pytest.raises(DocQLError) as excinfo:
        client_for(recorder).get_job(JOB_ID)
    assert excinfo.value.reason is None
    assert excinfo.value.status == 200


def test_job_repr_never_shows_the_key_or_the_result() -> None:
    recorder = Recorder(kit_answer("job-202-queued"))
    job = client_for(recorder).submit_document(
        b"%PDF-1.4 unit", filename="f.pdf", query="q", idempotency_key="very-secret-key"
    )
    assert job.idempotency_key == "very-secret-key"
    assert "very-secret-key" not in repr(job)
    assert isinstance(job, Job)
    done = client_for(Recorder(kit_answer("job-200-succeeded"))).get_job(JOB_ID)
    assert "result=" not in repr(done)


# --- every DocQLError from a job-id method carries the caller's job id (WR-02) -----------
# Go (jobAnswer/withJobID) and Java (jobOrThrow/withJob) set it on an unusable 2xx and on a
# failed job without an error object too; code that resumes or logs by err.job_id needs it.


def test_get_job_unusable_2xx_carries_the_job_id() -> None:
    client = client_for(Recorder(answer(200, b"<html>"), kit_answer("job-404-not-found")))
    with pytest.raises(DocQLError) as unusable:
        client.get_job("j1")
    assert unusable.value.status == 200
    assert unusable.value.job_id == "j1"
    # The non-2xx answer from the same method names the same job.
    with pytest.raises(DocQLError) as not_found:
        client.get_job("j1")
    assert not_found.value.status == 404
    assert not_found.value.job_id == unusable.value.job_id


def test_cancel_job_unusable_2xx_carries_the_job_id() -> None:
    recorder = Recorder(answer(202, {"status": "running", "metadata": {}}))
    with pytest.raises(DocQLError) as excinfo:
        client_for(recorder).cancel_job("j1")
    assert excinfo.value.status == 202
    assert excinfo.value.job_id == "j1"
    assert [r.method for r in recorder.requests] == ["DELETE"]


def test_wait_unusable_2xx_carries_the_job_id() -> None:
    recorder = Recorder(answer(200, b"not json"))
    with pytest.raises(DocQLError) as excinfo:
        client_for(recorder).wait("j1")
    assert excinfo.value.status == 200
    assert excinfo.value.job_id == "j1"
    assert len(recorder.requests) == 1


def test_wait_failed_job_without_error_object_carries_the_job_id() -> None:
    recorder = Recorder(answer(200, {"job_id": "j1", "status": "failed"}))
    with pytest.raises(DocQLError) as excinfo:
        client_for(recorder).wait("j1")
    assert excinfo.value.job_id == "j1"
    assert excinfo.value.retryable is False
    assert len(recorder.requests) == 1


def test_get_job_succeeded_with_unusable_result_carries_the_job_id() -> None:
    recorder = Recorder(
        answer(200, {"job_id": "j1", "status": "succeeded", "result": {"data": [1]}})
    )
    with pytest.raises(DocQLError) as excinfo:
        client_for(recorder).get_job("j1")
    assert excinfo.value.job_id == "j1"


def test_get_job_twice_on_an_unusable_answer_carries_the_job_id_both_times() -> None:
    client = client_for(Recorder(answer(200, b"[]")))
    ids = []
    for _ in range(2):
        with pytest.raises(DocQLError) as excinfo:
            client.get_job("j1")
        ids.append(excinfo.value.job_id)
    assert ids == ["j1", "j1"]

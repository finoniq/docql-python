"""wait() replays the kit's waitCases W1-W7 offline on a fake clock (D-03, D-04, D-05, OD-5).

The scripted answers come from the vendored kit (``job_cases["waitCases"]``),
never re-typed. ``docql._jobs._sleep`` and ``docql._jobs._monotonic`` are
replaced with a fake clock, and an httpx.MockTransport serves the answers and
records every request, so a DELETE or an extra poll would show up here.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from docql import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_SUCCEEDED,
    DocQL,
    DocQLError,
    DocQLWaitTimeoutError,
    Job,
    _jobs,
    _response,
)

ROOT = Path(__file__).resolve().parents[2]
KIT: dict[str, Any] = json.loads((ROOT / "contract" / "job-cases.json").read_bytes())
WAIT_CASES: list[dict[str, Any]] = KIT["waitCases"]
DECODE_CASES: dict[str, dict[str, Any]] = {c["id"]: c for c in KIT["decodeCases"]}
W: dict[str, dict[str, Any]] = {c["id"]: c for c in WAIT_CASES}
JOB_ID = "job_0123456789ABCDEFGHIJKL"
JOB_PATH = "/v1/jobs/" + JOB_ID

Answer = dict[str, Any] | BaseException


@pytest.fixture(autouse=True)
def _clean_docql_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCQL_API_KEY", raising=False)
    monkeypatch.delenv("DOCQL_API_URL", raising=False)


class FakeClock:
    """Monotonic time that only moves when the wait sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(_jobs, "_monotonic", fake.monotonic)
    monkeypatch.setattr(_jobs, "_sleep", fake.sleep)
    return fake


class Script:
    """Serve answers in order (the last one repeated when repeat_last) and record requests."""

    def __init__(self, answers: Sequence[Answer], *, repeat_last: bool = False) -> None:
        self.answers = list(answers)
        self.repeat_last = repeat_last
        self.requests: list[httpx.Request] = []
        self.timeouts: list[dict[str, float]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.timeouts.append(dict(request.extensions["timeout"]))
        index = len(self.requests) - 1
        if index >= len(self.answers):
            if not self.repeat_last:
                raise AssertionError(f"poll {index + 1} past the scripted answers")
            index = len(self.answers) - 1
        answer = self.answers[index]
        if isinstance(answer, BaseException):
            raise answer
        return httpx.Response(
            answer["status"],
            headers=answer.get("headers") or {},
            content=answer["bodyUtf8"].encode(),
        )

    @property
    def methods(self) -> list[str]:
        return [r.method for r in self.requests]


def client_for(script: Script) -> DocQL:
    return DocQL(
        api_key="unit-test-key",
        api_url="http://stub.test",
        http_client=httpx.Client(transport=httpx.MockTransport(script)),
    )


def job_answer(
    status: str, *, retry_after: str | None = None, http_status: int = 200
) -> dict[str, Any]:
    headers = {"content-type": "application/json", "x-request-id": "req-x"}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    body = {"job_id": JOB_ID, "status": status, "metadata": {"page_count": 3, "pages_done": 1}}
    return {"status": http_status, "headers": headers, "bodyUtf8": json.dumps(body)}


def kit(case_id: str, **header_overrides: str | None) -> dict[str, Any]:
    case = dict(DECODE_CASES[case_id])
    headers = dict(case.get("headers") or {})
    for name, value in header_overrides.items():
        if value is None:
            headers.pop(name.replace("_", "-"), None)
        else:
            headers[name.replace("_", "-")] = value
    case["headers"] = headers
    return case


RUNNING = job_answer("running", retry_after="2")
SUCCEEDED = kit("job-200-succeeded")


def test_the_kit_scripts_w1_to_w7_in_order() -> None:
    assert [c["id"] for c in WAIT_CASES] == [
        "w1-succeeded",
        "w2-failed",
        "w3-cancelled",
        "w4-expired",
        "w5-not-found",
        "w6-poll-429",
        "w7-timeout",
    ]


@pytest.mark.parametrize("case", WAIT_CASES, ids=[c["id"] for c in WAIT_CASES])
def test_wait_case_offline(case: dict[str, Any], clock: FakeClock) -> None:
    script = Script(list(case["responses"]), repeat_last=case.get("repeatLast") is True)
    client = client_for(script)
    kwargs: dict[str, Any] = {}
    if "timeoutSeconds" in case:
        kwargs["timeout"] = case["timeoutSeconds"]
    expect = case["expect"]
    outcome = expect["outcome"]

    if outcome in ("succeeded", "cancelled"):
        job = client.wait(case["jobId"], **kwargs)
        assert job.status == outcome
        assert job.job_id == case["jobId"]
    elif outcome == "timeout":
        with pytest.raises(DocQLWaitTimeoutError) as timeout_info:
            client.wait(case["jobId"], **kwargs)
        assert timeout_info.value.job_id == case["jobId"]
    else:
        assert outcome in ("failed", "error")
        with pytest.raises(DocQLError) as error_info:
            client.wait(case["jobId"], **kwargs)
        assert error_info.value.reason == expect["reason"]
        assert error_info.value.job_id == case["jobId"]
        assert error_info.value.retryable is False

    if "sleepsSeconds" in expect:
        assert clock.sleeps == expect["sleepsSeconds"]
    if "minElapsedSeconds" in expect:
        assert clock.now >= expect["minElapsedSeconds"]
    assert len([m for m in script.methods if m != "GET"]) == expect["deletes"]
    assert script.requests
    for request in script.requests:
        assert request.url.raw_path.decode() == "/v1/jobs/" + quote(case["jobId"], safe="")


def test_w7_default_timeout_is_900_and_never_sleeps_past_it(clock: FakeClock) -> None:
    script = Script(list(W["w7-timeout"]["responses"]), repeat_last=True)
    with pytest.raises(DocQLWaitTimeoutError) as excinfo:
        client_for(script).wait(JOB_ID)
    assert clock.now == 900
    assert excinfo.value.timeout == 900.0
    # Polls at 0, 5, ..., 895 s: the sleep that ends on 900 s ends the wait,
    # never a 181st poll at the deadline (owner decision 2026-10-26, as Go and Java).
    assert len(script.requests) == 180
    assert set(script.methods) == {"GET"}


def test_w7_with_a_caller_timeout_of_10_s(clock: FakeClock) -> None:
    script = Script(list(W["w7-timeout"]["responses"]), repeat_last=True)
    with pytest.raises(DocQLWaitTimeoutError) as excinfo:
        client_for(script).wait(JOB_ID, timeout=10)
    assert clock.sleeps == [5, 5]
    assert len(script.requests) == 2
    assert excinfo.value.job_id == JOB_ID
    assert excinfo.value.timeout == 10
    assert JOB_ID in str(excinfo.value)
    assert "not cancelled" in str(excinfo.value)


def test_w1_without_retry_after_sleeps_1_s(clock: FakeClock) -> None:
    """The kit's own variant: W1 with every Retry-After removed counts each as 1 s."""
    answers = [
        dict(r, headers={k: v for k, v in r["headers"].items() if k != "retry-after"})
        for r in W["w1-succeeded"]["responses"]
    ]
    job = client_for(Script(answers)).wait(JOB_ID)
    assert job.status == JOB_STATUS_SUCCEEDED
    assert clock.sleeps == [1, 1]


def test_a_sleep_ending_exactly_on_the_deadline_ends_the_wait_without_a_poll(
    clock: FakeClock,
) -> None:
    """No poll is sent at or past the deadline (owner decision 2026-10-26, as Go and Java).

    timeout 4 s with Retry-After 2 twice: polls at 0 s and 2 s, the second
    sleep ends exactly on the deadline, and the wait raises instead of a third
    poll at 4 s (the scripted SUCCEEDED answer is never fetched).
    """
    script = Script([RUNNING, RUNNING, SUCCEEDED])
    with pytest.raises(DocQLWaitTimeoutError) as excinfo:
        client_for(script).wait(JOB_ID, timeout=4)
    assert clock.sleeps == [2, 2]
    assert clock.now == 4
    assert len(script.requests) == 2
    assert set(script.methods) == {"GET"}
    assert excinfo.value.job_id == JOB_ID
    assert excinfo.value.timeout == 4


def test_a_sleep_that_overshoots_the_deadline_ends_the_wait_without_a_poll(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real sleep may return late; past the deadline the wait raises, never polls."""

    def late_sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds + 0.5

    monkeypatch.setattr(_jobs, "_sleep", late_sleep)
    script = Script([RUNNING, RUNNING, SUCCEEDED])
    with pytest.raises(DocQLWaitTimeoutError):
        client_for(script).wait(JOB_ID, timeout=2.2)
    assert clock.sleeps == [2]
    assert len(script.requests) == 1


def test_a_sleep_past_the_deadline_raises_without_sleeping(clock: FakeClock) -> None:
    script = Script([RUNNING], repeat_last=True)
    with pytest.raises(DocQLWaitTimeoutError):
        client_for(script).wait(JOB_ID, timeout=3)
    assert clock.sleeps == [2]
    assert len(script.requests) == 2


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [("0", 1), (None, 1), ("100", 30), ("7", 7), ("http-date-5s", 5)],
)
def test_retry_after_is_clamped_to_1_and_30(
    retry_after: str | None, expected: int, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = datetime(2026, 10, 25, 9, 0, 0, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "FrozenDatetime":
            return cls.fromtimestamp(fixed.timestamp(), tz)

    monkeypatch.setattr(_response, "datetime", FrozenDatetime)
    if retry_after == "http-date-5s":
        retry_after = format_datetime(fixed + timedelta(seconds=5), usegmt=True)
    script = Script([job_answer("queued", retry_after=retry_after), SUCCEEDED])
    client_for(script).wait(JOB_ID)
    assert clock.sleeps == [expected]


@pytest.mark.parametrize(("poll_interval", "expected"), [(10.0, 10.0), (0.5, 2)])
def test_poll_interval_raises_the_sleep_never_lowers_it(
    poll_interval: float, expected: float, clock: FakeClock
) -> None:
    client_for(Script([RUNNING, RUNNING, SUCCEEDED])).wait(JOB_ID, poll_interval=poll_interval)
    assert clock.sleeps == [expected, expected]


def test_transient_503_without_retry_after_backs_off_1_2_4(clock: FakeClock) -> None:
    poll_503 = kit("job-503-poll")
    assert "retry-after" not in poll_503["headers"]
    script = Script([poll_503, poll_503, poll_503, SUCCEEDED])
    assert client_for(script).wait(JOB_ID).status == JOB_STATUS_SUCCEEDED
    assert clock.sleeps == [1, 2, 4]
    assert set(script.methods) == {"GET"}


def test_backoff_caps_at_30_and_resets_after_a_200(clock: FakeClock) -> None:
    poll_503 = kit("job-503-poll")
    running_no_header = job_answer("running")
    answers = [poll_503] * 7 + [running_no_header, poll_503, SUCCEEDED]
    client_for(Script(answers)).wait(JOB_ID)
    assert clock.sleeps == [1, 2, 4, 8, 16, 30, 30, 1, 1]


def test_a_429_with_retry_after_is_honoured(clock: FakeClock) -> None:
    rate_limited = kit("job-429-queue-full-tenant", retry_after="3")
    client_for(Script([rate_limited, SUCCEEDED])).wait(JOB_ID)
    assert clock.sleeps == [3]


def test_network_errors_and_poll_timeouts_are_retried(clock: FakeClock) -> None:
    script = Script([httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), SUCCEEDED])
    assert client_for(script).wait(JOB_ID).status == JOB_STATUS_SUCCEEDED
    assert clock.sleeps == [1, 2]
    for timeout in script.timeouts:
        assert timeout["read"] == 30
        assert timeout["connect"] == 10


def test_poll_timeout_is_capped_by_the_time_left_with_a_1_s_floor(clock: FakeClock) -> None:
    script = Script([RUNNING], repeat_last=True)
    with pytest.raises(DocQLWaitTimeoutError):
        client_for(script).wait(JOB_ID, timeout=5)
    assert clock.sleeps == [2, 2]
    assert [t["read"] for t in script.timeouts] == [5, 3, 1]
    assert [t["connect"] for t in script.timeouts] == [5, 3, 1]

    tiny = Script([SUCCEEDED])
    client_for(tiny).wait(JOB_ID, timeout=0.5)
    assert tiny.timeouts[0]["read"] == 1
    assert tiny.timeouts[0]["connect"] == 1


INVALID_KEY_401 = {
    "status": 401,
    "headers": {"content-type": "application/json", "x-request-id": "req-401"},
    "bodyUtf8": json.dumps(
        {"error_info": "invalid API key", "metadata": {"reason": "invalid_api_key"}}
    ),
}


@pytest.mark.parametrize(
    ("answer", "status", "reason"),
    [
        (INVALID_KEY_401, 401, "invalid_api_key"),
        (kit("job-404-not-found"), 404, "job_not_found"),
        (kit("job-404-expired"), 404, "job_expired"),
        (kit("job-404-jobs-off"), 404, None),
        (kit("job-405"), 405, None),
    ],
    ids=["401", "404-not-found", "404-expired", "404-jobs-off", "405"],
)
def test_non_transient_errors_raise_at_once(
    answer: dict[str, Any], status: int, reason: str | None, clock: FakeClock
) -> None:
    script = Script([answer, SUCCEEDED])
    with pytest.raises(DocQLError) as excinfo:
        client_for(script).wait(JOB_ID)
    assert excinfo.value.status == status
    assert excinfo.value.reason == reason
    assert excinfo.value.job_id == JOB_ID
    assert len(script.requests) == 1
    assert clock.sleeps == []


def test_a_failed_job_with_an_embedded_503_raises_once_not_retryable(clock: FakeClock) -> None:
    script = Script([kit("job-200-failed-upstream"), SUCCEEDED])
    with pytest.raises(DocQLError) as excinfo:
        client_for(script).wait(JOB_ID)
    assert excinfo.value.status == 503
    assert excinfo.value.reason == "upstream_unavailable"
    assert excinfo.value.retryable is False
    assert excinfo.value.job_id == JOB_ID
    assert len(script.requests) == 1
    assert clock.sleeps == []


def test_keyboard_interrupt_in_a_sleep_propagates_and_sends_no_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(_jobs, "_sleep", interrupted)
    script = Script([RUNNING, SUCCEEDED])
    with pytest.raises(KeyboardInterrupt):
        client_for(script).wait(JOB_ID)
    assert script.methods == ["GET"]


def test_an_unknown_status_is_polled_until_the_deadline(clock: FakeClock) -> None:
    script = Script([job_answer("paused")], repeat_last=True)
    with pytest.raises(DocQLWaitTimeoutError):
        client_for(script).wait(JOB_ID, timeout=10)
    assert clock.sleeps == [1] * 10
    assert len(script.requests) == 10
    assert set(script.methods) == {"GET"}


def test_wait_twice_on_a_succeeded_job(clock: FakeClock) -> None:
    script = Script([SUCCEEDED], repeat_last=True)
    client = client_for(script)
    first = client.wait(JOB_ID)
    second = client.wait(JOB_ID)
    assert isinstance(first, Job)
    assert first == second
    assert first.status == JOB_STATUS_SUCCEEDED
    assert script.methods == ["GET", "GET"]
    assert clock.sleeps == []


def test_wait_on_a_cancelled_job_returns_it(clock: FakeClock) -> None:
    assert client_for(Script([kit("job-200-cancelled")])).wait(JOB_ID).status == (
        JOB_STATUS_CANCELLED
    )


@pytest.mark.parametrize(
    "kwargs",
    [{"timeout": 0}, {"timeout": -1}, {"poll_interval": 0}, {"poll_interval": -2.0}],
)
def test_bad_timeout_or_poll_interval_raise_before_any_request(
    kwargs: dict[str, float], clock: FakeClock
) -> None:
    script = Script([SUCCEEDED])
    with pytest.raises(ValueError):
        client_for(script).wait(JOB_ID, **kwargs)
    assert script.requests == []


def test_no_delete_anywhere_in_the_wait_module() -> None:
    """OD-5: the only DELETE in _jobs is the explicit cancel helper."""
    source = Path(_jobs.__file__).read_text()
    lines = [line for line in source.splitlines() if '"DELETE"' in line]
    assert len(lines) == 1
    cancel_source = source[source.index("def cancel(") : source.index("def wait_for(")]
    assert '"DELETE"' in cancel_source

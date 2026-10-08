"""Transport-error mapping, bounded error reads, the upload size hint, the
no-retry rule and key redaction tests (D-05, D-09, PY-04)."""

import json
import logging
from collections.abc import Callable, Iterator

import h11
import httpx
import pytest

from docql import (
    DocQL,
    DocQLConnectionError,
    DocQLError,
    DocQLException,
    DocQLTimeoutError,
    QueryResult,
)

KEY = "dql_SECRETSECRETSECRETSECRETSECRET_abcdef"


@pytest.fixture(autouse=True)
def _clean_docql_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient DOCQL_* configuration leaks into any test."""
    monkeypatch.delenv("DOCQL_API_KEY", raising=False)
    monkeypatch.delenv("DOCQL_API_URL", raising=False)


class FailingTransport(httpx.BaseTransport):
    """A transport whose handle_request raises a fixed exception at send time."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise self.exc


class StreamingTransport(httpx.BaseTransport):
    """A transport answering with a lazily streamed body."""

    def __init__(self, status: int, make_stream: Callable[[], Iterator[bytes]]) -> None:
        self.status = status
        self.make_stream = make_stream

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            self.status,
            headers={"content-type": "application/json"},
            content=self.make_stream(),
        )


def client_with(transport: httpx.BaseTransport, api_key: str = "unit-test-key") -> DocQL:
    return DocQL(
        api_key=api_key,
        api_url="http://stub.test",
        http_client=httpx.Client(transport=transport),
    )


def query(client: DocQL) -> QueryResult:
    return client.query_document(b"%PDF-1.4 unit test", filename="f.pdf", query="q")


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("connect timed out"),
        httpx.ReadTimeout("read timed out"),
        httpx.WriteTimeout("write timed out"),
        httpx.PoolTimeout("pool timed out"),
    ],
)
def test_timeout_exceptions_map_to_docql_timeout(exc: httpx.TimeoutException) -> None:
    with pytest.raises(DocQLTimeoutError) as excinfo:
        query(client_with(FailingTransport(exc)))
    assert excinfo.value.__cause__ is exc


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadError("read failed"),
        httpx.RemoteProtocolError("server disconnected"),
        httpx.DecodingError("bad chunked encoding"),
    ],
)
def test_request_errors_map_to_docql_connection(exc: httpx.RequestError) -> None:
    with pytest.raises(DocQLConnectionError) as excinfo:
        query(client_with(FailingTransport(exc)))
    err = excinfo.value
    assert not isinstance(err, DocQLTimeoutError)
    assert err.__cause__ is exc


def test_raw_h11_error_maps_to_docql_connection() -> None:
    exc = h11.LocalProtocolError("Too little data for declared Content-Length")
    with pytest.raises(DocQLConnectionError) as excinfo:
        query(client_with(FailingTransport(exc)))
    err = excinfo.value
    assert not isinstance(err, DocQLTimeoutError)
    assert err.__cause__ is exc


def test_read_timeout_while_body_is_read_maps_to_timeout() -> None:
    def stream() -> Iterator[bytes]:
        yield b'{"data": '
        raise httpx.ReadTimeout("read timed out")

    with pytest.raises(DocQLTimeoutError) as excinfo:
        query(client_with(StreamingTransport(200, stream)))
    assert isinstance(excinfo.value.__cause__, httpx.ReadTimeout)


def test_unparseable_api_url_raises_value_error_at_construction() -> None:
    with pytest.raises(ValueError, match="DOCQL_API_URL") as excinfo:
        DocQL(api_key="unit-test-key", api_url="http://[::1")
    assert not isinstance(excinfo.value, httpx.InvalidURL)


def envelope_handler(
    seen: list[httpx.Request], status: int
) -> Callable[[httpx.Request], httpx.Response]:
    """A handler that records the request and answers with an error envelope."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            status,
            json={
                "error_info": "busy",
                "status_code": 429,
                "metadata": {"reason": "service_overloaded", "request_id": "r"},
            },
        )

    return handler


def test_no_automatic_retry_for_429_503_or_timeout() -> None:
    for status in (429, 503):
        seen: list[httpx.Request] = []
        client = DocQL(
            api_key="unit-test-key",
            api_url="http://stub.test",
            http_client=httpx.Client(transport=httpx.MockTransport(envelope_handler(seen, status))),
        )
        with pytest.raises(DocQLError) as excinfo:
            client.query_document(b"%PDF-1.4 unit test", filename="f.pdf", query="q")
        assert excinfo.value.status == status
        assert len(seen) == 1

    transport = FailingTransport(httpx.ReadTimeout("read timed out"))
    with pytest.raises(DocQLTimeoutError):
        query(client_with(transport))
    assert transport.calls == 1


class PullMeter:
    """Counts how many bytes a response stream actually yielded, and its close."""

    def __init__(self) -> None:
        self.pulled = 0
        self.closed = False


class MeteredStream(httpx.SyncByteStream):
    """A 10 MiB body recording its pulls; close() observes the response close.

    httpx's IteratorByteStream has no close(), so a generator body can never
    observe response.close(); real network streams release their connection
    through this exact SyncByteStream.close() hook.
    """

    def __init__(self, meter: PullMeter) -> None:
        self.meter = meter

    def __iter__(self) -> Iterator[bytes]:
        while self.meter.pulled < 10 * 1024 * 1024:
            self.meter.pulled += 65536
            yield b"x" * 65536

    def close(self) -> None:
        self.meter.closed = True


def test_error_body_read_is_bounded_and_closed() -> None:
    meter = PullMeter()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, headers={"content-type": "text/plain"}, stream=MeteredStream(meter)
        )

    with pytest.raises(DocQLError) as excinfo:
        query(client_with(httpx.MockTransport(handler)))
    err = excinfo.value
    assert err.status == 500
    assert len(err.body) <= 2048
    assert meter.pulled <= 65536
    assert meter.closed is True


def test_success_body_is_read_in_full() -> None:
    blob = "x" * (1024 * 1024)
    payload = json.dumps({"data": {"blob": blob}, "metadata": {"request_id": "r1"}}).encode()

    def stream() -> Iterator[bytes]:
        for start in range(0, len(payload), 65536):
            yield payload[start : start + 65536]

    result = query(client_with(StreamingTransport(200, stream)))
    assert result.data["blob"] == blob


def test_size_hint_only_for_oversized_uploads() -> None:
    with pytest.raises(DocQLConnectionError) as big:
        client_with(FailingTransport(httpx.ConnectError("connection refused"))).query_document(
            b"x" * 26_214_401, filename="f.pdf", query="q"
        )
    assert "25 MiB" in str(big.value)
    assert not hasattr(big.value, "reason")

    with pytest.raises(DocQLConnectionError) as small:
        client_with(FailingTransport(httpx.ConnectError("connection refused"))).query_document(
            b"x" * 10, filename="f.pdf", query="q"
        )
    assert "25 MiB" not in str(small.value)
    assert not hasattr(small.value, "reason")


def test_api_key_never_appears_on_any_surface(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def echo_envelope(request: httpx.Request) -> httpx.Response:
        echoed = request.headers["x-api-key"]
        return httpx.Response(
            401,
            json={
                "error_info": f"key {echoed} rejected",
                "status_code": 401,
                "metadata": {"reason": "invalid_api_key", "request_id": "r-401"},
            },
        )

    def echo_plain(request: httpx.Request) -> httpx.Response:
        echoed = request.headers["x-api-key"]
        return httpx.Response(400, content=f"bad key {echoed}".encode())

    envelope_client = DocQL(
        api_key=KEY,
        api_url="http://stub.test",
        http_client=httpx.Client(transport=httpx.MockTransport(echo_envelope)),
    )
    with pytest.raises(DocQLError) as envelope_exc:
        envelope_client.query_document(b"%PDF-1.4", filename="f.pdf", query="q")
    err = envelope_exc.value
    for surface in (str(err), repr(err), err.body, err.message or ""):
        assert KEY not in surface

    plain_client = DocQL(
        api_key=KEY,
        api_url="http://stub.test",
        http_client=httpx.Client(transport=httpx.MockTransport(echo_plain)),
    )
    with pytest.raises(DocQLError) as plain_exc:
        plain_client.query_document(b"%PDF-1.4", filename="f.pdf", query="q")
    plain = plain_exc.value
    for surface in (str(plain), repr(plain), plain.body, plain.message or ""):
        assert KEY not in surface

    for raised in (
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("read timed out"),
    ):
        failing = DocQL(
            api_key=KEY,
            api_url="http://stub.test",
            http_client=httpx.Client(transport=FailingTransport(raised)),
        )
        with pytest.raises(DocQLException) as caught:
            failing.query_document(b"%PDF-1.4", filename="f.pdf", query="q")
        assert KEY not in str(caught.value)
        assert KEY not in repr(caught.value)

    assert KEY not in repr(envelope_client)

    for record in caplog.records:
        if record.name.split(".")[0] in ("httpx", "docql"):
            assert KEY not in record.getMessage()

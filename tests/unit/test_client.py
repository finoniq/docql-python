"""Constructor configuration and injected-client policy tests (D-01, D-02, D-05..D-08)."""

import json
import re
import typing
from typing import Any

import httpx
import pytest

import docql
from docql import DocQL, DocQLError

OK_REQUEST_ID = "00000000-0000-4000-8000-000000000001"
USER_AGENT_RE = re.compile(r"^docql-python/\S+ \(python \d+\.\d+\.\d+; httpx \d+\.\d+\.\d+\)$")


@pytest.fixture(autouse=True)
def _clean_docql_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient DOCQL_* configuration leaks into any test."""
    monkeypatch.delenv("DOCQL_API_KEY", raising=False)
    monkeypatch.delenv("DOCQL_API_URL", raising=False)


def ok_response() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-request-id": OK_REQUEST_ID},
        json={
            "data": {"answer": "ok"},
            "metadata": {"request_id": OK_REQUEST_ID, "page_count": 1, "pages_ocr": 0},
        },
    )


def query_once(
    client_kwargs: dict[str, Any] | None = None,
    call_kwargs: dict[str, Any] | None = None,
) -> httpx.Request:
    """Run one query_document call through a MockTransport and return the captured request."""
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return ok_response()

    defaults: dict[str, Any] = {
        "api_key": "unit-test-key",
        "api_url": "http://stub.test",
        "http_client": httpx.Client(transport=httpx.MockTransport(handler)),
    }
    client = DocQL(**{**defaults, **(client_kwargs or {})})
    call: dict[str, Any] = {"file": b"%PDF-1.4 unit test", "filename": "f.pdf", "query": "q"}
    client.query_document(**{**call, **(call_kwargs or {})})
    request = captured["request"]
    assert request is not None
    return request


def body_part_json(request: httpx.Request) -> Any:
    """Decode the JSON body part of a captured multipart request."""
    raw = request.read()
    boundary = request.headers["content-type"].partition("boundary=")[2].encode()
    marker = b'Content-Disposition: form-data; name="body"\r\n\r\n'
    start = raw.index(marker) + len(marker)
    end = raw.index(b"\r\n--" + boundary + b"--")
    return json.loads(raw[start:end])


def test_env_key_and_url_used_when_arguments_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCQL_API_KEY", "env-key")
    monkeypatch.setenv("DOCQL_API_URL", "http://env-stub.test/")
    request = query_once({"api_key": None, "api_url": None})
    assert request.headers["x-api-key"] == "env-key"
    assert str(request.url) == "http://env-stub.test/v1/query-document"


def test_explicit_arguments_beat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCQL_API_KEY", "env-key")
    monkeypatch.setenv("DOCQL_API_URL", "http://env-stub.test/")
    request = query_once({"api_key": "explicit-key", "api_url": "http://explicit.test/"})
    assert request.headers["x-api-key"] == "explicit-key"
    assert str(request.url) == "http://explicit.test/v1/query-document"


@pytest.mark.parametrize("env_value", ["", "   "])
def test_blank_env_url_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, env_value: str
) -> None:
    monkeypatch.setenv("DOCQL_API_URL", env_value)
    request = query_once({"api_url": None})
    assert str(request.url) == "https://api-docql.finoniq.com/v1/query-document"


def test_unset_env_url_falls_back_to_default() -> None:
    request = query_once({"api_url": None})
    assert str(request.url) == "https://api-docql.finoniq.com/v1/query-document"


def test_trailing_slash_stripped() -> None:
    request = query_once({"api_url": "http://127.0.0.1:1/"})
    assert str(request.url) == "http://127.0.0.1:1/v1/query-document"


@pytest.mark.parametrize(
    ("api_key_arg", "env_value"),
    [(None, None), (None, ""), (None, "  "), ("", None), ("  ", None)],
)
def test_missing_api_key_raises_before_any_request(
    monkeypatch: pytest.MonkeyPatch, api_key_arg: str | None, env_value: str | None
) -> None:
    if env_value is not None:
        monkeypatch.setenv("DOCQL_API_KEY", env_value)
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return ok_response()

    with pytest.raises(ValueError, match="DOCQL_API_KEY"):
        client = DocQL(
            api_key=api_key_arg,
            api_url="http://stub.test",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        client.query_document(b"%PDF-1.4 unit test", filename="f.pdf", query="q")
    assert "request" not in captured


@pytest.mark.parametrize("bad_url", ["", "ftp://x", "api-docql.finoniq.com"])
def test_invalid_api_url_raises_value_error(bad_url: str) -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return ok_response()

    with pytest.raises(ValueError, match="DOCQL_API_URL"):
        DocQL(
            api_key="unit-test-key",
            api_url=bad_url,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    assert "request" not in captured


def test_timeout_float_form_sets_read_write_pool() -> None:
    request = query_once({"timeout": 5})
    assert request.extensions["timeout"] == {
        "connect": 10.0,
        "read": 5.0,
        "write": 5.0,
        "pool": 5.0,
    }


def test_timeout_httpx_timeout_used_unchanged() -> None:
    request = query_once({"timeout": httpx.Timeout(3.0)})
    assert request.extensions["timeout"] == {
        "connect": 3.0,
        "read": 3.0,
        "write": 3.0,
        "pool": 3.0,
    }


@pytest.mark.parametrize("bad_timeout", [None, True, "5"])
def test_timeout_invalid_type_raises_type_error(bad_timeout: object) -> None:
    with pytest.raises(TypeError):
        query_once({"timeout": bad_timeout})


def test_injected_client_defaults_scrubbed_and_timeout_overridden() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return ok_response()

    injected = httpx.Client(
        timeout=1.0,
        follow_redirects=True,
        headers={"X-Request-Id": "x", "X-DocQL-Tenant": "t", "x-docql-other": "y"},
        transport=httpx.MockTransport(handler),
    )
    client = DocQL(api_key="unit-test-key", api_url="http://stub.test", http_client=injected)
    client.query_document(b"%PDF-1.4 unit test", filename="f.pdf", query="q")
    request = captured["request"]
    assert request.extensions["timeout"] == {
        "connect": 10.0,
        "read": 660.0,
        "write": 660.0,
        "pool": 660.0,
    }
    for forbidden in ("x-request-id", "x-docql-tenant", "x-docql-other"):
        assert forbidden not in request.headers
    assert request.headers["x-api-key"] == "unit-test-key"
    assert request.headers["user-agent"].startswith("docql-python/")
    # The key is a per-request header only; the caller's client stays untouched.
    assert injected.headers.get("x-api-key") is None
    assert injected.headers["x-request-id"] == "x"
    assert injected.headers["x-docql-tenant"] == "t"


def test_redirect_to_other_host_is_not_followed() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(307, headers={"location": "https://other.example/v1/query-document"})

    injected = httpx.Client(follow_redirects=True, transport=httpx.MockTransport(handler))
    client = DocQL(api_key="unit-test-key", api_url="http://stub.test", http_client=injected)
    with pytest.raises(DocQLError) as excinfo:
        client.query_document(b"%PDF-1.4 unit test", filename="f.pdf", query="q")
    assert excinfo.value.status == 307
    assert excinfo.value.reason is None
    assert len(calls) == 1


def test_context_manager_closes_only_the_owned_client() -> None:
    owned = DocQL(api_key="unit-test-key")
    with owned as entered:
        assert entered is owned
    assert owned._client.is_closed

    def handler(request: httpx.Request) -> httpx.Response:
        return ok_response()

    injected = httpx.Client(transport=httpx.MockTransport(handler))
    DocQL(api_key="unit-test-key", api_url="http://stub.test", http_client=injected).close()
    assert injected.is_closed is False


def test_repr_shows_url_not_key() -> None:
    client = DocQL(api_key="secret-key-123", api_url="http://h")
    assert "http://h" in repr(client)
    assert "secret-key-123" not in repr(client)


def test_user_agent_carries_runtime_versions() -> None:
    request = query_once()
    assert USER_AGENT_RE.fullmatch(request.headers["user-agent"])


def test_mode_constants_and_open_mode_string() -> None:
    assert docql.MODE_FAST == "fast"
    assert docql.MODE_STANDARD == "standard"
    assert typing.get_args(docql.Mode) == ("fast", "standard")
    # mode stays an open str: unknown values ride inside params verbatim (D-05)
    request = query_once(call_kwargs={"mode": "turbo"})
    assert body_part_json(request) == {"query": "q", "params": {"mode": "turbo"}}

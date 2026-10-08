"""Error decoding tests (PY-04): the vendored decode cases, Retry-After,
the retryable set, unusable 2xx bodies and the key scrub (D-10..D-12)."""

import json
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest

from docql import _response
from docql._errors import DocQLError
from docql._response import decode_error, parse_result

ROOT = Path(__file__).resolve().parents[2]
DECODE_CASE_IDS = [
    case["id"]
    for case in json.loads((ROOT / "contract" / "sdk-cases.json").read_bytes())["decodeCases"]
]

API_KEY = "secret-key-123"
NOW_WHOLE = datetime(2026, 10, 8, 12, 0, 1, tzinfo=UTC)  # an integral-second "now"
NOW_SUB_SECOND = datetime(2026, 10, 8, 12, 0, 0, 800000, tzinfo=UTC)  # .8 s past the minute


def envelope_body(reason: str, error_info: str, request_id: str | None = "req-1") -> bytes:
    """A DocQL error envelope like the stub's sendEnvelopeError output."""
    metadata: dict[str, Any] = {"reason": reason}
    if request_id is not None:
        metadata["request_id"] = request_id
    return json.dumps({"error_info": error_info, "status_code": 503, "metadata": metadata}).encode()


def vendored_case(case_id: str, sdk_cases: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in sdk_cases["decodeCases"] if c["id"] == case_id)


def http_date(second: int, minute: int = 0, hour: int = 12) -> str:
    return format_datetime(datetime(2026, 10, 8, hour, minute, second, tzinfo=UTC), usegmt=True)


@pytest.mark.parametrize("case_id", DECODE_CASE_IDS)
def test_decode_case_matches_vendored_expectation(case_id: str, sdk_cases: dict[str, Any]) -> None:
    case = vendored_case(case_id, sdk_cases)
    err = decode_error(
        case["status"], case.get("headers") or {}, case["bodyUtf8"].encode(), api_key=API_KEY
    )
    expect = case["expect"]
    assert err.status == expect["status"]
    assert err.reason == expect["reason"]
    assert err.request_id == expect["requestId"]


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (429, True),
        (503, True),
        (400, False),
        (401, False),
        (404, False),
        (413, False),
        (415, False),
        (422, False),
        (500, False),
        (502, False),
        (504, False),
        (524, False),
    ],
)
def test_retryable_only_for_429_and_503(status: int, retryable: bool) -> None:
    err = decode_error(status, {}, envelope_body("service_overloaded", "busy"), api_key=API_KEY)
    assert err.retryable is retryable


def test_envelope_429_with_retry_after_header() -> None:
    err = decode_error(
        429,
        {"Retry-After": "5"},
        envelope_body("service_overloaded", "busy"),
        api_key=API_KEY,
    )
    assert err.retry_after == 5
    assert err.retryable is True


def test_parse_retry_after_http_date_uses_injected_now() -> None:
    assert _response.parse_retry_after(http_date(31), now=NOW_WHOLE) == 30  # exactly 30.0 s
    assert _response.parse_retry_after(http_date(30), now=NOW_SUB_SECOND) == 30  # 29.2 s -> 30
    assert (
        _response.parse_retry_after(http_date(0, minute=59, hour=11), now=NOW_SUB_SECOND) == 0
    )  # clamped


@pytest.mark.parametrize("value", ["soon", "-1", ""])
def test_parse_retry_after_unparsable_gives_none(value: str) -> None:
    assert _response.parse_retry_after(value) is None


def test_retry_after_parsed_on_503_too() -> None:
    err = decode_error(
        503,
        {"Retry-After": "7"},
        envelope_body("upstream_unavailable", "down"),
        api_key=API_KEY,
    )
    assert err.retry_after == 7


def test_non_envelope_bodies_keep_reason_none() -> None:
    int_reason = json.dumps({"metadata": {"reason": 5}}).encode()
    for body in (int_reason, b"[1, 2]", b"<html><body>nope</body></html>"):
        err = decode_error(500, {}, body, api_key=API_KEY)
        assert err.reason is None


def test_str_envelope_429() -> None:
    err = decode_error(
        429,
        {"Retry-After": "5"},
        envelope_body("service_overloaded", "slow down", request_id="req-429"),
        api_key=API_KEY,
    )
    assert str(err) == "429 service_overloaded: slow down (request_id=req-429, retry_after=5s)"


def test_str_framework_404(sdk_cases: dict[str, Any]) -> None:
    case = vendored_case("framework-404", sdk_cases)
    err = decode_error(case["status"], case["headers"], case["bodyUtf8"].encode(), api_key=API_KEY)
    assert str(err) == (
        '404 (non-DocQL response): {"detail":"Not Found"} '
        "(request_id=00000000-0000-4000-8000-000000000404)"
    )


def test_str_empty_502_body() -> None:
    err = decode_error(502, {}, b"", api_key=API_KEY)
    assert str(err) == "502 (non-DocQL response): (empty body)"


def test_str_524_html_snippet_collapsed_and_bounded() -> None:
    body = (
        "<html>\r\n<head><title>524</title></head>\r\n<body>" + "x" * 400 + "</body>\r\n</html>\r\n"
    )
    err = decode_error(524, {"content-type": "text/html"}, body.encode(), api_key=API_KEY)
    text = str(err)
    assert "\r" not in text
    assert "\n" not in text
    snippet = text.split("): ", 1)[1]
    assert len(snippet) <= 200


def test_body_kept_at_2048_chars() -> None:
    err = decode_error(500, {}, b"x" * 5000, api_key=API_KEY)
    assert len(err.body) == 2048


def test_key_scrubbed_from_body_message_str_and_repr() -> None:
    body = envelope_body("invalid_api_key", f"key {API_KEY} is invalid", request_id="req-9")
    err = decode_error(401, {}, body, api_key=API_KEY)
    assert API_KEY not in err.body
    assert "***" in err.body
    assert err.message is not None
    assert API_KEY not in err.message
    assert "***" in err.message
    assert API_KEY not in str(err)
    assert API_KEY not in repr(err)


@pytest.mark.parametrize("body", [b"<html>", b"[]"])
def test_parse_result_non_json_body_raises(body: bytes) -> None:
    with pytest.raises(DocQLError) as excinfo:
        parse_result(200, {}, body, api_key=API_KEY)
    err = excinfo.value
    assert err.status == 200
    assert err.reason is None
    assert err.retry_after is None
    assert err.retryable is False
    assert err.message == "unexpected non-JSON response"


def test_parse_result_data_not_an_object_raises() -> None:
    with pytest.raises(DocQLError) as excinfo:
        parse_result(200, {}, b'{"data": [1]}', api_key=API_KEY)
    assert excinfo.value.message == "unexpected response: data is not a JSON object"


@pytest.mark.parametrize("body", [b"{}", b'{"data": null}'])
def test_parse_result_missing_or_null_data_is_empty(body: bytes) -> None:
    assert parse_result(200, {}, body, api_key=API_KEY).data == {}


def test_parse_result_keeps_unknown_metadata_fields() -> None:
    body = b'{"data": {"a": 1}, "metadata": {"request_id": "r1", "future_field": "kept"}}'
    result = parse_result(200, {}, body, api_key=API_KEY)
    assert result.metadata["future_field"] == "kept"
    assert result.metadata["request_id"] == "r1"


@pytest.mark.parametrize("value", [True, 3.0, "3"])
def test_parse_result_page_count_requires_exact_int(value: Any) -> None:
    body = json.dumps({"data": {}, "metadata": {"page_count": value}}).encode()
    assert parse_result(200, {}, body, api_key=API_KEY).page_count is None


def test_parse_result_request_id_falls_back_to_header_then_empty() -> None:
    from_body = parse_result(
        200, {}, b'{"data": {}, "metadata": {"request_id": "body-id"}}', api_key=API_KEY
    )
    assert from_body.request_id == "body-id"
    from_header = parse_result(200, {"x-request-id": "header-id"}, b'{"data": {}}', api_key=API_KEY)
    assert from_header.request_id == "header-id"
    neither = parse_result(200, {}, b'{"data": {}}', api_key=API_KEY)
    assert neither.request_id == ""

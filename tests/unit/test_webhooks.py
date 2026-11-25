"""verify_webhook API shape, input validation, no-echo, pickling and totality (D-06..D-14)."""

import base64
import dataclasses
import email.message
import http.client
import inspect
import io
import json
import math
import pickle
import random
from pathlib import Path
from typing import Any

import pytest

import docql
from docql import (
    DocQLError,
    DocQLException,
    WebhookEvent,
    WebhookVerificationError,
    verify_webhook,
)
from docql._webhooks import _verify_at

ROOT = Path(__file__).resolve().parents[2]
CASES: dict[str, dict[str, Any]] = {
    case["id"]: case
    for case in json.loads((ROOT / "testdata" / "webhook-vectors.json").read_bytes())["cases"]
}
VALID = CASES["valid-job-succeeded"]
VALID_BODY = base64.b64decode(VALID["body_base64"], validate=True)
VALID_HEADERS: dict[str, str] = dict(VALID["headers"])
SECRET: str = VALID["secrets"][0]
NOW: int = VALID["now"]
REASONS = (
    "missing_header",
    "invalid_timestamp",
    "invalid_signature_header",
    "signature_mismatch",
    "timestamp_too_old",
    "timestamp_too_new",
    "invalid_payload",
)


def verify_valid(
    body: Any = VALID_BODY, headers: Any = None, secrets: Any = SECRET
) -> WebhookEvent:
    return _verify_at(
        body,
        VALID_HEADERS if headers is None else headers,
        secrets,
        tolerance=300,
        now=NOW,
    )


def windows(text: str, size: int = 6) -> set[str]:
    return {text[i : i + size] for i in range(len(text) - size + 1)}


def test_signature_is_body_headers_secrets_and_keyword_only_tolerance() -> None:
    params = inspect.signature(verify_webhook).parameters
    assert list(params) == ["body", "headers", "secrets", "tolerance"]
    for name in ("body", "headers", "secrets"):
        assert params[name].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert params[name].default is inspect.Parameter.empty
    assert params["tolerance"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["tolerance"].default == 300
    assert docql.verify_webhook is verify_webhook


@pytest.mark.parametrize("tolerance", [True, False, "300", None, [300]])
def test_tolerance_of_the_wrong_type_is_a_type_error(tolerance: Any) -> None:
    # Checked first: nothing else about the call is valid here.
    with pytest.raises(TypeError, match="tolerance"):
        verify_webhook(b"", {}, [], tolerance=tolerance)


@pytest.mark.parametrize("tolerance", [math.nan, math.inf, -math.inf, 0, 0.0, -1, -0.5])
def test_tolerance_that_is_not_positive_and_finite_is_a_value_error(tolerance: float) -> None:
    with pytest.raises(ValueError, match="tolerance"):
        verify_webhook(b"", {}, [], tolerance=tolerance)


def test_a_positive_fractional_tolerance_is_accepted() -> None:
    event = _verify_at(VALID_BODY, VALID_HEADERS, SECRET, tolerance=0.5, now=NOW)
    assert event.id == VALID["expect"]["event"]["id"]


def test_a_huge_integer_tolerance_is_accepted() -> None:
    event = _verify_at(VALID_BODY, VALID_HEADERS, SECRET, tolerance=10**400, now=NOW)
    assert event.type == docql.EVENT_JOB_SUCCEEDED


@pytest.mark.parametrize("body", [bytearray(VALID_BODY), memoryview(VALID_BODY), None, 42])
def test_a_body_that_is_not_bytes_or_str_is_a_type_error(body: Any) -> None:
    with pytest.raises(TypeError, match="body must be bytes or str"):
        verify_webhook(body, VALID_HEADERS, SECRET)


def test_headers_without_items_are_a_type_error() -> None:
    with pytest.raises(TypeError, match="headers must be a mapping"):
        verify_webhook(VALID_BODY, None, SECRET)  # type: ignore[arg-type]


def test_a_tuple_of_secrets_works() -> None:
    assert verify_valid(secrets=(SECRET,)).id == VALID["expect"]["event"]["id"]


def test_a_non_str_secret_is_a_type_error_naming_its_position() -> None:
    with pytest.raises(TypeError, match="secret 2"):
        verify_webhook(VALID_BODY, VALID_HEADERS, [SECRET, 7])  # type: ignore[list-item]


@pytest.mark.parametrize("secrets", [None, 7, SECRET.encode(), {SECRET}])
def test_secrets_of_the_wrong_type_are_a_type_error(secrets: Any) -> None:
    with pytest.raises(TypeError, match="secrets"):
        verify_webhook(VALID_BODY, VALID_HEADERS, secrets)


def test_an_empty_secret_list_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="no webhook secrets"):
        verify_webhook(VALID_BODY, VALID_HEADERS, [])


def test_the_public_function_reads_the_wall_clock() -> None:
    # The vector was signed for 2026-11-19; the HMAC passes, then the window refuses it.
    with pytest.raises(WebhookVerificationError) as info:
        verify_webhook(VALID_BODY, VALID_HEADERS, VALID["secrets"])
    assert info.value.reason == "timestamp_too_old"


def test_the_verification_error_is_a_docql_exception_and_not_a_docql_error() -> None:
    assert issubclass(WebhookVerificationError, DocQLException)
    assert not issubclass(WebhookVerificationError, DocQLError)


def first_error(reason: str) -> tuple[dict[str, Any], WebhookVerificationError]:
    case = next(
        c
        for c in CASES.values()
        if c["expect"]["result"] == "webhook_error" and c["expect"]["reason"] == reason
    )
    with pytest.raises(WebhookVerificationError) as info:
        _verify_at(
            base64.b64decode(case["body_base64"]),
            dict(case["headers"]),
            case["secrets"],
            tolerance=case["tolerance_seconds"],
            now=case["now"],
        )
    return case, info.value


@pytest.mark.parametrize("reason", REASONS)
def test_str_and_repr_hold_only_the_reason_and_a_fixed_sentence(reason: str) -> None:
    case, err = first_error(reason)
    assert err.reason == reason
    assert str(err).startswith(f"{reason}: ")
    assert repr(err) == f"WebhookVerificationError(reason={reason!r})"
    secret_text = "".join(case["secrets"])
    signatures = " ".join(value for name, value in case["headers"] if "signature" in name.lower())
    for text in (str(err), repr(err)):
        for window in windows(secret_text.split("whsec_", 1)[-1]) | windows(signatures[3:]):
            assert window not in text
    # The sentence depends on the reason alone, never on the request.
    assert str(err) == str(WebhookVerificationError(reason))


@pytest.mark.parametrize("reason", REASONS)
def test_the_verification_error_pickles(reason: str) -> None:
    _, err = first_error(reason)
    restored = pickle.loads(pickle.dumps(err))
    assert type(restored) is WebhookVerificationError
    assert restored.reason == reason
    assert str(restored) == str(err)


def test_the_event_is_frozen() -> None:
    event = verify_valid()
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.id = "other"  # type: ignore[misc]


def test_two_events_from_the_same_body_do_not_share_data() -> None:
    first = verify_valid()
    second = verify_valid()
    assert first == second
    assert first.data is not second.data
    first.data["status"] = "changed"
    assert second.data["status"] == "succeeded"


def test_an_email_message_works_as_headers() -> None:
    message = email.message.Message()
    for name, value in VALID["headers"]:
        message[name] = value
    event = verify_valid(headers=message)
    assert event.job_id == VALID["expect"]["event"]["job_id"]


def http_message(pairs: list[tuple[str, str]]) -> http.client.HTTPMessage:
    raw = b"".join(f"{name}: {value}\r\n".encode("latin-1") for name, value in pairs) + b"\r\n"
    return http.client.parse_headers(io.BytesIO(raw))


def test_an_http_message_passes_straight_in() -> None:
    # Called directly, not through verify_valid, so mypy checks the headers annotation.
    message = http_message([(name, value) for name, value in VALID["headers"]])
    event = _verify_at(VALID_BODY, message, SECRET, tolerance=300, now=NOW)
    assert event.id == VALID["expect"]["event"]["id"]
    with pytest.raises(WebhookVerificationError) as info:  # the public function, wall clock
        verify_webhook(VALID_BODY, message, SECRET)
    assert info.value.reason == "timestamp_too_old"


def test_two_different_ids_in_an_http_message_are_missing_header() -> None:
    pairs = [(name, value) for name, value in VALID["headers"]] + [("webhook-id", "another-id")]
    with pytest.raises(WebhookVerificationError) as info:
        _verify_at(VALID_BODY, http_message(pairs), SECRET, tolerance=300, now=NOW)
    assert info.value.reason == "missing_header"


def test_non_str_header_names_and_values_are_ignored() -> None:
    headers: dict[Any, Any] = dict(VALID_HEADERS)
    headers[7] = "x"
    headers["webhook-id-extra"] = None
    headers["x-number"] = 5
    assert verify_valid(headers=headers).id == VALID["expect"]["event"]["id"]


def test_a_lone_surrogate_in_the_id_or_a_str_body_is_a_signature_mismatch() -> None:
    headers = dict(VALID_HEADERS) | {"webhook-id": "id-\ud800"}
    with pytest.raises(WebhookVerificationError) as info:
        verify_valid(headers=headers)
    assert info.value.reason == "signature_mismatch"
    with pytest.raises(WebhookVerificationError) as info:
        verify_valid(body=VALID_BODY.decode("utf-8") + "\udc80")
    assert info.value.reason == "signature_mismatch"


def test_the_returned_event_type_and_status_use_the_exported_constants() -> None:
    event = verify_valid()
    assert event.type == docql.EVENT_JOB_SUCCEEDED == "job.succeeded"
    assert event.status == docql.JOB_STATUS_SUCCEEDED
    assert docql.EVENT_JOB_FAILED == "job.failed"
    assert docql.EVENT_JOB_CANCELLED == "job.cancelled"
    assert docql.EVENT_WEBHOOK_TEST == "webhook.test"


_ODD_TEXT = [" ", "\t", "\n", "\r", "\x00", "é", "ไทย", " ", "K", "ſ", "\ud800", "١"]


def _mutate_text(rng: random.Random, text: str) -> str:
    choice = rng.randrange(5)
    if choice == 0 or not text:
        return text + rng.choice(_ODD_TEXT)
    position = rng.randrange(len(text))
    if choice == 1:
        return text[:position] + rng.choice(_ODD_TEXT) + text[position + 1 :]
    if choice == 2:
        return text[:position]
    if choice == 3:
        return "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in text)
    return text[:position] + chr(rng.randrange(32, 127)) + text[position + 1 :]


def _mutate(rng: random.Random) -> tuple[bytes | str, dict[Any, Any]]:
    body: bytes | str = VALID_BODY
    pairs: list[tuple[Any, Any]] = list(VALID["headers"])
    for _ in range(rng.randint(1, 3)):
        kind = rng.randrange(8)
        if kind == 0 and isinstance(body, bytes) and body:
            position = rng.randrange(len(body))
            flipped = body[position] ^ rng.randint(1, 255)
            body = body[:position] + bytes([flipped]) + body[position + 1 :]
        elif kind == 1 and pairs:
            pairs.pop(rng.randrange(len(pairs)))
        elif kind == 2 and pairs:
            name, value = rng.choice(pairs)
            pairs.append((_mutate_text(rng, name), value))
        elif kind == 3 and pairs:
            index = rng.randrange(len(pairs))
            name, value = pairs[index]
            pairs[index] = (_mutate_text(rng, name), value)
        elif kind == 4 and pairs:
            index = rng.randrange(len(pairs))
            name, value = pairs[index]
            pairs[index] = (name, _mutate_text(rng, value))
        elif kind == 5:
            raw = body if isinstance(body, bytes) else body.encode("utf-8", "surrogatepass")
            body = raw.decode("utf-8", "surrogateescape")
        elif kind == 6:
            pairs.append((rng.choice(["webhook-signature", "Webhook-Id", "webhook-timestamp"]), ""))
        else:
            garbage = bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
            raw = body if isinstance(body, bytes) else body.encode("utf-8", "surrogatepass")
            body = raw + garbage
    return body, dict(pairs)


def test_request_data_only_ever_gives_an_event_or_a_verification_error() -> None:
    rng = random.Random(451)
    outcomes = {"event": 0, "error": 0}
    for _ in range(2000):
        body, headers = _mutate(rng)
        try:
            result = _verify_at(body, headers, [SECRET], tolerance=300, now=NOW)
        except WebhookVerificationError as err:
            assert err.reason in REASONS
            outcomes["error"] += 1
        else:
            assert isinstance(result, WebhookEvent)
            outcomes["event"] += 1
    assert outcomes["error"] > 1000
    assert outcomes["event"] > 0

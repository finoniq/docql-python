"""The shared console webhook vectors (D-16): sha256 pin, exact count, every case.

``testdata/webhook-vectors.json`` is a byte copy of docql-console
``packages/webhooks/webhook-vectors.json`` (version 1, frozen in 45.1-01). It was
made by the console's real signer and payload builders, so these cases prove the
helper agrees with what the console sends. Every case runs through the internal
``_verify_at`` seam with the case's ``now``; event cases run again with the body
as a ``str``.
"""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from docql import WebhookEvent, WebhookVerificationError
from docql._webhooks import _verify_at

ROOT = Path(__file__).resolve().parents[2]
VECTORS_PATH = ROOT / "testdata" / "webhook-vectors.json"

# Pinned from 45.1-01-SUMMARY.md. A regenerated console file changes both values,
# and the copy here must then be replaced byte for byte.
VECTORS_SHA256 = "63bbaeb4063ebcbd98fd4535121d29333589efddc85a6a9c6eb5419e7b186bbd"
VECTORS_CASE_COUNT = 123

_VECTORS: dict[str, Any] = json.loads(VECTORS_PATH.read_bytes())
CASES: list[dict[str, Any]] = _VECTORS["cases"]
EVENT_CASES = [case for case in CASES if case["expect"]["result"] == "event"]
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _headers(case: dict[str, Any]) -> dict[str, str]:
    # Header names never repeat exactly inside one case, so a dict is exact.
    return {name: value for name, value in case["headers"]}


def _run(case: dict[str, Any], body: bytes | str) -> WebhookEvent:
    return _verify_at(
        body,
        _headers(case),
        case["secrets"],
        tolerance=case["tolerance_seconds"],
        now=case["now"],
    )


def _body(case: dict[str, Any]) -> bytes:
    return base64.b64decode(case["body_base64"], validate=True)


def _windows(text: str, size: int = 6) -> set[str]:
    return {text[i : i + size] for i in range(len(text) - size + 1)}


def _assert_no_secret_echo(message: str, secrets: list[str]) -> None:
    for secret in secrets:
        base64_part = secret.split("whsec_", 1)[-1]
        for window in _windows(base64_part):
            assert window not in message, "the message carries a piece of a secret"


def _assert_no_signature_echo(message: str, case: dict[str, Any]) -> None:
    for name, value in case["headers"]:
        if name.lower() != "webhook-signature":
            continue
        for entry in value.split(" "):
            for window in _windows(entry.removeprefix("v1,")):
                assert window not in message, "the message carries a piece of a signature"


def _check_event(event: WebhookEvent, want: dict[str, Any]) -> None:
    assert isinstance(event, WebhookEvent)
    assert event.id == want["id"]
    assert event.type == want["type"]
    assert event.timestamp.utcoffset() == timedelta(0)
    assert (event.timestamp - _EPOCH) // timedelta(milliseconds=1) == want["timestamp_epoch_ms"]
    assert event.job_id == want["job_id"]
    assert event.status == want["status"]
    assert event.data == want["data"]


def test_vector_file_pin() -> None:
    raw = VECTORS_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == VECTORS_SHA256
    assert _VECTORS["version"] == 1
    assert len(CASES) == VECTORS_CASE_COUNT
    assert len({case["id"] for case in CASES}) == VECTORS_CASE_COUNT


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_vector(case: dict[str, Any]) -> None:
    expect = case["expect"]
    if expect["result"] == "event":
        _check_event(_run(case, _body(case)), expect["event"])
    elif expect["result"] == "webhook_error":
        with pytest.raises(WebhookVerificationError) as info:
            _run(case, _body(case))
        assert info.value.reason == expect["reason"]
        for text in (str(info.value), repr(info.value)):
            _assert_no_secret_echo(text, case["secrets"])
            _assert_no_signature_echo(text, case)
    elif expect["result"] == "validation_error":
        # D-26: tolerance 0 and -1 are ValueError, like every malformed secret.
        with pytest.raises(ValueError) as error:
            _run(case, _body(case))
        message = str(error.value)
        if expect["argument"] == "tolerance":
            assert "tolerance" in message
        else:
            assert "secret" in message
        if "secret_index" in expect:
            assert f"secret {expect['secret_index']}" in message
        _assert_no_secret_echo(message, case["secrets"])
    else:
        pytest.fail(f"unknown expect.result {expect['result']!r}")


@pytest.mark.parametrize("case", EVENT_CASES, ids=[case["id"] for case in EVENT_CASES])
def test_vector_str_body(case: dict[str, Any]) -> None:
    _check_event(_run(case, _body(case).decode("utf-8")), case["expect"]["event"])

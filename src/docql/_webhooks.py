"""Verify a DocQL webhook delivery (D-06..D-16, D-25..D-29).

A receiver has no API key, so the helper is a standalone function, never a
client method. It checks, in this fixed order: the tolerance, every secret, the
body type, the presence of the three headers, the timestamp syntax, the ``v1``
signature entries, the HMAC, the tolerance window and finally the event body.
Request data (the body and the header names and values) can only produce a
:class:`WebhookEvent` or a :class:`WebhookVerificationError`.
"""

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from docql._errors import DocQLException

EVENT_JOB_SUCCEEDED = "job.succeeded"
EVENT_JOB_FAILED = "job.failed"
EVENT_JOB_CANCELLED = "job.cancelled"
EVENT_WEBHOOK_TEST = "webhook.test"

DEFAULT_TOLERANCE = 300

_SECRET_PREFIX = "whsec_"
_SECRET_MIN_BYTES = 24
_SECRET_MAX_BYTES = 64
_SIGNATURE_BYTES = 32
_TIMESTAMP_MAX_DIGITS = 12
# D-29: exactly space, tab, LF and CR; never the Unicode-wide default strip set.
_SECRET_TRIM = " \t\n\r"

_HEADER_ID = "webhook-id"
_HEADER_TIMESTAMP = "webhook-timestamp"
_HEADER_SIGNATURE = "webhook-signature"

# D-14: the seven reasons and the one fixed sentence each carries. These names
# never start with REASON_, which is the API error namespace (D-28).
_WEBHOOK_MESSAGES = {
    "missing_header": (
        "a webhook-id, webhook-timestamp or webhook-signature header is absent, "
        "empty, or repeated with different values"
    ),
    "invalid_timestamp": "the webhook-timestamp header is not 1 to 12 ASCII digits",
    "invalid_signature_header": "the webhook-signature header has no well-formed v1 signature",
    "signature_mismatch": "no v1 signature matches any secret (wrong secret or a changed body)",
    "timestamp_too_old": "the webhook-timestamp is older than the tolerance allows",
    "timestamp_too_new": "the webhook-timestamp is further in the future than the tolerance allows",
    "invalid_payload": "the signed body is not a DocQL webhook event",
}
_WEBHOOK_REASONS = frozenset(_WEBHOOK_MESSAGES)

_EVENT_TIMESTAMP = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z", re.ASCII
)


@dataclass(frozen=True)
class WebhookEvent:
    """A verified DocQL webhook event (D-08, D-25).

    ``type`` is an open string: compare it with ``EVENT_JOB_SUCCEEDED``,
    ``EVENT_JOB_FAILED``, ``EVENT_JOB_CANCELLED`` or ``EVENT_WEBHOOK_TEST``,
    and treat any other value as an event type this SDK does not know yet.
    ``timestamp`` is an aware UTC datetime. ``job_id`` and ``status`` are
    ``data["job_id"]`` and ``data["status"]`` when they are strings, else None
    (a ``webhook.test`` event has ``data == {}``). ``data`` is the event's raw
    ``data`` object, unknown keys included; it is never None.
    """

    id: str
    type: str
    timestamp: datetime
    job_id: str | None
    status: str | None
    data: dict[str, Any]


class WebhookVerificationError(DocQLException):
    """The delivery failed verification; do not act on it (D-13, D-14).

    ``reason`` is one of ``missing_header``, ``invalid_timestamp``,
    ``invalid_signature_header``, ``signature_mismatch``,
    ``timestamp_too_old``, ``timestamp_too_new`` or ``invalid_payload``.
    Neither :func:`str` nor :func:`repr` ever holds anything but the reason and
    a fixed sentence: never a secret, the expected signature or a received
    signature. This is not a :class:`DocQLError`, because no API call failed.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {_WEBHOOK_MESSAGES.get(reason, 'verification failed')}")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(reason={self.reason!r})"

    def __reduce__(self) -> tuple[Callable[[str], "WebhookVerificationError"], tuple[str]]:
        return _rebuild_webhook_error, (self.reason,)


def _rebuild_webhook_error(reason: str) -> WebhookVerificationError:
    return WebhookVerificationError(reason)


def verify_webhook(
    body: bytes | str,
    headers: Mapping[str, str],
    secrets: str | Sequence[str],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> WebhookEvent:
    """Verify one DocQL webhook delivery and return its event.

    Pass the raw request body exactly as received, before any JSON parsing (a
    ``str`` is UTF-8 encoded), the request headers (names match without regard
    to ASCII case) and the endpoint's webhook secret, or a list of secrets
    while a rotation overlaps. ``tolerance`` is the allowed clock skew in
    seconds either way (default 300, inclusive). The helper does not detect
    replays: dedupe on ``event.id``, which stays the same across retries.

    Raises:
        TypeError: ``tolerance``, ``secrets`` or ``body`` has the wrong type.
        ValueError: ``tolerance`` is not a positive finite number, the secret
            list is empty, or a secret is not ``whsec_`` plus canonical base64
            of 24 to 64 bytes. The message names the position, never the secret.
        WebhookVerificationError: the delivery is not an authentic, timely
            DocQL webhook event; ``reason`` says why.
    """
    return _verify_at(body, headers, secrets, tolerance=tolerance, now=int(time.time()))


def _verify_at(
    body: bytes | str,
    headers: Mapping[str, str],
    secrets: str | Sequence[str],
    *,
    tolerance: float,
    now: int,
) -> WebhookEvent:
    """:func:`verify_webhook` against a given clock: ``now`` in whole unix seconds (D-12)."""
    _check_tolerance(tolerance)
    keys = _decode_secrets(secrets)
    if not isinstance(body, (bytes, str)):
        raise TypeError(f"body must be bytes or str, not {type(body).__name__!r}")
    if not callable(getattr(headers, "items", None)):
        raise TypeError(f"headers must be a mapping, not {type(headers).__name__!r}")

    msg_id, timestamp, signature = _read_headers(headers)
    if not (
        1 <= len(timestamp) <= _TIMESTAMP_MAX_DIGITS and timestamp.isascii() and timestamp.isdigit()
    ):
        raise WebhookVerificationError("invalid_timestamp")
    entries = _signature_entries(signature)
    if not entries:
        raise WebhookVerificationError("invalid_signature_header")

    try:
        body_bytes = body.encode("utf-8") if isinstance(body, str) else body
        content = msg_id.encode("utf-8") + b"." + timestamp.encode("ascii") + b"." + body_bytes
    except UnicodeEncodeError:
        # A lone surrogate cannot have been signed by the console (D-29 notes).
        raise WebhookVerificationError("signature_mismatch") from None
    if not _any_match(keys, content, entries):
        raise WebhookVerificationError("signature_mismatch")

    skew = now - int(timestamp)
    if skew > tolerance:
        raise WebhookVerificationError("timestamp_too_old")
    if -skew > tolerance:
        raise WebhookVerificationError("timestamp_too_new")

    return _parse_event(body_bytes)


def _check_tolerance(tolerance: Any) -> None:
    # The client's timeout rule: bool is an int subclass but never a number here.
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
        raise TypeError(
            f"webhook tolerance must be a number of seconds, not {type(tolerance).__name__!r}"
        )
    if isinstance(tolerance, float) and not math.isfinite(tolerance):
        raise ValueError("webhook tolerance must be a finite number of seconds")
    if tolerance <= 0:
        raise ValueError("webhook tolerance must be greater than zero")


def _decode_secrets(secrets: Any) -> list[bytes]:
    if isinstance(secrets, str):
        items: Sequence[Any] = [secrets]
    elif isinstance(secrets, Sequence) and not isinstance(secrets, (bytes, bytearray)):
        items = secrets
    else:
        raise TypeError(
            f"webhook secrets must be a str or a sequence of str, not {type(secrets).__name__!r}"
        )
    if len(items) == 0:
        raise ValueError("no webhook secrets: pass at least one webhook secret")
    keys = []
    for position, item in enumerate(items, start=1):
        if not isinstance(item, str):
            raise TypeError(
                f"webhook secret {position}: must be a str, not {type(item).__name__!r}"
            )
        keys.append(_decode_secret(position, item))
    return keys


def _decode_secret(position: int, secret: str) -> bytes:
    text = secret.strip(_SECRET_TRIM)
    if not text.startswith(_SECRET_PREFIX):
        raise ValueError(f"webhook secret {position}: does not start with {_SECRET_PREFIX}")
    raw = _canonical_base64(text[len(_SECRET_PREFIX) :])
    if raw is None:
        raise ValueError(f"webhook secret {position}: is not canonical padded base64")
    if not _SECRET_MIN_BYTES <= len(raw) <= _SECRET_MAX_BYTES:
        raise ValueError(
            f"webhook secret {position}: must decode to {_SECRET_MIN_BYTES} to "
            f"{_SECRET_MAX_BYTES} bytes"
        )
    return raw


def _canonical_base64(text: str) -> bytes | None:
    """The decoded bytes when ``text`` is canonical padded standard base64, else None."""
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    # b64decode accepts non-zero trailing bits; the re-encode check refuses them.
    if base64.b64encode(raw).decode("ascii") != text:
        return None
    return raw


def _read_headers(headers: Mapping[str, str]) -> tuple[str, str, str]:
    found: dict[str, list[str]] = {_HEADER_ID: [], _HEADER_TIMESTAMP: [], _HEADER_SIGNATURE: []}
    for key, value in headers.items():
        # ASCII-only case fold (D-29): a look-alike such as U+212A never matches.
        if not isinstance(key, str) or not isinstance(value, str) or not key.isascii():
            continue
        values = found.get(key.lower())
        if values is not None and value != "":
            values.append(value)
    if not all(found.values()):
        raise WebhookVerificationError("missing_header")
    ids = set(found[_HEADER_ID])
    timestamps = set(found[_HEADER_TIMESTAMP])
    if len(ids) != 1 or len(timestamps) != 1:
        raise WebhookVerificationError("missing_header")
    return ids.pop(), timestamps.pop(), " ".join(found[_HEADER_SIGNATURE])


def _signature_entries(signature: str) -> list[bytes]:
    entries = []
    # Split on U+0020 only: a tab or newline stays inside a piece and spoils it.
    for piece in signature.split(" "):
        if not piece.startswith("v1,"):
            continue
        raw = _canonical_base64(piece[3:])
        if raw is not None and len(raw) == _SIGNATURE_BYTES:
            entries.append(raw)
    return entries


def _any_match(keys: list[bytes], content: bytes, entries: list[bytes]) -> bool:
    for key in keys:
        expected = hmac.new(key, content, hashlib.sha256).digest()
        for entry in entries:
            if hmac.compare_digest(expected, entry):
                return True
    return False


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _parse_event(body: bytes) -> WebhookEvent:
    try:
        doc: Any = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise WebhookVerificationError("invalid_payload") from None
    if not isinstance(doc, dict):
        raise WebhookVerificationError("invalid_payload")
    event_id = doc.get("id")
    event_type = doc.get("type")
    data = doc.get("data")
    if not isinstance(event_id, str) or not event_id:
        raise WebhookVerificationError("invalid_payload")
    if not isinstance(event_type, str) or not event_type:
        raise WebhookVerificationError("invalid_payload")
    if not isinstance(data, dict):
        raise WebhookVerificationError("invalid_payload")
    job_id = data.get("job_id")
    status = data.get("status")
    return WebhookEvent(
        id=event_id,
        type=event_type,
        timestamp=_parse_timestamp(doc.get("timestamp")),
        job_id=job_id if isinstance(job_id, str) else None,
        status=status if isinstance(status, str) else None,
        data=data,
    )


def _parse_timestamp(value: Any) -> datetime:
    """``YYYY-MM-DDTHH:MM:SS[.fraction]Z`` as an aware UTC datetime.

    The fraction keeps at most microseconds (truncated). The lenient ISO parser
    is never used alone: it also takes dates without a time, offsets and
    compact forms.
    """
    match = _EVENT_TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise WebhookVerificationError("invalid_payload")
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    fraction = match.group(7) or ""
    try:
        # datetime itself range-checks month, day (February 30), hour 0-23,
        # minute 0-59 and second 0-59 (no leap second).
        return datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            int(fraction[:6].ljust(6, "0")),
            tzinfo=UTC,
        )
    except ValueError:
        raise WebhookVerificationError("invalid_payload") from None

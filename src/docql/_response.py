"""Transport-independent response decoding (D-04, D-10..D-12).

Both functions take ``(status, headers, body_bytes)`` so the same decode path
serves the sync client, raw contract replays and a future async client.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from docql._constants import ERROR_BODY_KEEP_CHARS
from docql._errors import DocQLError


@dataclass(frozen=True)
class QueryResult:
    """A successful query answer (D-04). Unknown fields pass through."""

    data: dict[str, Any]
    request_id: str
    page_count: int | None
    pages_ocr: int | None
    metadata: dict[str, Any]


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _int_or_none(value: Any) -> int | None:
    # bool is an int subclass; the contract counts exact integers only.
    return value if type(value) is int else None


def _scrub_and_truncate(text: str, api_key: str) -> str:
    """Replace every occurrence of the key, then keep the first chars (D-11).

    Scrubbing before truncating means a key straddling the cut can never
    survive as a fragment.
    """
    if api_key:
        text = text.replace(api_key, "***")
    return text[:ERROR_BODY_KEEP_CHARS]


def parse_retry_after(value: str | None, now: datetime | None = None) -> int | None:
    """``Retry-After`` as whole seconds, or None when absent/unparsable.

    Delta-seconds pass through as an int. An HTTP-date is converted to the
    seconds from ``now`` (default: current UTC time) rounded up with
    :func:`math.ceil` and clamped at 0, so a date in the past means "retry
    now" rather than a negative sleep.
    """
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        return int(text)
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        # An RFC 5322 "-0000" zone means "unknown local time"; assume UTC.
        parsed = parsed.replace(tzinfo=UTC)
    reference = now if now is not None else datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return max(0, math.ceil((parsed - reference).total_seconds()))


def _unusable_result(
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    api_key: str,
    message: str,
) -> DocQLError:
    """The error for a 2xx body the SDK cannot decode (research OQ 3/4)."""
    return DocQLError(
        status=status,
        reason=None,
        request_id=_header(headers, "x-request-id"),
        retry_after=None,
        retryable=False,
        message=message,
        body=_scrub_and_truncate(body.decode("utf-8", "replace"), api_key),
    )


def parse_result(
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    api_key: str,
) -> QueryResult:
    """Decode a 2xx DocQL envelope into a :class:`QueryResult`.

    A body that is not a JSON object, or whose ``data`` is present, non-null
    and not an object, is unusable: it raises :class:`DocQLError` naming the
    problem instead of silently answering with empty data. ``api_key`` scrubs
    the body stored on that error, symmetric with :func:`decode_error`.
    """
    try:
        doc: Any = json.loads(body)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise _unusable_result(status, headers, body, api_key, "unexpected non-JSON response")
    if "data" in doc and doc["data"] is not None and not isinstance(doc["data"], dict):
        raise _unusable_result(
            status, headers, body, api_key, "unexpected response: data is not a JSON object"
        )
    data = doc.get("data")
    if not isinstance(data, dict):
        data = {}
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    request_id = metadata.get("request_id")
    if not isinstance(request_id, str):
        request_id = _header(headers, "x-request-id") or ""
    return QueryResult(
        data=data,
        request_id=request_id,
        page_count=_int_or_none(metadata.get("page_count")),
        pages_ocr=_int_or_none(metadata.get("pages_ocr")),
        metadata=metadata,
    )


def decode_error(
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    api_key: str,
) -> DocQLError:
    """Decode a non-2xx response into a :class:`DocQLError`.

    Classification is by status, never by body shape. An envelope is present
    only when the JSON body is a dict whose ``metadata`` is a dict with a str
    ``reason``. Neither the stored ``body`` nor ``message`` ever contains the
    API key, even when the server echoes it back.
    """
    reason: str | None = None
    message: str | None = None
    request_id: str | None = None
    try:
        doc: Any = json.loads(body)
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        metadata = doc.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("reason"), str):
            reason = metadata["reason"]
            error_info = doc.get("error_info")
            if isinstance(error_info, str):
                message = error_info
            raw_id = metadata.get("request_id")
            if isinstance(raw_id, str):
                request_id = raw_id
    if request_id is None:
        request_id = _header(headers, "x-request-id")

    retry_after = parse_retry_after(_header(headers, "retry-after"))

    if message is not None and api_key:
        message = message.replace(api_key, "***")

    return DocQLError(
        status=status,
        reason=reason,
        request_id=request_id,
        retry_after=retry_after,
        retryable=status in (429, 503),
        message=message,
        body=_scrub_and_truncate(body.decode("utf-8", "replace"), api_key),
    )

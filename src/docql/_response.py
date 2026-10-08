"""Transport-independent response decoding (D-04, D-10..D-12).

Both functions take ``(status, headers, body_bytes)`` so the same decode path
serves the sync client, raw contract replays and a future async client.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
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


def parse_result(
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    api_key: str,
) -> QueryResult:
    """Decode a 2xx DocQL envelope into a :class:`QueryResult`.

    ``api_key`` is accepted for symmetry with :func:`decode_error`; it only
    matters when a later release raises on an unusable 2xx body.
    """
    try:
        doc: Any = json.loads(body)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        doc = {}
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
    ``reason``. The stored ``body`` never contains the API key.
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

    retry_header = _header(headers, "retry-after")
    retry_after = (
        int(retry_header)
        if retry_header is not None and retry_header.isascii() and retry_header.isdigit()
        else None
    )

    text = body.decode("utf-8", "replace")
    if api_key:
        text = text.replace(api_key, "***")

    return DocQLError(
        status=status,
        reason=reason,
        request_id=request_id,
        retry_after=retry_after,
        retryable=status in (429, 503),
        message=message,
        body=text[:ERROR_BODY_KEEP_CHARS],
    )

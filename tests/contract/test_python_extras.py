"""Python-only live checks against the digest-pinned stub (SC1-SC3, D-13/D-14/D-16).

Every test uses its own query string so the stub's extraction cache can never
mask a difference between two calls. No assertion reads the stub's
extraction-state or stub-prefixed metadata: completed and cached answers both
pass.
"""

import base64
import json
import logging
from pathlib import Path
from typing import Any, BinaryIO

import httpx
import pytest

import docql
from docql import DocQL, DocQLError, DocQLTimeoutError

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
GOLDEN: dict[str, Any] = json.loads((ROOT / "contract" / "golden.json").read_bytes())
CASES: dict[str, Any] = json.loads((ROOT / "contract" / "sdk-cases.json").read_bytes())
OK_PDF: bytes = base64.b64decode(
    next(c["fileBase64"] for c in GOLDEN["cases"] if c["id"] == "ascii-query")
)
VALID_KEY: str = CASES["keys"]["valid"]


class _NonSeekableSource:
    """A read-only binary source without seek/tell: length unknown (D-16)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
        else:
            chunk = self._data[self._pos : self._pos + size]
            self._pos += len(chunk)
        return chunk


def test_sdk_timeout_on_delay_3000_raises_docql_timeout(stub_url: str) -> None:
    """D-02/SC1: a 1 s SDK timeout cuts the stub's 3 s filename delay."""
    with DocQL(api_key=VALID_KEY, api_url=stub_url, timeout=1.0) as client:
        with pytest.raises(DocQLTimeoutError):
            client.query_document(OK_PDF, filename="delay-3000.pdf", query="extras: timeout")


def test_injected_client_default_timeout_cannot_shorten_the_sdk_default(stub_url: str) -> None:
    """SC1: the SDK applies its own timeout to every request, injected client or not.

    The injected client's 1 s default would cut the stub's 2 s delay; the 200
    proves the per-request SDK timeout (660 s read) won.
    """
    with httpx.Client(timeout=1.0) as injected:
        with DocQL(api_key=VALID_KEY, api_url=stub_url, http_client=injected) as client:
            result = client.query_document(
                OK_PDF, filename="delay-2000.pdf", query="extras: injected timeout"
            )
    assert result.page_count == 1
    assert result.request_id != ""


def test_injected_client_default_headers_never_reach_the_server(stub_url: str) -> None:
    """SC2: X-Request-Id and x-docql-* client defaults are dropped, not forwarded.

    The stub adopts an inbound X-Request-Id verbatim and rejects x-docql-*
    headers, so the 200 and the server-minted id are both live proofs of the
    scrub.
    """
    with httpx.Client(
        headers={"X-Request-Id": "client-chosen-id", "X-DocQL-Tenant": "t"}
    ) as injected:
        with DocQL(api_key=VALID_KEY, api_url=stub_url, http_client=injected) as client:
            result = client.query_document(OK_PDF, filename="invoice.pdf", query="extras: scrub")
    assert result.request_id not in ("", "client-chosen-id")


def test_path_input_reports_the_filename_page_knobs(stub_url: str, tmp_path: Path) -> None:
    """D-13/D-14: a Path input is uploaded under its basename, knobs included."""
    document = tmp_path / "pages-2_ocr-1.pdf"
    document.write_bytes(OK_PDF)
    with DocQL(api_key=VALID_KEY, api_url=stub_url) as client:
        result = client.query_document(document, query="extras: path input")
    assert result.page_count == 2
    assert result.pages_ocr == 1


def test_positioned_file_object_is_read_from_its_current_position(
    stub_url: str, tmp_path: Path
) -> None:
    """D-16: a caller's file object is read from its position and never closed."""
    document = tmp_path / "positioned.pdf"
    document.write_bytes(b"JUNKJOK" + OK_PDF)
    handle: BinaryIO = document.open("rb")
    try:
        handle.seek(7)
        with DocQL(api_key=VALID_KEY, api_url=stub_url) as client:
            result = client.query_document(handle, query="extras: positioned file object")
        assert result.page_count == 1
        assert not handle.closed
    finally:
        handle.close()


def test_non_seekable_read_only_stream_succeeds(stub_url: str) -> None:
    """D-16: a stream whose length cannot be measured is sent chunked and still works."""
    source: BinaryIO = _NonSeekableSource(OK_PDF)  # type: ignore[assignment]
    with DocQL(api_key=VALID_KEY, api_url=stub_url) as client:
        result = client.query_document(
            source, filename="invoice.pdf", query="extras: non-seekable stream"
        )
    assert result.page_count == 1


def test_oversized_path_upload_answers_upload_too_large(stub_url: str, tmp_path: Path) -> None:
    """The stub's 64 KiB cap answers 413 upload_too_large for a 128 KiB path upload."""
    big = tmp_path / "big.pdf"
    big.write_bytes(b"A" * 131072)
    with DocQL(api_key=VALID_KEY, api_url=stub_url) as client:
        with pytest.raises(DocQLError) as excinfo:
            client.query_document(str(big), query="extras: oversize")
    err = excinfo.value
    assert err.status == 413
    assert err.reason == docql.REASON_UPLOAD_TOO_LARGE


def test_debug_transport_logs_never_contain_the_api_key(
    stub_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """T-11-19: a live call leaves no trace of the key in httpx/httpcore DEBUG logs."""
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="httpcore")
    with DocQL(api_key=VALID_KEY, api_url=stub_url) as client:
        result = client.query_document(OK_PDF, filename="invoice.pdf", query="extras: logs")
    assert result.request_id != ""
    for record in caplog.records:
        if record.name.split(".", 1)[0] in ("httpx", "httpcore"):
            assert VALID_KEY not in record.getMessage(), record.name

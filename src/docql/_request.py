"""Transport-independent multipart request building.

The wire format is byte-exact with the golden fixtures (D-01..D-03 of the
shared contract): part order ``file`` then ``body``, only the double quote,
CR and LF escaped in the filename, compact canonical UTF-8 JSON for the body
part, CRLF line endings everywhere.

httpx's own multipart encoder is deliberately not used: it emits ``data``
fields before ``files``, escapes a different character set and rewinds the
caller's file object. The framing here is built by hand; everything stateful
(TLS, pooling, timeouts) stays in the transport.
"""

import json
import secrets
from collections.abc import Iterator
from types import TracebackType
from typing import Self

_CHUNK_BYTES = 65536


def new_boundary() -> str:
    """Return a fresh random multipart boundary (tests monkeypatch this)."""
    return secrets.token_hex(16)


def escape_filename(name: str) -> str:
    """Escape only ``"``, CR and LF in a form filename (D-02 of the contract)."""
    return name.replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A")


def body_json(query: str | None, prompt: str | None, mode: str | None) -> bytes:
    """Encode the body part JSON: query or prompt first, params only when set."""
    doc: dict[str, object] = {}
    if query is not None:
        doc["query"] = query
    if prompt is not None:
        doc["prompt"] = prompt
    if mode is not None:
        doc["params"] = {"mode": mode}
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def frame(filename: str, body: bytes, boundary: str) -> tuple[bytes, bytes]:
    """Return the (preamble, epilogue) framing around the file bytes."""
    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; '
        f'filename="{escape_filename(filename)}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    epilogue = (
        (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="body"\r\n\r\n').encode()
        + body
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return preamble, epilogue


class Upload:
    """A framed multipart upload, streamable as request content (D-09 wire rules).

    ``content_length`` is the exact byte count ``iter_bytes`` yields, so the
    transport sends a ``Content-Length`` header and never chunked framing.
    """

    def __init__(self, filename: str, file_bytes: bytes, body: bytes, boundary: str) -> None:
        self._preamble, self._epilogue = frame(filename, body, boundary)
        self._file_bytes = file_bytes
        self.content_type = f"multipart/form-data; boundary={boundary}"
        self.file_size: int | None = len(file_bytes)
        self.content_length: int | None = (
            len(self._preamble) + len(file_bytes) + len(self._epilogue)
        )

    def iter_bytes(self) -> Iterator[bytes]:
        """Yield the preamble, the file bytes in bounded chunks, the epilogue."""
        yield self._preamble
        for start in range(0, len(self._file_bytes), _CHUNK_BYTES):
            yield self._file_bytes[start : start + _CHUNK_BYTES]
        yield self._epilogue

    def close(self) -> None:
        """Release underlying resources (none for bytes input)."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def prepare_upload(
    file: bytes,
    *,
    query: str | None,
    prompt: str | None,
    mode: str | None,
    filename: str | None,
) -> Upload:
    """Validate the call and frame the upload for one query_document call (D-14/D-17)."""
    if (query is None) == (prompt is None):
        raise ValueError("pass exactly one of query= or prompt=")
    if not isinstance(file, bytes):
        raise TypeError(
            f"unsupported file input of type {type(file).__name__!r}: "
            "this pre-release build accepts bytes; path and file-object "
            "inputs arrive with the full release"
        )
    if filename is None:
        raise ValueError("bytes input needs filename= to name the file part")
    boundary = new_boundary()
    return Upload(filename, file, body_json(query, prompt, mode), boundary)

"""Transport-independent multipart request building.

The wire format is byte-exact with the golden fixtures (D-01..D-03 of the
shared contract): part order ``file`` then ``body``, only the double quote,
CR and LF escaped in the filename, compact canonical UTF-8 JSON for the body
part, CRLF line endings everywhere.

httpx's own multipart encoder is deliberately not used: it emits ``data``
fields before ``files``, escapes a different character set and rewinds the
caller's file object. The framing here is built by hand; everything stateful
(TLS, pooling, timeouts) stays in the transport.

File inputs (D-13): ``str`` and ``os.PathLike`` mean a path, ``bytes`` mean
contents, any other object is a binary file object only when its ``read()``
returns ``bytes``. A caller's file object is read from its current position
and never closed (D-16); a file the SDK opened from a path is closed by
:class:`Upload`. Validation completes before a path is opened or a byte is
sent.
"""

import builtins
import io
import json
import os
import secrets
from collections.abc import Iterator
from types import TracebackType
from typing import BinaryIO, Self

from docql._constants import CHUNK_SIZE

TYPE_ERROR_TEXT = "file must be a path (str or os.PathLike), bytes, or a binary file object"


class _ShortReadError(Exception):
    """A measured file yielded fewer bytes than its declared length."""


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


def open(path: str) -> BinaryIO:
    """Open a path for binary reading (a seam tests wrap to track closes)."""
    # builtins.open because this module-level name IS the seam under test.
    return builtins.open(path, "rb")


def _is_binary_stream(obj: object) -> bool:
    """True for a readable binary file object (D-15): callable read() returning bytes."""
    if isinstance(obj, io.TextIOBase):
        return False
    read = getattr(obj, "read", None)
    if not callable(read):
        return False
    try:
        probe = read(0)
    except Exception:
        return False
    return isinstance(probe, bytes)


def _remaining_length(stream: BinaryIO) -> int | None:
    """Bytes left in a caller's file object, or None when unmeasurable.

    The object is only ever moved relative to its own current position
    (D-16): one tell, one seek to the end and one seek back.
    io.UnsupportedOperation subclasses OSError and ValueError, so catching
    those two covers it.
    """
    try:
        seekable = getattr(stream, "seekable", None)
        if seekable is not None and not seekable():
            return None
        pos = stream.tell()
        end = stream.seek(0, os.SEEK_END)
        stream.seek(pos)
    except (AttributeError, OSError, ValueError):
        return None
    if not isinstance(end, int):
        return None
    return end - pos


class Upload:
    """A framed multipart upload, streamable as request content (D-09 wire rules).

    ``content_length`` is the exact byte count ``iter_bytes`` yields when the
    file length is known, so the transport sends a ``Content-Length`` header
    and never chunked framing; ``None`` means the length could not be
    measured and the transport falls back to chunked encoding.
    """

    def __init__(
        self,
        filename: str,
        body: bytes,
        boundary: str,
        source: bytes | BinaryIO,
        file_size: int | None,
        own_file: BinaryIO | None,
    ) -> None:
        self._preamble, self._epilogue = frame(filename, body, boundary)
        self._source = source
        self._file_size = file_size
        self._own_file = own_file
        self.content_type = f"multipart/form-data; boundary={boundary}"
        self.content_length: int | None = (
            len(self._preamble) + file_size + len(self._epilogue) if file_size is not None else None
        )

    def iter_bytes(self) -> Iterator[bytes]:
        """Yield the preamble, the file bytes in bounded chunks, the epilogue."""
        yield self._preamble
        source = self._source
        if isinstance(source, bytes):
            for start in range(0, len(source), CHUNK_SIZE):
                yield source[start : start + CHUNK_SIZE]
        elif self._file_size is not None:
            # Read exactly the measured length: a file that grew never
            # over-sends, and one that shrank raises instead of undersending.
            left = self._file_size
            while left > 0:
                chunk = source.read(min(CHUNK_SIZE, left))
                if not chunk:
                    raise _ShortReadError(
                        f"short read: {left} of {self._file_size} measured bytes missing"
                    )
                part = chunk[:left]
                yield part
                left -= len(part)
        else:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        yield self._epilogue

    def close(self) -> None:
        """Close a file the SDK opened from a path; never a caller's object (D-16)."""
        if self._own_file is not None:
            self._own_file.close()

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
    file: str | os.PathLike[str] | bytes | BinaryIO,
    *,
    query: str | None,
    prompt: str | None,
    mode: str | None,
    filename: str | None,
) -> Upload:
    """Validate the call, resolve the input kind and frame the upload (D-13..D-17).

    The filename rule (D-14): explicit ``filename`` wins verbatim, then a
    path's basename, then the basename of a ``str`` ``.name``; anything else
    needs ``filename=`` and there is no default filename. Every rule is
    checked before a path is opened or a byte is sent.
    """
    if (query is None) == (prompt is None):
        raise ValueError("pass exactly one of query= or prompt=")

    path: str | None = None
    contents: bytes | None = None
    stream: BinaryIO | None = None
    if isinstance(file, os.PathLike):
        fspath = os.fspath(file)
        if isinstance(fspath, bytes):
            raise TypeError("a bytes path is not supported: pass a str path or os.PathLike[str]")
        path = fspath
    elif isinstance(file, str):
        path = file
    elif isinstance(file, bytes):
        contents = file
    elif isinstance(file, (bytearray, memoryview)):
        raise TypeError(f"pass bytes for the file contents, not {type(file).__name__!r}")
    elif _is_binary_stream(file):
        stream = file
    else:
        raise TypeError(TYPE_ERROR_TEXT)

    if filename is not None:
        part_name = filename
    elif path is not None:
        part_name = os.path.basename(path)
    elif stream is not None:
        object_name = getattr(stream, "name", None)
        if isinstance(object_name, str):
            part_name = os.path.basename(object_name)
        else:
            raise ValueError(
                "a file object without a str .name needs filename= to name the file part"
            )
    else:
        raise ValueError("bytes input needs filename= to name the file part")

    source: bytes | BinaryIO
    own_file: BinaryIO | None = None
    file_size: int | None
    if path is not None:
        opened = open(path)
        own_file = opened
        file_size = os.fstat(opened.fileno()).st_size
        source = opened
    elif contents is not None:
        file_size = len(contents)
        source = contents
    else:
        assert stream is not None
        source = stream
        file_size = _remaining_length(stream)

    return Upload(
        part_name, body_json(query, prompt, mode), new_boundary(), source, file_size, own_file
    )

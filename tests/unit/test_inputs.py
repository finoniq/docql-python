"""File-input rule tests: paths, bytes and binary streams (D-13..D-17)."""

import io
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import httpx
import pytest

import docql._request
from docql import DocQL, DocQLConnectionError

FILE_BYTES = b"%PDF-1.4 unit test document line\n" * 4
BOUNDARY_RE = re.compile(r"[0-9a-f]{32}")


def recording_handler(calls: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    """Capture every request and answer a minimal success envelope."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {}, "metadata": {"request_id": "r1"}})

    return handler


def consuming_handler(calls: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    """Capture the request and read its content inside the handler."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        request.read()
        return httpx.Response(200, json={"data": {}, "metadata": {"request_id": "r1"}})

    return handler


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> DocQL:
    return DocQL(
        api_key="input-test-key",
        api_url="http://stub.test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def boundary_of(request: httpx.Request) -> bytes:
    return request.headers["content-type"].partition("boundary=")[2].encode()


def file_part(request: httpx.Request) -> bytes:
    """The file bytes between the file-part headers and the closing delimiter."""
    raw = request.read()
    start = raw.index(b"\r\n\r\n") + 4
    end = raw.index(b"\r\n--" + boundary_of(request))
    return raw[start:end]


def write_document(tmp_path: Path, name: str = "doc.pdf") -> Path:
    document = tmp_path / name
    document.write_bytes(FILE_BYTES)
    return document


class ReadOnlyStream:
    """A binary stream with read() only: no seek/tell to measure a length."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk, self._data = self._data, b""
        else:
            chunk, self._data = self._data[:size], self._data[size:]
        return chunk


class UnseekableStream:
    """seek/tell exist but seekable() is False, so no length can be measured."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._buffer.tell()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._buffer.seek(offset, whence)


class LyingShortStream:
    """Declares n bytes through seek/tell but read() comes up empty."""

    def __init__(self, declared: int) -> None:
        self._declared = declared

    def read(self, size: int = -1) -> bytes:
        return b""

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return 0

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._declared if whence == os.SEEK_END else offset


class LyingLongStream:
    """Declares n bytes but read() ignores the size and returns everything."""

    def __init__(self, data: bytes, declared: int) -> None:
        self._data = data
        self._declared = declared

    def read(self, size: int = -1) -> bytes:
        if size == 0:  # the type-check probe consumes nothing
            return b""
        chunk, self._data = self._data, b""
        return chunk

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return 0

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._declared if whence == os.SEEK_END else offset


def test_path_input_uses_basename_for_str_and_pathlib(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    document = write_document(tmp_path)
    client.query_document(str(document), query="q")
    client.query_document(document, query="q")
    assert b'filename="doc.pdf"' in calls[0].read()
    assert b'filename="doc.pdf"' in calls[1].read()


def test_explicit_filename_overrides_path_basename(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    document = write_document(tmp_path)
    client.query_document(str(document), filename="x.pdf", query="q")
    assert b'filename="x.pdf"' in calls[0].read()


def test_sdk_opened_file_closed_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[BinaryIO] = []
    real_open = docql._request.open

    def tracking_open(path: str) -> BinaryIO:
        stream = real_open(path)
        opened.append(stream)
        return stream

    monkeypatch.setattr(docql._request, "open", tracking_open)
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    client.query_document(str(write_document(tmp_path)), query="q")
    assert len(opened) == 1
    assert opened[0].closed


def test_sdk_opened_file_closed_when_send_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[BinaryIO] = []
    real_open = docql._request.open

    def tracking_open(path: str) -> BinaryIO:
        stream = real_open(path)
        opened.append(stream)
        return stream

    monkeypatch.setattr(docql._request, "open", tracking_open)

    def refusing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = make_client(refusing_handler)
    with pytest.raises(DocQLConnectionError):
        client.query_document(str(write_document(tmp_path)), query="q")
    assert len(opened) == 1
    assert opened[0].closed


def test_bytes_without_filename_raises_value_error() -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    with pytest.raises(ValueError, match="filename="):
        client.query_document(b"x", query="q")
    assert calls == []


def test_bytesio_without_filename_raises_value_error() -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    with pytest.raises(ValueError, match="filename="):
        client.query_document(io.BytesIO(FILE_BYTES), query="q")
    assert calls == []


def test_file_object_with_non_str_name_raises_value_error() -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))

    class NumberedNameStream:
        def __init__(self) -> None:
            self.name = 7

        def read(self, size: int = -1) -> bytes:
            return b""

    with pytest.raises(ValueError, match="filename="):
        client.query_document(NumberedNameStream(), query="q")  # type: ignore[arg-type]
    assert calls == []


def _text_mode_file(tmp_path: Path) -> object:
    (tmp_path / "doc.txt").write_text("text mode", encoding="utf-8")
    return open(tmp_path / "doc.txt", encoding="utf-8")


def _string_io(tmp_path: Path) -> object:
    return io.StringIO("text mode")


def _an_int(tmp_path: Path) -> object:
    return 42


def _a_list(tmp_path: Path) -> object:
    return [b"x"]


def _a_none(tmp_path: Path) -> object:
    return None


@pytest.mark.parametrize(
    "factory",
    [_text_mode_file, _string_io, _an_int, _a_list, _a_none],
    ids=["text-mode-file", "string-io", "int", "list", "none"],
)
def test_non_binary_input_raises_type_error_before_any_request(
    tmp_path: Path, factory: Callable[[Path], object]
) -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    bad = factory(tmp_path)
    with pytest.raises(TypeError, match="file must be a path"):
        client.query_document(bad, query="q")  # type: ignore[arg-type]
    assert calls == []
    if isinstance(bad, io.IOBase):
        bad.close()


@pytest.mark.parametrize(
    "buffer_like", [bytearray(b"x"), memoryview(b"x")], ids=["bytearray", "memoryview"]
)
def test_buffer_likes_raise_type_error_pointing_at_bytes(buffer_like: object) -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    with pytest.raises(TypeError, match="pass bytes"):
        client.query_document(buffer_like, query="q")  # type: ignore[arg-type]
    assert calls == []


def test_reads_from_current_position_and_never_closes(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    with open(write_document(tmp_path), "rb") as stream:
        k = 5
        stream.seek(k)
        client.query_document(stream, query="q")
        assert file_part(calls[0]) == FILE_BYTES[k:]
        preamble, epilogue = docql._request.frame(
            "doc.pdf", docql._request.body_json("q", None, None), boundary_of(calls[0]).decode()
        )
        expected_length = len(preamble) + len(FILE_BYTES) - k + len(epilogue)
        assert calls[0].headers["content-length"] == str(expected_length)
        assert stream.closed is False


@pytest.mark.parametrize(
    ("label", "factory"),
    [
        ("read-only", lambda data: ReadOnlyStream(data)),
        ("unseekable", lambda data: UnseekableStream(data)),
    ],
)
def test_unmeasurable_stream_sends_chunked(label: str, factory: Callable[[bytes], object]) -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    client.query_document(
        factory(FILE_BYTES),  # type: ignore[arg-type]
        filename="doc.pdf",
        query="q",
    )
    request = calls[0]
    assert "content-length" not in request.headers
    assert request.headers["transfer-encoding"] == "chunked"
    preamble, epilogue = docql._request.frame(
        "doc.pdf", docql._request.body_json("q", None, None), boundary_of(request).decode()
    )
    assert request.read() == preamble + FILE_BYTES + epilogue


def test_short_read_raises_connection_error_naming_the_change() -> None:
    calls: list[httpx.Request] = []
    client = make_client(consuming_handler(calls))
    with pytest.raises(DocQLConnectionError, match="the file changed size during upload"):
        client.query_document(
            LyingShortStream(10),  # type: ignore[arg-type]
            filename="doc.pdf",
            query="q",
        )


def test_over_reporting_stream_sends_exactly_the_measured_length() -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    data = bytes(range(256)) * 8
    client.query_document(
        LyingLongStream(data, 10),  # type: ignore[arg-type]
        filename="doc.pdf",
        query="q",
    )
    request = calls[0]
    assert file_part(request) == data[:10]
    preamble, epilogue = docql._request.frame(
        "doc.pdf", docql._request.body_json("q", None, None), boundary_of(request).decode()
    )
    assert request.headers["content-length"] == str(len(preamble) + 10 + len(epilogue))


def test_empty_bytes_with_filename_send_zero_length_part() -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    client.query_document(b"", filename="empty.pdf", query="q")
    request = calls[0]
    assert file_part(request) == b""
    assert "content-length" in request.headers
    assert "transfer-encoding" not in request.headers


def test_boundary_like_file_content_is_sent_verbatim() -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    content = b"x\r\n--abc\r\n"
    client.query_document(content, filename="doc.pdf", query="q")
    assert file_part(calls[0]) == content


def test_consecutive_calls_draw_fresh_boundaries() -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    client.query_document(b"x", filename="f.pdf", query="q")
    client.query_document(b"x", filename="f.pdf", query="q")
    first = boundary_of(calls[0]).decode()
    second = boundary_of(calls[1]).decode()
    assert first != second
    assert BOUNDARY_RE.fullmatch(first)
    assert BOUNDARY_RE.fullmatch(second)


def test_query_prompt_exclusivity_enforced_before_any_request() -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    with pytest.raises(ValueError, match="exactly one"):
        client.query_document(b"x", filename="f.pdf")
    with pytest.raises(ValueError, match="exactly one"):
        client.query_document(b"x", filename="f.pdf", query="q", prompt="p")
    assert calls == []


def test_empty_query_string_passes_through_to_the_server() -> None:
    calls: list[httpx.Request] = []
    client = make_client(recording_handler(calls))
    client.query_document(b"x", filename="f.pdf", query="")
    assert b'{"query":""}' in calls[0].read()


def test_iter_bytes_caps_chunks_at_64kib() -> None:
    upload = docql._request.prepare_upload(
        b"a" * 200_001, query="q", prompt=None, mode=None, filename="big.bin"
    )
    chunks = list(upload.iter_bytes())
    assert max(len(chunk) for chunk in chunks) <= 65536
    assert upload.content_length == sum(len(chunk) for chunk in chunks)

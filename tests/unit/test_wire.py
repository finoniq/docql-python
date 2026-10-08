"""Golden wire tests: the request bytes the client sends are byte-exact."""

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

import docql._request
from docql import DocQL

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_IDS = ["ascii-query", "thai-query", "prompt", "mode-standard", "escaped-filename"]


def make_golden_client(
    monkeypatch: pytest.MonkeyPatch, golden: dict[str, Any]
) -> tuple[DocQL, list[httpx.Request]]:
    """A client whose boundary is the golden boundary, capturing every request."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            headers={"x-request-id": "00000000-0000-4000-8000-000000000001"},
            json={
                "data": {"answer": "ok"},
                "metadata": {
                    "request_id": "00000000-0000-4000-8000-000000000001",
                    "page_count": 1,
                    "pages_ocr": 0,
                },
            },
        )

    monkeypatch.setattr(docql._request, "new_boundary", lambda: golden["boundary"])
    return DocQL(
        api_key="golden-wire-test-key",
        api_url="http://stub.test/",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ), calls


def golden_case(golden: dict[str, Any], case_id: str) -> dict[str, Any]:
    return next(c for c in golden["cases"] if c["id"] == case_id)


def golden_bin(case_id: str) -> bytes:
    return (ROOT / "contract" / "golden" / f"{case_id}.bin").read_bytes()


@pytest.mark.parametrize("case_id", GOLDEN_IDS)
def test_wire_bytes_match_golden(
    monkeypatch: pytest.MonkeyPatch, golden: dict[str, Any], case_id: str
) -> None:
    case = golden_case(golden, case_id)
    client, calls = make_golden_client(monkeypatch, golden)
    call: dict[str, Any] = {
        "file": base64.b64decode(case["fileBase64"]),
        "filename": case["filename"],
    }
    call.update({key: case[key] for key in ("query", "prompt", "mode") if key in case})
    client.query_document(**call)
    request = calls[0]
    assert request.read() == golden_bin(case_id)
    assert request.headers["content-type"] == golden["contentType"]
    assert request.headers["content-length"] == str(len(golden_bin(case_id)))
    assert "transfer-encoding" not in request.headers


def test_wire_path_input_matches_golden(
    monkeypatch: pytest.MonkeyPatch, golden: dict[str, Any], tmp_path: Path
) -> None:
    case = golden_case(golden, "ascii-query")
    document = tmp_path / case["filename"]
    document.write_bytes(base64.b64decode(case["fileBase64"]))
    client, calls = make_golden_client(monkeypatch, golden)
    client.query_document(document, query=case["query"])
    request = calls[0]
    assert request.read() == golden_bin("ascii-query")
    assert request.headers["content-length"] == str(len(golden_bin("ascii-query")))
    assert "transfer-encoding" not in request.headers


def test_wire_file_object_input_matches_golden(
    monkeypatch: pytest.MonkeyPatch, golden: dict[str, Any], tmp_path: Path
) -> None:
    case = golden_case(golden, "ascii-query")
    document = tmp_path / case["filename"]
    document.write_bytes(base64.b64decode(case["fileBase64"]))
    client, calls = make_golden_client(monkeypatch, golden)
    with open(document, "rb") as stream:
        client.query_document(stream, query=case["query"])
        assert stream.closed is False
    request = calls[0]
    assert request.read() == golden_bin("ascii-query")
    assert request.headers["content-length"] == str(len(golden_bin("ascii-query")))
    assert "transfer-encoding" not in request.headers

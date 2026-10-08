"""Golden wire test: the request bytes the client sends are byte-exact."""

import base64
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

import docql._request
from docql import DocQL

ROOT = Path(__file__).resolve().parents[2]


def test_wire_bytes_match_golden(monkeypatch: pytest.MonkeyPatch, golden: dict[str, Any]) -> None:
    case = next(c for c in golden["cases"] if c["id"] == "ascii-query")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            headers={"x-request-id": "00000000-0000-4000-8000-000000000001"},
            json={
                "data": {"answer": "1 page"},
                "metadata": {
                    "request_id": "00000000-0000-4000-8000-000000000001",
                    "page_count": 1,
                    "pages_ocr": 0,
                },
            },
        )

    monkeypatch.setattr(docql._request, "new_boundary", lambda: golden["boundary"])

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = DocQL(
            api_key="golden-wire-test-key",
            api_url="http://stub.test/",
            http_client=http_client,
        )
        result = client.query_document(
            base64.b64decode(case["fileBase64"]),
            filename=case["filename"],
            query=case["query"],
        )

    request: httpx.Request = captured["request"]
    golden_bin = (ROOT / "contract" / "golden" / "ascii-query.bin").read_bytes()
    assert request.read() == golden_bin
    assert request.headers["content-type"] == golden["contentType"]
    assert request.headers["content-length"] == str(len(golden_bin))
    assert "transfer-encoding" not in request.headers
    assert request.headers["x-api-key"] == "golden-wire-test-key"
    assert re.fullmatch(
        r"docql-python/0\.1\.0 \(python [^;]+; httpx [^)]+\)",
        request.headers["user-agent"],
    )
    assert request.extensions["timeout"] == {
        "connect": 10.0,
        "read": 660.0,
        "write": 660.0,
        "pool": 660.0,
    }
    assert result.page_count == 1
    assert result.pages_ocr == 0
    assert result.request_id == "00000000-0000-4000-8000-000000000001"

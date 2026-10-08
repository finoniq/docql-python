"""The shared contract case list, sdk cases, against the digest-pinned stub."""

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from docql import DocQL, DocQLError

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
CASES: dict[str, Any] = json.loads((ROOT / "contract" / "sdk-cases.json").read_bytes())
KEYS: dict[str, str] = {"valid": CASES["keys"]["valid"], "invalid": CASES["keys"]["invalid"]}
SDK_CASES: list[dict[str, Any]] = [c for c in CASES["cases"] if c["via"] == "sdk"]


def _file_bytes(file: dict[str, Any]) -> bytes:
    if "base64" in file:
        decoded: bytes = base64.b64decode(file["base64"])
        return decoded
    repeated: bytes = file["repeatByte"].encode("ascii") * int(file["length"])
    return repeated


@pytest.mark.parametrize("case", SDK_CASES, ids=[c["id"] for c in SDK_CASES])
def test_sdk_case(case: dict[str, Any], stub_url: str) -> None:
    client = DocQL(api_key=KEYS[case["key"]], api_url=stub_url)
    request = case["request"]
    file = request["file"]
    kwargs: dict[str, Any] = {"filename": file["name"]}
    if "query" in request:
        kwargs["query"] = request["query"]
    if "prompt" in request:
        kwargs["prompt"] = request["prompt"]
    if "mode" in request:
        kwargs["mode"] = request["mode"]
    expect = case["expect"]
    data = _file_bytes(file)

    if expect["status"] == 200:
        result = client.query_document(data, **kwargs)
        assert result.page_count == expect["pageCount"]
        assert result.pages_ocr == expect["pagesOcr"]
        assert result.request_id != ""
        return

    with pytest.raises(DocQLError) as excinfo:
        client.query_document(data, **kwargs)
    err = excinfo.value
    assert err.status == expect["status"]
    assert err.reason == expect["reason"]
    if "retryAfterSeconds" in expect:
        assert err.retry_after == expect["retryAfterSeconds"]
    assert err.retryable == (expect["status"] in (429, 503))
    if expect.get("requestIdPresent"):
        assert err.request_id

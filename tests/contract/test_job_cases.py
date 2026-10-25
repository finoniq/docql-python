"""The kit's ``via: wait`` job cases through the public job methods, against the stub (JSDK-01).

Step semantics follow the kit's reference runners (docql-console
``deploy/scripts/job-cases-edge.mjs``): a per-run ``{run}`` token keeps each
case's fingerprint unique, ``submit`` posts the document as a job and binds the
answered job id, and ``wait`` calls the public ``wait()``.
"""

import base64
import json
import re
import secrets
import string
from pathlib import Path
from typing import Any

import pytest

from docql import JOB_STATUS_QUEUED, JOB_STATUS_SUCCEEDED, DocQL, DocQLError, Job, QueryResult

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
KIT: dict[str, Any] = json.loads((ROOT / "contract" / "job-cases.json").read_bytes())
KEYS: dict[str, str] = {"valid": KIT["keys"]["valid"], "validOther": KIT["keys"]["validOther"]}
KNOWN_VIAS = ("submit", "poll", "cancel", "wait", "sync", "infra")

# One token per module run: 8 base62 characters (kit invariant 8).
RUN = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
assert re.fullmatch(r"[A-Za-z0-9]{8}", RUN)

WAIT_CASES: list[dict[str, Any]] = [
    c
    for c in KIT["cases"]
    if c["target"] == "stub"
    and c["via"] == "wait"
    and not any(step["op"] == "cancel" for step in c["steps"])
]


def _sub(text: str) -> str:
    return text.replace("{run}", RUN)


def _file_bytes(file: dict[str, Any]) -> bytes:
    if "base64" in file:
        decoded: bytes = base64.b64decode(file["base64"])
        return decoded
    repeated: bytes = file["repeatByte"].encode("ascii") * int(file["length"])
    return repeated


def test_every_job_case_uses_a_known_via() -> None:
    """A case with an unknown via must fail the run, never silently disappear."""
    unknown = [c["id"] for c in KIT["cases"] if c.get("via") not in KNOWN_VIAS]
    assert not unknown, f"job-cases.json carries cases with an unknown via: {unknown}"


def test_wait_cases_are_selected() -> None:
    assert [c["id"] for c in WAIT_CASES] == ["wait-succeeded", "wait-failed"]


@pytest.mark.parametrize("case", WAIT_CASES, ids=[c["id"] for c in WAIT_CASES])
def test_wait_case(case: dict[str, Any], stub_url: str) -> None:
    client = DocQL(api_key=KEYS["valid"], api_url=stub_url)
    jobs: dict[str, Job] = {}
    for step in case["steps"]:
        op = step["op"]
        if op == "submit":
            assert step.get("key", "valid") == "valid"
            request = step["request"]
            kwargs: dict[str, Any] = {"filename": request["file"]["name"]}
            for name in ("query", "prompt", "mode"):
                if name in request:
                    kwargs[name] = _sub(request[name])
            assert step["expect"]["status"] == 202
            job = client.submit_document(_file_bytes(request["file"]), **kwargs)
            assert job.status == JOB_STATUS_QUEUED
            assert job.job_id
            assert job.idempotency_key
            jobs[step["as"]] = job
        elif op == "wait":
            job_id = jobs[step["job"]].job_id
            expect = step["expect"]
            if expect["outcome"] == "failed":
                with pytest.raises(DocQLError) as excinfo:
                    client.wait(job_id)
                err = excinfo.value
                assert err.reason == expect["reason"]
                assert err.job_id == job_id
                assert err.retryable is False
            else:
                assert expect["outcome"] == "succeeded"
                done = client.wait(job_id)
                assert done.job_id == job_id
                assert done.status == JOB_STATUS_SUCCEEDED
                assert isinstance(done.result, QueryResult)
        else:
            pytest.fail(f"{case['id']}: unsupported op {op!r}")

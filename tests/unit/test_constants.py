"""Public reason constants and __all__ pinned against the vendored contract kit (D-05, D-09)."""

from typing import Any

import docql

EXPECTED_ALL = sorted(
    [
        "DocQL",
        "DocQLConnectionError",
        "DocQLError",
        "DocQLException",
        "DocQLTimeoutError",
        "MODE_FAST",
        "MODE_STANDARD",
        "Mode",
        "QueryResult",
        "REASON_BODY_NOT_JSON",
        "REASON_CONCURRENCY_LIMIT_EXCEEDED",
        "REASON_CORRUPTED_FILE",
        "REASON_DOCUMENT_EXCEEDS_TIME_BUDGET",
        "REASON_IDEMPOTENCY_IN_PROGRESS",
        "REASON_IDEMPOTENCY_KEY_CONFLICT",
        "REASON_INVALID_API_KEY",
        "REASON_INVALID_PARAMS",
        "REASON_JOB_EXPIRED",
        "REASON_JOB_INTERRUPTED",
        "REASON_JOB_NOT_FOUND",
        "REASON_MISSING_BODY_FIELD",
        "REASON_MISSING_FILE_PART",
        "REASON_MISSING_QUERY_OR_PROMPT",
        "REASON_QUEUE_FULL",
        "REASON_RATE_LIMIT_EXCEEDED",
        "REASON_SERVICE_OVERLOADED",
        "REASON_UNSUPPORTED_FILE_TYPE",
        "REASON_UNSUPPORTED_MEDIA_TYPE",
        "REASON_UPLOAD_TOO_LARGE",
        "REASON_UPSTREAM_AUTH_FAILED",
        "REASON_UPSTREAM_UNAVAILABLE",
        "__version__",
    ]
)


def reason_items() -> dict[str, str]:
    """The module's REASON_* constants as a name -> value dict."""
    return {
        name: value
        for name, value in vars(docql).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }


def test_exactly_twenty_two_reason_constants() -> None:
    assert len(reason_items()) == 22


def test_reason_values_are_lowercased_names() -> None:
    for name, value in reason_items().items():
        assert value == name.removeprefix("REASON_").lower()


def test_reason_values_equal_the_vendored_case_reasons(
    sdk_cases: dict[str, Any], job_cases: dict[str, Any]
) -> None:
    sdk = [
        case["expect"]["reason"]
        for section in ("cases", "decodeCases")
        for case in sdk_cases[section]
    ]
    job = [entry["reason"] for entry in job_cases["reasons"]["entries"]]
    job += [case["expect"]["reason"] for case in job_cases["decodeCases"]]
    job += [
        step["expect"].get(field)
        for case in job_cases["cases"]
        for step in case.get("steps", [])
        if "expect" in step
        for field in ("reason", "embeddedReason")
    ]
    job += [case["expect"].get("reason") for case in job_cases["waitCases"]]
    expected = {reason for reason in sdk + job if reason is not None}
    assert set(reason_items().values()) == expected


def test_all_exports_exactly() -> None:
    assert sorted(docql.__all__) == EXPECTED_ALL

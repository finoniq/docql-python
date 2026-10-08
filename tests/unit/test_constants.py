"""Public reason constants and __all__ pinned against the vendored case list (D-05)."""

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
        "REASON_CORRUPTED_FILE",
        "REASON_DOCUMENT_EXCEEDS_TIME_BUDGET",
        "REASON_INVALID_API_KEY",
        "REASON_INVALID_PARAMS",
        "REASON_MISSING_BODY_FIELD",
        "REASON_MISSING_FILE_PART",
        "REASON_MISSING_QUERY_OR_PROMPT",
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


def test_exactly_fourteen_reason_constants() -> None:
    assert len(reason_items()) == 14


def test_reason_values_are_lowercased_names() -> None:
    for name, value in reason_items().items():
        assert value == name.removeprefix("REASON_").lower()


def test_reason_values_equal_the_vendored_case_reasons(sdk_cases: dict[str, Any]) -> None:
    expected = {
        case["expect"]["reason"]
        for section in ("cases", "decodeCases")
        for case in sdk_cases[section]
        if case["expect"]["reason"] is not None
    }
    assert set(reason_items().values()) == expected


def test_all_exports_exactly() -> None:
    assert sorted(docql.__all__) == EXPECTED_ALL

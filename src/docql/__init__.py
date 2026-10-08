"""Python client for the DocQL document-extraction API."""

from docql._client import DocQL
from docql._constants import (
    MODE_FAST,
    MODE_STANDARD,
    REASON_BODY_NOT_JSON,
    REASON_CORRUPTED_FILE,
    REASON_DOCUMENT_EXCEEDS_TIME_BUDGET,
    REASON_INVALID_API_KEY,
    REASON_INVALID_PARAMS,
    REASON_MISSING_BODY_FIELD,
    REASON_MISSING_FILE_PART,
    REASON_MISSING_QUERY_OR_PROMPT,
    REASON_SERVICE_OVERLOADED,
    REASON_UNSUPPORTED_FILE_TYPE,
    REASON_UNSUPPORTED_MEDIA_TYPE,
    REASON_UPLOAD_TOO_LARGE,
    REASON_UPSTREAM_AUTH_FAILED,
    REASON_UPSTREAM_UNAVAILABLE,
    Mode,
)
from docql._errors import (
    DocQLConnectionError,
    DocQLError,
    DocQLException,
    DocQLTimeoutError,
)
from docql._response import QueryResult
from docql._version import __version__

__all__ = [
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

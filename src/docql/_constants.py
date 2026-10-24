"""Shared constants for the DocQL client."""

from typing import Final, Literal

import httpx

DEFAULT_API_URL: Final[str] = "https://api-docql.finoniq.com"
ENV_API_KEY: Final[str] = "DOCQL_API_KEY"
ENV_API_URL: Final[str] = "DOCQL_API_URL"
QUERY_PATH: Final[str] = "/v1/query-document"

# Extraction modes (D-05): plain module-level constants plus a Literal alias.
# ``mode`` itself stays an open ``str`` everywhere — unknown values pass through
# for the server to validate.
MODE_FAST: Final[str] = "fast"
MODE_STANDARD: Final[str] = "standard"
Mode = Literal["fast", "standard"]

# Largest chunk one upload yields, keeping large files out of memory (T-11-09).
CHUNK_SIZE: Final[int] = 65536

# One timeout ladder for every request (D-02): 660 s read/write/pool over the
# engine's 540 s standard ceiling, 10 s connect.
CONNECT_TIMEOUT: Final[float] = 10.0
DEFAULT_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(660.0, connect=10.0)

# Kept characters of a non-2xx response body stored on DocQLError (D-11).
ERROR_BODY_KEEP_CHARS: Final[int] = 2048

# A non-2xx body is streamed and discarded after this many bytes, so a
# misbehaving proxy cannot exhaust memory (T-11-12).
ERROR_BODY_READ_LIMIT: Final[int] = 65536

# The API's upload ceiling; over this, transport failures gain a size hint
# because an early RST may have discarded the 413 (research Pitfall 4).
MAX_UPLOAD_BYTES: Final[int] = 26_214_400

# Reason constants (D-05, D-09): exactly the 22 non-null reasons of the vendored
# contract kit — contract/sdk-cases.json (the sync stub and gateway errors) plus
# contract/job-cases.json (the job errors). The engine's wider class table stays
# reachable through the open ``reason: str``; adding constants later is
# backward compatible, removing them is not.
REASON_INVALID_API_KEY: Final[str] = "invalid_api_key"
REASON_UPLOAD_TOO_LARGE: Final[str] = "upload_too_large"
REASON_UNSUPPORTED_MEDIA_TYPE: Final[str] = "unsupported_media_type"
REASON_MISSING_FILE_PART: Final[str] = "missing_file_part"
REASON_MISSING_BODY_FIELD: Final[str] = "missing_body_field"
REASON_BODY_NOT_JSON: Final[str] = "body_not_json"
REASON_MISSING_QUERY_OR_PROMPT: Final[str] = "missing_query_or_prompt"
REASON_INVALID_PARAMS: Final[str] = "invalid_params"
REASON_UNSUPPORTED_FILE_TYPE: Final[str] = "unsupported_file_type"
REASON_CORRUPTED_FILE: Final[str] = "corrupted_file"
REASON_DOCUMENT_EXCEEDS_TIME_BUDGET: Final[str] = "document_exceeds_time_budget"
REASON_SERVICE_OVERLOADED: Final[str] = "service_overloaded"
REASON_UPSTREAM_UNAVAILABLE: Final[str] = "upstream_unavailable"
REASON_UPSTREAM_AUTH_FAILED: Final[str] = "upstream_auth_failed"
REASON_CONCURRENCY_LIMIT_EXCEEDED: Final[str] = "concurrency_limit_exceeded"
REASON_RATE_LIMIT_EXCEEDED: Final[str] = "rate_limit_exceeded"
REASON_JOB_NOT_FOUND: Final[str] = "job_not_found"
REASON_JOB_EXPIRED: Final[str] = "job_expired"
REASON_QUEUE_FULL: Final[str] = "queue_full"
REASON_IDEMPOTENCY_KEY_CONFLICT: Final[str] = "idempotency_key_conflict"
REASON_IDEMPOTENCY_IN_PROGRESS: Final[str] = "idempotency_in_progress"
REASON_JOB_INTERRUPTED: Final[str] = "job_interrupted"

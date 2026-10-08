"""Shared constants for the DocQL client."""

from typing import Final

import httpx

DEFAULT_API_URL: Final[str] = "https://api-docql.finoniq.com"
ENV_API_KEY: Final[str] = "DOCQL_API_KEY"
ENV_API_URL: Final[str] = "DOCQL_API_URL"
QUERY_PATH: Final[str] = "/v1/query-document"

# One timeout ladder for every request (D-02): 660 s read/write/pool over the
# engine's 540 s standard ceiling, 10 s connect.
CONNECT_TIMEOUT: Final[float] = 10.0
DEFAULT_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(660.0, connect=10.0)

# Kept characters of a non-2xx response body stored on DocQLError (D-11).
ERROR_BODY_KEEP_CHARS: Final[int] = 2048

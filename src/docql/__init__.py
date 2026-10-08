"""Python client for the DocQL document-extraction API."""

from docql._client import DocQL
from docql._constants import MODE_FAST, MODE_STANDARD, Mode
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
    "__version__",
]

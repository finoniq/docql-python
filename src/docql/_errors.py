"""Exception tree for the DocQL client (D-09/D-11/D-12).

DocQLException(Exception)
├── DocQLError                # the server answered non-2xx
└── DocQLConnectionError      # no response: connect, TLS, reset, early RST
    └── DocQLTimeoutError
"""

from collections.abc import Callable
from typing import Any

_SNIPPET_MAX_CHARS = 200


def _snippet(body: str) -> str:
    collapsed = " ".join(body.split())
    if not collapsed:
        return "(empty body)"
    return collapsed[:_SNIPPET_MAX_CHARS]


class DocQLException(Exception):
    """Base class for every exception the DocQL client raises."""


class DocQLError(DocQLException):
    """The DocQL API answered with a non-2xx status (D-11/D-12).

    Neither :func:`str` nor :func:`repr` ever contains anything beyond the
    stored fields, and ``body`` is scrubbed by the decoder before it is
    stored, so no form leaks the API key.
    """

    def __init__(
        self,
        *,
        status: int,
        reason: str | None,
        request_id: str | None,
        retry_after: int | None,
        retryable: bool,
        message: str | None,
        body: str,
    ) -> None:
        super().__init__()
        self.status = status
        self.reason = reason
        self.request_id = request_id
        self.retry_after = retry_after
        self.retryable = retryable
        self.message = message
        self.body = body

    def __str__(self) -> str:
        if self.reason is not None:
            text = f"{self.status} {self.reason}"
            if self.message is not None:
                text = f"{text}: {self.message}"
            extras = []
            if self.request_id is not None:
                extras.append(f"request_id={self.request_id}")
            if self.retry_after is not None:
                extras.append(f"retry_after={self.retry_after}s")
            if extras:
                text = f"{text} ({', '.join(extras)})"
            return text
        text = f"{self.status} (non-DocQL response): {_snippet(self.body)}"
        if self.request_id is not None:
            text = f"{text} (request_id={self.request_id})"
        return text

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status={self.status!r}, "
            f"reason={self.reason!r}, request_id={self.request_id!r})"
        )

    def __reduce__(self) -> tuple[Callable[[dict[str, Any]], "DocQLError"], tuple[dict[str, Any]]]:
        # Keyword-only __init__ does not pickle by default; rebuild through a
        # module-level callable so pickle.loads(pickle.dumps(err)) round-trips.
        return _rebuild_docql_error, (
            {
                "status": self.status,
                "reason": self.reason,
                "request_id": self.request_id,
                "retry_after": self.retry_after,
                "retryable": self.retryable,
                "message": self.message,
                "body": self.body,
            },
        )


def _rebuild_docql_error(fields: dict[str, Any]) -> DocQLError:
    return DocQLError(**fields)


class DocQLConnectionError(DocQLException):
    """No DocQL response arrived: connect, TLS, reset or early connection close."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class DocQLTimeoutError(DocQLConnectionError):
    """The DocQL request timed out. The call may still have been billed."""

"""Exception tree for the DocQL client (D-09/D-11/D-12).

DocQLException(Exception)
├── DocQLError                # the server answered non-2xx, or a job failed
└── DocQLConnectionError      # no response: connect, TLS, reset, early RST
    └── DocQLTimeoutError
        └── DocQLWaitTimeoutError   # wait() ran out of time; the job was not cancelled
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

    ``job_id`` is set on errors from the job methods (a failed job, a poll or
    cancel error). ``idempotency_key`` is set on errors from
    ``submit_document`` so the caller can retry with the same key; it is a
    field only and never appears in :func:`str` or :func:`repr` (D-08).
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
        job_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        super().__init__()
        self.status = status
        self.reason = reason
        self.request_id = request_id
        self.retry_after = retry_after
        self.retryable = retryable
        self.message = message
        self.body = body
        self.job_id = job_id
        self.idempotency_key = idempotency_key

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
            if self.job_id is not None:
                extras.append(f"job_id={self.job_id}")
            if extras:
                text = f"{text} ({', '.join(extras)})"
            return text
        text = f"{self.status} (non-DocQL response): {_snippet(self.body)}"
        tail = []
        if self.request_id is not None:
            tail.append(f"request_id={self.request_id}")
        if self.job_id is not None:
            tail.append(f"job_id={self.job_id}")
        if tail:
            text = f"{text} ({', '.join(tail)})"
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
                "job_id": self.job_id,
                "idempotency_key": self.idempotency_key,
            },
        )


def _rebuild_docql_error(fields: dict[str, Any]) -> DocQLError:
    return DocQLError(**fields)


class DocQLConnectionError(DocQLException):
    """No DocQL response arrived: connect, TLS, reset or early connection close.

    ``idempotency_key`` is set when the failed request was a
    ``submit_document`` call, so the caller can retry it with the same key
    (D-08). It never appears in the message.
    """

    def __init__(self, message: str, *, idempotency_key: str | None = None) -> None:
        super().__init__(message)
        self.idempotency_key = idempotency_key


class DocQLTimeoutError(DocQLConnectionError):
    """The DocQL request timed out. The call may still have been billed."""


class DocQLWaitTimeoutError(DocQLTimeoutError):
    """``wait()`` ran out of time before the job finished (OD-5).

    The job was not cancelled: it may still finish, and ``get_job(job_id)``
    or another ``wait(job_id)`` picks it up again.
    """

    def __init__(self, *, job_id: str, timeout: float) -> None:
        super().__init__(
            f"wait for job {job_id} timed out after {timeout:g} s; the job was not cancelled"
        )
        self.job_id = job_id
        self.timeout = timeout

    def __reduce__(
        self,
    ) -> tuple[Callable[[str, float], "DocQLWaitTimeoutError"], tuple[str, float]]:
        return _rebuild_wait_timeout, (self.job_id, self.timeout)


def _rebuild_wait_timeout(job_id: str, timeout: float) -> DocQLWaitTimeoutError:
    return DocQLWaitTimeoutError(job_id=job_id, timeout=timeout)

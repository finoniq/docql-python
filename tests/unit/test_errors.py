"""Exception-tree and pickling tests (D-09, D-12)."""

import pickle

from docql import DocQLConnectionError, DocQLError, DocQLException, DocQLTimeoutError


def sample_error() -> DocQLError:
    return DocQLError(
        status=429,
        reason="service_overloaded",
        request_id="req-1",
        retry_after=5,
        retryable=True,
        message="slow down",
        body='{"echo": "***"}',
    )


def test_docql_error_pickles_with_every_field() -> None:
    original = sample_error()
    restored = pickle.loads(pickle.dumps(original))
    assert type(restored) is DocQLError
    assert restored.status == original.status
    assert restored.reason == original.reason
    assert restored.request_id == original.request_id
    assert restored.retry_after == original.retry_after
    assert restored.retryable is original.retryable
    assert restored.message == original.message
    assert restored.body == original.body
    assert str(restored) == str(original)


def test_connection_and_timeout_errors_pickle() -> None:
    for original in (DocQLConnectionError("connection failed"), DocQLTimeoutError("timed out")):
        restored = pickle.loads(pickle.dumps(original))
        assert type(restored) is type(original)
        assert str(restored) == str(original)


def test_exception_hierarchy() -> None:
    assert issubclass(DocQLTimeoutError, DocQLConnectionError)
    assert not issubclass(DocQLError, DocQLConnectionError)
    for exc in (DocQLError, DocQLConnectionError, DocQLTimeoutError):
        assert issubclass(exc, DocQLException)

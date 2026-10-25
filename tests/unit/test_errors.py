"""Exception-tree and pickling tests (D-09, D-12)."""

import pickle

import pytest

from docql import (
    DocQLConnectionError,
    DocQLError,
    DocQLException,
    DocQLTimeoutError,
    DocQLWaitTimeoutError,
)


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


def job_error(**overrides: object) -> DocQLError:
    fields: dict[str, object] = {
        "status": 503,
        "reason": "upstream_unavailable",
        "request_id": "req-9",
        "retry_after": None,
        "retryable": False,
        "message": "an upstream provider is unavailable",
        "body": "{}",
        "job_id": "job_abc",
        "idempotency_key": "secret-idem-key",
    }
    fields.update(overrides)
    return DocQLError(**fields)  # type: ignore[arg-type]


def test_job_fields_survive_pickle() -> None:
    original = job_error()
    restored = pickle.loads(pickle.dumps(original))
    assert restored.job_id == "job_abc"
    assert restored.idempotency_key == "secret-idem-key"
    assert str(restored) == str(original)


def test_str_shows_job_id_and_never_the_key() -> None:
    err = job_error()
    assert str(err) == (
        "503 upstream_unavailable: an upstream provider is unavailable "
        "(request_id=req-9, job_id=job_abc)"
    )
    assert "secret-idem-key" not in str(err)
    assert "secret-idem-key" not in repr(err)
    framework = job_error(reason=None, message=None, body='{"detail":"Not Found"}')
    assert str(framework).endswith("(request_id=req-9, job_id=job_abc)")
    assert "secret-idem-key" not in str(framework)


def test_without_job_id_str_and_repr_are_the_v01_forms() -> None:
    err = job_error(job_id=None, idempotency_key=None)
    assert str(err) == (
        "503 upstream_unavailable: an upstream provider is unavailable (request_id=req-9)"
    )
    assert repr(err) == (
        "DocQLError(status=503, reason='upstream_unavailable', request_id='req-9')"
    )


def test_connection_error_key_survives_pickle_and_stays_out_of_str() -> None:
    for cls in (DocQLConnectionError, DocQLTimeoutError):
        original = cls("connection failed", idempotency_key="secret-idem-key")
        restored = pickle.loads(pickle.dumps(original))
        assert type(restored) is cls
        assert restored.idempotency_key == "secret-idem-key"
        assert "secret-idem-key" not in str(restored)
    assert DocQLConnectionError("x").idempotency_key is None


def test_wait_timeout_error() -> None:
    err = DocQLWaitTimeoutError(job_id="job_abc", timeout=900.0)
    assert isinstance(err, DocQLTimeoutError)
    assert err.job_id == "job_abc"
    assert err.timeout == 900.0
    assert str(err) == "wait for job job_abc timed out after 900 s; the job was not cancelled"
    restored = pickle.loads(pickle.dumps(err))
    assert type(restored) is DocQLWaitTimeoutError
    assert (restored.job_id, restored.timeout, str(restored)) == (err.job_id, 900.0, str(err))
    with pytest.raises(TypeError):
        DocQLWaitTimeoutError("job_abc", 900.0)  # type: ignore[call-arg]

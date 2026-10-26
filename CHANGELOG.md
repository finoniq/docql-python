# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-26

### Added

- Async jobs for long documents: `submit_document(file, *, query, prompt, mode,
  filename, idempotency_key)`, `get_job(job_id)`, `wait(job_id, *, timeout,
  poll_interval)` and `cancel_job(job_id)`
- `Job` with `job_id`, `status`, `page_count`, `pages_done`, `request_id`, the
  `created_at` / `started_at` / `finished_at` / `expires_at` timestamps,
  `result` (a `QueryResult` on a succeeded job), `error` (a `DocQLError` on a
  failed job), `retry_after` and `idempotency_key`
- Job status constants `JOB_STATUS_QUEUED`, `JOB_STATUS_RUNNING`,
  `JOB_STATUS_SUCCEEDED`, `JOB_STATUS_FAILED` and `JOB_STATUS_CANCELLED`; an
  unknown status from a newer API is kept verbatim
- Eight new reason constants: `REASON_CONCURRENCY_LIMIT_EXCEEDED`,
  `REASON_RATE_LIMIT_EXCEEDED`, `REASON_JOB_NOT_FOUND`, `REASON_JOB_EXPIRED`,
  `REASON_QUEUE_FULL`, `REASON_IDEMPOTENCY_KEY_CONFLICT`,
  `REASON_IDEMPOTENCY_IN_PROGRESS` and `REASON_JOB_INTERRUPTED`
- `DocQLError.job_id` and `DocQLError.idempotency_key`,
  `DocQLConnectionError.idempotency_key`, and `DocQLWaitTimeoutError` (a
  `DocQLTimeoutError` with `job_id` and `timeout`)
- Every submit carries an `Idempotency-Key`, generated when you pass none. A
  503 or a 409 `idempotency_in_progress` on submit is retried with that same
  key, at most 4 attempts, and only for input that can be replayed from its
  start (a path, `bytes` or a seekable file object)
- `wait()` sleeps the server's `Retry-After` between polls (clamped to 1-30 s),
  retries network errors, 429 and 503, and gives up with
  `DocQLWaitTimeoutError` after 15 minutes by default. It never sends a poll at
  or past its deadline: a sleep that ends exactly on the deadline ends the wait.
  It never cancels the job

### Changed

- Nothing breaking. 0.1.0's "the SDK never retries" now applies to
  `query_document` only: `submit_document` retries as described above, and
  `query_document`, `get_job` and `cancel_job` each send exactly one request

### Fixed

- `Retry-After` on every 429 and 503 answer is now pinned by tests against the
  contract kit (`retry_after` and `retryable` on the decoded error)
- The release workflow no longer passes `secrets: inherit` to the contract
  workflow, which only needs `GITHUB_TOKEN`

No breaking changes: code written against 0.1.0 runs unchanged.

## [0.1.0] - 2026-10-08

### Added

- `DocQL` client with keyword-only `api_key`, `api_url`, `timeout` and
  `http_client`; usable as a context manager (`with DocQL() as client:`) and
  closable with `close()`
- `query_document(file, *, query, prompt, mode, filename)`: file input as a
  path (`str` or `os.PathLike`), raw `bytes`, or a binary file object read
  from its current position and never closed
- `QueryResult` with `data`, `request_id`, `page_count`, `pages_ocr` and the
  raw `metadata`
- Exception tree `DocQLException` → `DocQLError`, `DocQLConnectionError` →
  `DocQLTimeoutError`; `DocQLError` carries `status`, `reason`, `request_id`,
  `retry_after`, `retryable`, `message` and a truncated `body`, and the API
  key never appears on any of them
- Mode constants `MODE_FAST` / `MODE_STANDARD` and the fourteen `REASON_*`
  reason constants
- Typed package (`py.typed`)

[0.2.0]: https://github.com/finoniq/docql-python/tree/v0.2.0
[0.1.0]: https://github.com/finoniq/docql-python/tree/v0.1.0

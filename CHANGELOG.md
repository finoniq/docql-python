# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.1] - 2026-11-25

### Fixed

- The README webhook receiver answers a malformed or set-but-empty secret with
  HTTP 500 instead of dropping the connection, and passes the request headers
  as they are, so repeated webhook headers follow the documented join and
  disagreement rules
- `verify_webhook` accepts header objects such as `http.client.HTTPMessage`
  and `BaseHTTPRequestHandler.headers` in its type, because the `headers`
  annotation now matches what the function always accepted at run time

### Changed

- The README rotation guidance says to remove the old secret from the list
  when the overlap ends, and at once after a roll caused by a leak

No breaking changes: code written against 0.4.0 runs unchanged.

### Release

Like 0.4.0, 0.4.1 is uploaded from a maintainer machine with a project-scoped
PyPI API token instead of Trusted Publishing, so it has no PEP 740
attestation. Check a download against the sha256 digests PyPI lists for each
file.

## [0.4.0] - 2026-11-24

### Added

- `docql.verify_webhook(body, headers, secrets, *, tolerance=300)` verifies a
  DocQL webhook delivery, signed as the open Standard Webhooks spec describes,
  and returns a frozen `docql.WebhookEvent` (`id`, `type`, `timestamp`,
  `job_id`, `status`, `data`). Pass the raw request bytes, the request headers
  and one secret, or a list of secrets while a rotation overlaps. `tolerance`
  is the allowed clock difference in seconds
- `docql.WebhookVerificationError`, a `DocQLException` whose `reason` is one of
  `missing_header`, `invalid_timestamp`, `invalid_signature_header`,
  `signature_mismatch`, `timestamp_too_old`, `timestamp_too_new` or
  `invalid_payload`
- Four event type constants: `EVENT_JOB_SUCCEEDED`, `EVENT_JOB_FAILED`,
  `EVENT_JOB_CANCELLED` and `EVENT_WEBHOOK_TEST`
- A README `## Webhooks` section with a receiver built on the standard library

### Fixed

- Decoding an error answer or a failed job no longer raises `RecursionError`
  when the metadata is deeply nested, a regression on Python 3.12 and later.
  The error is still a `DocQLError`, with the API key scrubbed from the
  metadata, or with empty metadata when the body is too deep to parse

### Changed

- The README spreadsheet example finds the axis by name (`cells`, `runes`,
  `estimated_output_tokens` or `bytes`) and re-raises when none is present
- The vendored contract kit moves to v6, whose success bodies no longer carry
  `metadata.screenshot`; the SDK reads a success body with or without that key
  the same way
- The PyPI `Changelog` link points at this file
- The README now suggests pinning `docql>=0.4,<0.5`

No breaking changes: code written against 0.3.0 runs unchanged.

### Release

Like 0.3.0, 0.4.0 is uploaded from a maintainer machine with a project-scoped
PyPI API token instead of Trusted Publishing, so it has no PEP 740
attestation. Check a download against the sha256 digests PyPI lists for each
file.

## [0.3.0] - 2026-11-23

### Added

- `DocQLError.metadata`: the server's raw refusal metadata as a `dict`, with
  the API key scrubbed from every key and string value. It is `{}` when the
  answer carries no metadata object, is filled on `query_document` and on every
  job method, and is kept through pickling. For `spreadsheet_too_large` it
  holds the measured value under its axis key (`cells`, `runes`,
  `estimated_output_tokens` or `bytes`) plus `limit`, `reason` and `request_id`
- Three new reason constants: `REASON_SPREADSHEET_TOO_LARGE`,
  `REASON_DOCUMENT_TOO_COMPLEX` and `REASON_TOO_MANY_PAGES`
- A README spreadsheet example and the CSV file-name rule: a CSV is recognised
  only by a name ending in `.csv` (any case), `bytes` and streams need
  `filename=`, and the name is sent exactly as given

### Changed

- The vendored contract kit is now v5 (stub pin `sha-1a3c3ce`), and the test
  suite runs its 26 spreadsheet cases on both the sync and the jobs route
- The README now suggests pinning `docql>=0.3,<0.4`

No breaking changes: code written against 0.2.1 runs unchanged.

### Release

0.3.0 is uploaded from a maintainer machine with a project-scoped PyPI API
token instead of Trusted Publishing, so unlike 0.1.0 and 0.2.x it has no
PEP 740 attestation. Check a download against the sha256 digests PyPI lists
for each file.

## [0.2.1] - 2026-10-26

### Fixed

- WR-01: after a submit 503, a failure to re-read the input for the same-key
  retry (a moved or deleted path, a failing seek, a closed stream, a read
  error) now raises `DocQLConnectionError` carrying `idempotency_key`, so you
  can resubmit with that key instead of creating a second job. An interrupt
  such as Ctrl-C during the retry keeps its type and carries the key as its
  `idempotency_key` attribute; the key is never printed
- WR-02: `get_job`, `cancel_job` and `wait` set `job_id` on every `DocQLError`
  they raise, including an unusable 2xx answer and a failed job without an
  error object

No breaking changes: code written against 0.2.0 runs unchanged.

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

[0.4.1]: https://github.com/finoniq/docql-python/tree/v0.4.1
[0.4.0]: https://github.com/finoniq/docql-python/tree/v0.4.0
[0.3.0]: https://github.com/finoniq/docql-python/tree/v0.3.0
[0.2.1]: https://github.com/finoniq/docql-python/tree/v0.2.1
[0.2.0]: https://github.com/finoniq/docql-python/tree/v0.2.0
[0.1.0]: https://github.com/finoniq/docql-python/tree/v0.1.0

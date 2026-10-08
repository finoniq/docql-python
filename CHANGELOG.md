# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/finoniq/docql-python/tree/v0.1.0

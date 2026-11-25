# DocQL Python SDK

A Python client for the DocQL document-extraction API (`POST /v1/query-document`).

## Install

```sh
pip install docql
```

## Requirements

- Python 3.11 or newer
- httpx `>=0.27,<1` (installed with the SDK)

## Quickstart

Set `DOCQL_API_KEY`, and set `DOCQL_API_URL` to the API origin you were given — the production endpoint `https://api-docql.finoniq.com` is not live yet.

<!-- docql:quickstart:start -->
```python
from docql import DocQL

with DocQL() as client:  # reads DOCQL_API_KEY and DOCQL_API_URL
    result = client.query_document("invoice.pdf", query="{ invoice_number total_amount }")
    print(result.request_id, result.page_count)
    print(result.data)
```
<!-- docql:quickstart:end -->

The query uses DocQL's brace-and-field syntax; pass `prompt=` instead for a
natural-language instruction. A call takes exactly one of `query` or `prompt`.

## Long documents: jobs

Use a job for a long document: a proxied edge cuts a single call at about
100–120 s, while `mode=standard` may take up to 540 s. `submit_document` uploads
the file and answers at once with a queued job, and `wait` polls it until it
finishes.

<!-- docql:jobs:start -->
```python
from docql import DocQL

with DocQL() as client:  # reads DOCQL_API_KEY and DOCQL_API_URL
    job = client.submit_document("invoice.pdf", query="{ invoice_number total_amount }")
    job = client.wait(job.job_id)  # polls until succeeded or cancelled
    print(job.status)
    if job.result is not None:
        print(job.result.data)
```
<!-- docql:jobs:end -->

- `get_job(job_id)` makes one poll and answers the job as it is; a `failed` job
  is answered too (read `job.error`), never raised.
- `cancel_job(job_id)` asks DocQL to cancel and answers the job at that moment,
  which may still be `running`: poll to confirm.
- `wait` answers a `succeeded` or `cancelled` job and raises the job's
  `DocQLError` for a `failed` one. It gives up after 15 minutes by default
  (`timeout=` in seconds) with `DocQLWaitTimeoutError`, and never sends a poll
  at or past that deadline. It never cancels the job: `get_job` or another
  `wait` picks it up again.
- Every submit carries an `Idempotency-Key`, generated for you unless you pass
  `idempotency_key=`; it is on the returned job and on every error the submit
  raises. DocQL scopes the key to your organisation. Unlike `query_document`,
  `submit_document` retries a 503 with the same key (a 503 may mean the job was
  created anyway), but only for a path, `bytes` or a seekable file. A job that
  ended `failed`, `job_interrupted` included, needs a new key: the old one
  replays the failure.
- `job.pages_done` stays 0 while DocQL runs OCR, so show progress as
  indeterminate while a running job reports 0 pages done.

## Webhooks

DocQL signs every webhook delivery with your endpoint's secret, following the
open Standard Webhooks spec. Verify each delivery with `docql.verify_webhook`
before you trust it:

- Read the raw request bytes before any JSON framework parses them. A body that
  was parsed and serialized again never verifies.
- Pass the endpoint secret (`whsec_…`). During the 24 h rotation overlap, pass
  both secrets as a list: a delivery signed with either one verifies. Remove the
  old secret from the list when the overlap ends, and at once if you rolled the
  secret because it leaked: a secret you still pass still verifies.
- The helper does no replay detection. Store each `event.id` and skip one you
  have already seen: the id is the same across retries and resends.
- `event.type` is an open string. Compare it with `docql.EVENT_JOB_SUCCEEDED`,
  `docql.EVENT_JOB_FAILED`, `docql.EVENT_JOB_CANCELLED` or
  `docql.EVENT_WEBHOOK_TEST`, and ignore a type you do not know. Compare
  `event.status` with the `docql.JOB_STATUS_*` constants.
- `tolerance=` (seconds, default 300) is the clock difference allowed between
  DocQL and your receiver, either way.

A refused delivery raises `docql.WebhookVerificationError`, whose `reason` is
one of:

- `missing_header`: a `webhook-id`, `webhook-timestamp` or `webhook-signature`
  header is absent, empty, or repeated with different values
- `invalid_timestamp`: `webhook-timestamp` is not 1 to 12 ASCII digits
- `invalid_signature_header`: `webhook-signature` has no well-formed v1 signature
- `signature_mismatch`: a wrong secret or a changed body
- `timestamp_too_old`: the signature is valid but older than the tolerance;
  check your receiver's clock
- `timestamp_too_new`: the timestamp is further in the future than the tolerance
- `invalid_payload`: the signed body is not a DocQL webhook event

A receiver built on the standard library:

<!-- docql:webhook:start -->
```python
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import docql

SECRET = os.environ["DOCQL_WEBHOOK_SECRET"]  # whsec_..., from the console
MAX_BODY = 1024 * 1024


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            length = int(self.headers["Content-Length"])
        except (TypeError, ValueError):  # absent or not a number
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.send_response(413 if length > MAX_BODY else 400)
            self.end_headers()
            return
        body = self.rfile.read(length)  # the raw bytes, before any JSON parsing
        try:
            event = docql.verify_webhook(body, self.headers, SECRET)
        except docql.WebhookVerificationError as err:
            self.log_error("webhook refused: %s", err.reason)
            self.send_response(400)
            self.end_headers()
            return
        except ValueError as err:  # a malformed secret: fix the configuration
            self.log_error("%s", err)
            self.send_response(500)
            self.end_headers()
            return
        print(f"event {event.type} {event.id}")  # store event.id; skip one already seen
        self.send_response(204)
        self.end_headers()


HTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8000"))), Receiver).serve_forever()
```
<!-- docql:webhook:end -->

## Configuration

| Keyword | Env variable | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `DOCQL_API_KEY` | none | required; `ValueError` at construction when neither is set |
| `api_url` | `DOCQL_API_URL` | `https://api-docql.finoniq.com` | trailing `/` ignored; `http://` allowed for local stacks; the default is not live yet |
| `timeout` | — | 660 s read / 10 s connect | a number sets read/write/pool; an `httpx.Timeout` gives full control; applied to every request, even with an injected client |
| `http_client` | — | none | an injected `httpx.Client` is never closed by the SDK; the SDK still sets its own timeout per request and never follows redirects |

Keyword arguments win over the environment variables. A missing key raises
`ValueError` before any upload.

**Local stacks behind Caddy `tls internal`:** the local CA is private and httpx
does not trust it. Export the root certificate path in `SSL_CERT_FILE`, or
inject a client that trusts it explicitly:

```python
import ssl

import httpx
from docql import DocQL

client = DocQL(http_client=httpx.Client(verify=ssl.create_default_context(cafile="root.crt")))
```

Pass an `ssl.SSLContext`, not a path string — httpx 0.28 deprecates
`verify="<path>"`. Never disable certificate verification.

## Files

`query_document` accepts a path (`str` or `os.PathLike`), raw `bytes`, or a
binary file object:

- The filename rule: an explicit `filename=` is sent verbatim; otherwise a path
  uses its basename; otherwise a file object uses the basename of its `.name`.
  `bytes` and unnamed streams need `filename=` — there is no default filename.
- A file object is read from its current position and never closed. A file the
  SDK opened from a path is closed by the SDK.
- There are no client-side type or size checks — the server is the authority.
- A stream whose length cannot be measured is sent chunked; an oversized one
  may answer `503` instead of `413`. The API rejects files over 25 MiB.
- A CSV is recognised only by a file name that ends in `.csv`, in any letter
  case (`sales.csv`, `SALES.CSV`). When you send `bytes` or a stream, pass the
  name with `filename=`. The name is sent exactly as given and never trimmed,
  so `"a.csv "` with a trailing space is refused with `unsupported_file_type`.

## Spreadsheets

`.xlsx`, `.ods` and `.csv` files are sent like any other file. A refused
spreadsheet raises `docql.DocQLError`, whose `metadata` is the server's raw
refusal object. For `spreadsheet_too_large` it holds the measured value under
its axis key (`cells`, `runes`, `estimated_output_tokens` or `bytes`), plus
`limit`, `reason` and `request_id`. Find the axis by name, not by key order:

<!-- docql:spreadsheet:start -->
```python
from pathlib import Path

import docql
from docql import DocQL

with DocQL() as client:  # reads DOCQL_API_KEY and DOCQL_API_URL
    data = Path("sales.csv").read_bytes()
    try:
        result = client.query_document(data, query="{ region total_sales }", filename="sales.csv")
        print(result.page_count)
    except docql.DocQLError as err:
        if err.reason != docql.REASON_SPREADSHEET_TOO_LARGE:
            raise
        axes = ("cells", "runes", "estimated_output_tokens", "bytes")
        axis = next((k for k in axes if k in err.metadata), None)
        if axis is None:
            raise
        print(f"too large: {axis}={err.metadata[axis]} limit={err.metadata['limit']}")
```
<!-- docql:spreadsheet:end -->

## Modes

`docql.MODE_FAST` and `docql.MODE_STANDARD` name the two processing modes.
`mode` is passed through and validated by the server; omitting it means the
server default.

## Errors

```
DocQLException
├── DocQLError                # the server answered non-2xx
└── DocQLConnectionError      # no response arrived
    └── DocQLTimeoutError     # the request timed out
```

`DocQLError` carries `status`, `reason`, `request_id`, `retry_after`,
`retryable`, `message` (the envelope's `error_info`) and `body` (the response
text, truncated to about 2 KB). Every reason below is also exported as a
`docql.REASON_*` constant (`invalid_api_key` is
`docql.REASON_INVALID_API_KEY`).

| Status | Reason | What to do |
| --- | --- | --- |
| 401 | `invalid_api_key` | the key is wrong or revoked — check `DOCQL_API_KEY` or issue a new key |
| 413 | `upload_too_large` | the file exceeds the API limit (25 MiB) — shrink or split it |
| 415 | `unsupported_media_type` | the request was not multipart/form-data — send it through `query_document` |
| 422 | `missing_file_part` | the multipart body has no file part |
| 422 | `missing_body_field` | the multipart body has no `body` form field |
| 422 | `body_not_json` | the `body` form field is not valid JSON |
| 422 | `missing_query_or_prompt` | pass exactly one of `query=` or `prompt=` |
| 422 | `invalid_params` | check the parameters — for example an unknown `mode` |
| 422 | `unsupported_file_type` | the API does not accept this file type |
| 422 | `corrupted_file` | the file could not be parsed |
| 422 | `document_exceeds_time_budget` | the document is too large for the mode — try `docql.MODE_STANDARD` or split it |
| 429 | `service_overloaded` | retryable — wait `retry_after` seconds and retry |
| 502 | `upstream_auth_failed` | not retryable — quote `request_id` when contacting support |
| 503 | `upstream_unavailable` | retryable — wait `retry_after` seconds and retry |
| any | any other reason | read `message`; only 429 and 503 set `retryable` |
| any | `reason` is `None` | a non-DocQL response: a framework 404/405 means a wrong `api_url`; an empty 5xx or an HTML 524 from an edge means the call may still have been processed |

## Retrying

`query_document` never retries — every call is billed. Retry it yourself, only on
`retryable` errors, sleeping `retry_after` when the server sent one and a
capped exponential backoff otherwise:

```python
import time

from docql import DocQL, DocQLError

client = DocQL()
attempt = 0
while True:
    try:
        result = client.query_document("invoice.pdf", query="{ invoice_number total_amount }")
        break
    except DocQLError as err:
        attempt += 1
        if not err.retryable or attempt >= 5:
            raise
        time.sleep(err.retry_after if err.retry_after is not None else min(2**attempt, 30))
```

## Timeouts and billing

Usage is metered in pages. A timed-out call may still have been billed — the
server may finish the work after the client gave up. For the same reason,
502/504/524 answers are not `retryable`: only 429 and 503 are, because those
two mean the request was not processed.

## Versioning

The SDK follows [Semantic Versioning](https://semver.org/). While the major
version is 0, a minor release may change the API; pin `docql>=0.4,<0.5` until
1.0.

## Support

When asking for help, quote the `request_id` — `result.request_id` on success,
`err.request_id` on a `DocQLError`. Never share your API key, and never paste a
full exception dump: a chained exception can hold the request headers. Report
security issues through [SECURITY.md](SECURITY.md).

## License

Apache-2.0 — see [LICENSE](LICENSE).

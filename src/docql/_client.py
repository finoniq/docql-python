"""The DocQL client: config resolution, per-request send, error mapping."""

import os
import platform
from types import TracebackType
from typing import BinaryIO, Self

import httpx

from docql._constants import (
    CONNECT_TIMEOUT,
    DEFAULT_API_URL,
    DEFAULT_TIMEOUT,
    ENV_API_KEY,
    ENV_API_URL,
    ERROR_BODY_READ_LIMIT,
    MAX_UPLOAD_BYTES,
    QUERY_PATH,
)
from docql._errors import DocQLConnectionError, DocQLTimeoutError
from docql._request import Upload, _ShortReadError, prepare_upload
from docql._response import QueryResult, decode_error, parse_result
from docql._version import __version__

# h11/httpcore failures (a raw h11.LocalProtocolError is not an httpx type)
# are re-mapped by module origin so the undeclared h11 dependency is never
# imported (research Pitfall 1).
_INTERNAL_MODULE_ROOTS = ("h11", "httpcore")


def _read_error_body(response: httpx.Response) -> bytes:
    """Read at most ``ERROR_BODY_READ_LIMIT`` bytes of a non-2xx body (T-11-12).

    Slicing each chunk keeps the bound even when a transport yields chunks
    larger than the limit; the caller closes the response right after.
    """
    parts: list[bytes] = []
    remaining = ERROR_BODY_READ_LIMIT
    for chunk in response.iter_bytes():
        if len(chunk) >= remaining:
            parts.append(chunk[:remaining])
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


class DocQL:
    """Client for the DocQL document-extraction API.

    Keyword-only construction (D-01): ``api_key`` then ``DOCQL_API_KEY``;
    ``api_url`` then ``DOCQL_API_URL`` (empty treated as unset) then the
    production default. The key travels on each request, never as a client
    default, and redirects are never followed, so it cannot leave the
    configured host.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_url: str | None = None,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
        http_client: httpx.Client | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get(ENV_API_KEY)
        if key is None or not key.strip():
            raise ValueError(
                "DocQL API key missing: pass api_key= or set the "
                f"{ENV_API_KEY} environment variable"
            )
        self._api_key = key

        if api_url is not None:
            url = api_url
        else:
            env_url = os.environ.get(ENV_API_URL, "")
            url = env_url.strip() or DEFAULT_API_URL
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError(
                "DocQL api_url must be an http:// or https:// URL: pass "
                f"api_url= or set {ENV_API_URL} (got {url!r})"
            )
        try:
            httpx.URL(url)
        except httpx.InvalidURL as exc:
            raise ValueError(
                "DocQL api_url is not a URL httpx can parse: pass api_url= "
                f"or set {ENV_API_URL} (got {url!r})"
            ) from exc
        self._api_url = url.rstrip("/")

        if isinstance(timeout, httpx.Timeout):
            self._timeout = timeout
        elif isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            # D-02 float form: read/write/pool = the float, connect stays 10 s.
            self._timeout = httpx.Timeout(float(timeout), connect=CONNECT_TIMEOUT)
        else:
            raise TypeError(
                "timeout must be a float (seconds) or an httpx.Timeout, "
                f"not {type(timeout).__name__!r}"
            )

        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.Client(timeout=DEFAULT_TIMEOUT)
            self._owns_client = True

    def _scrub(self, text: str) -> str:
        """Keep the API key out of every message this client builds."""
        return text.replace(self._api_key, "***")

    def _connection_message(self, exc: Exception, upload: Upload) -> str:
        if isinstance(exc, _ShortReadError):
            text = f"the file changed size during upload: {exc}"
        else:
            text = f"connection to {self._api_url} failed: {type(exc).__name__}"
        if upload.file_size is not None and upload.file_size > MAX_UPLOAD_BYTES:
            text += f" (the upload is {upload.file_size} bytes; the API rejects files over 25 MiB)"
        return self._scrub(text)

    def query_document(
        self,
        file: str | os.PathLike[str] | bytes | BinaryIO,
        *,
        query: str | None = None,
        prompt: str | None = None,
        mode: str | None = None,
        filename: str | None = None,
    ) -> QueryResult:
        """Upload one document and answer its extraction result (D-03/D-04).

        ``file`` is a path (``str`` or ``os.PathLike``), raw ``bytes``, or a
        binary file object read from its current position and never closed
        (D-13/D-16).
        """
        with prepare_upload(
            file, query=query, prompt=prompt, mode=mode, filename=filename
        ) as upload:
            headers: dict[str, str] = {
                "X-API-Key": self._api_key,
                "Content-Type": upload.content_type,
                "User-Agent": (
                    f"docql-python/{__version__} "
                    f"(python {platform.python_version()}; httpx {httpx.__version__})"
                ),
            }
            if upload.content_length is not None:
                headers["Content-Length"] = str(upload.content_length)
            request = self._client.build_request(
                "POST",
                self._api_url + QUERY_PATH,
                content=upload.iter_bytes(),
                headers=headers,
                timeout=self._timeout,
            )
            # build_request merges an injected client's default headers; drop
            # the forbidden ones so tenant headers never leave the process
            # (T-11-05). The key stays a per-request header only.
            for header_name in list(request.headers.keys()):
                lowered = header_name.lower()
                if lowered == "x-request-id" or lowered.startswith("x-docql-"):
                    del request.headers[header_name]
            # One boundary around the send AND the body read (D-09): a timeout
            # or transport failure raised while streaming the response body is
            # mapped exactly like a send failure, so no httpx or h11 exception
            # ever reaches the caller. Nothing here retries: calls are paid.
            response: httpx.Response | None = None
            body: bytes
            try:
                response = self._client.send(request, follow_redirects=False, stream=True)
                body = (
                    response.read()
                    if 200 <= response.status_code < 300
                    else _read_error_body(response)
                )
            except httpx.TimeoutException as exc:
                raise DocQLTimeoutError(
                    self._scrub(f"DocQL request timed out after {self._timeout}")
                ) from exc
            except (_ShortReadError, httpx.RequestError) as exc:
                raise DocQLConnectionError(self._connection_message(exc, upload)) from exc
            except Exception as exc:
                if type(exc).__module__.split(".", 1)[0] in _INTERNAL_MODULE_ROOTS:
                    raise DocQLConnectionError(self._connection_message(exc, upload)) from exc
                raise
            finally:
                if response is not None:
                    response.close()
            if 200 <= response.status_code < 300:
                return parse_result(response.status_code, response.headers, body, self._api_key)
            raise decode_error(response.status_code, response.headers, body, self._api_key)

    def close(self) -> None:
        """Close the httpx client the SDK created; an injected one stays open (D-06)."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"DocQL(api_url={self._api_url!r})"

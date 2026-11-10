"""Async client for the Miragea Agent Audio API (https://miragea.ai/v1).

Only the endpoints documented at https://miragea.ai/v1/guide and used by the
official miragea-audio skill are wrapped:

    GET  /capabilities
    GET  /actors?limit=&offset=
    POST /audio/generations          (JSON, Idempotency-Key)
    POST /subtitles/recognitions     (multipart: file + expected_rate, Idempotency-Key)
    GET  /tasks/{task_id}
    GET  /tasks?limit=
"""

from __future__ import annotations

import asyncio
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote, urlsplit

import httpx

from . import __version__
from .config import API_KEY_PAGE, MISSING_KEY_MESSAGE, Settings

# Cloudflare in front of miragea.ai rejects some default library user agents
# (e.g. Python-urllib gets HTTP 403), so always identify as a normal client.
USER_AGENT = (
    f"Mozilla/5.0 (compatible; youtube-dubbing-mcp/{__version__}; "
    "+https://github.com/RTY2345RR/youtube-dubbing-mcp) python-httpx"
)

TERMINAL_STATES = ("succeeded", "failed")
RETRYABLE_HTTP = (502, 503, 504)

STATUS_HINTS = {
    400: "Fix the invalid or missing parameter (model, tags, speed, text length).",
    401: f"The API key was rejected. Check MIRAGEA_API_KEY or create a new key at {API_KEY_PAGE}.",
    403: "This account has no API access right now. Check API access for your Miragea account.",
    404: "Task not found for this account. Check the task_id.",
    409: "Idempotency key reused with different input, or the subtitle rate changed. "
    "Re-check capabilities and submit again with a new request.",
    413: "The upload is too large. Trim or compress the media file.",
    429: "Rate limited or the account limit was reached. Check your Miragea account, or wait and retry.",
}

ProgressFn = Callable[[str], Awaitable[None]]


class MirageaError(Exception):
    """API or client error with a structured, secret-free payload."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: Optional[int] = None,
        param: Optional[str] = None,
        retryable: bool = False,
        task_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        hint: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.param = param
        self.retryable = retryable
        self.task_id = task_id
        self.idempotency_key = idempotency_key
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "message": self.message}
        for name in ("http_status", "param", "task_id", "idempotency_key", "hint"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        data["retryable"] = self.retryable
        return data

    def __str__(self) -> str:
        parts = [f"[{self.code}] {self.message}"]
        if self.http_status:
            parts.append(f"(HTTP {self.http_status})")
        if self.param:
            parts.append(f"param={self.param}")
        if self.hint:
            parts.append(f"Hint: {self.hint}")
        if self.task_id:
            parts.append(f"task_id={self.task_id} (re-run the same tool to resume; do not resubmit)")
        if self.idempotency_key:
            parts.append(f"idempotency_key={self.idempotency_key}")
        return " ".join(parts)


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


class MirageaClient:
    """Thin, robust wrapper: auth, retries with the same idempotency key, polling, downloads."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        max_retries: int = 2,
        backoff: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not settings.has_key:
            raise MirageaError("missing_api_key", MISSING_KEY_MESSAGE)
        self.settings = settings
        self._max_retries = max_retries
        self._backoff = backoff
        self._sleep = sleep
        self._transport = transport
        self._api = httpx.AsyncClient(
            base_url=settings.base_url,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=settings.http_timeout,
            follow_redirects=False,  # never forward the bearer token to another host
            transport=transport,
        )

    async def __aenter__(self) -> "MirageaClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._api.aclose()

    # ------------------------------------------------------------------ core
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        attempt = 0
        while True:
            try:
                response = await self._api.request(
                    method, path, params=params, json=json_body, files=files, data=data, headers=headers
                )
            except httpx.HTTPError as exc:
                # Safe to retry: GETs are read-only and POSTs reuse the same idempotency key.
                if attempt < self._max_retries:
                    attempt += 1
                    await self._sleep(self._backoff * attempt)
                    continue
                outcome = (
                    " The submission outcome is unknown; retry with the same idempotency_key and identical input."
                    if idempotency_key
                    else ""
                )
                raise MirageaError(
                    "request_failed",
                    _redact(f"Could not reach the Miragea API ({type(exc).__name__}).{outcome}", self.settings.api_key),
                    retryable=True,
                    idempotency_key=idempotency_key,
                ) from None

            if response.status_code in RETRYABLE_HTTP and attempt < self._max_retries:
                attempt += 1
                await self._sleep(self._backoff * attempt)
                continue
            return self._parse(response, idempotency_key)

    def _parse(self, response: httpx.Response, idempotency_key: Optional[str]) -> dict[str, Any]:
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        if 200 <= status < 300 and isinstance(body, dict):
            return body
        if 300 <= status < 400:
            raise MirageaError(
                "unexpected_redirect",
                "The API answered with a redirect; check MIRAGEA_BASE_URL (it must include /v1).",
                http_status=status,
            )
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "api_error")
            message = str(err.get("message") or f"Miragea API returned HTTP {status}.")
            param = err.get("param")
            retryable = bool(err.get("retryable", False))
        elif 200 <= status < 300:
            code, message, param, retryable = "invalid_response", "The API returned a non-JSON response.", None, False
        else:
            code = "cloudflare_blocked" if status == 403 and body is None else "http_error"
            message = f"Miragea API returned HTTP {status}."
            param, retryable = None, status in RETRYABLE_HTTP
        raise MirageaError(
            code,
            _redact(message, self.settings.api_key),
            http_status=status,
            param=param if isinstance(param, str) else None,
            retryable=retryable,
            idempotency_key=idempotency_key,
            hint=STATUS_HINTS.get(status),
        )

    # ------------------------------------------------------------- endpoints
    async def capabilities(self) -> dict[str, Any]:
        return await self._request("GET", "/capabilities")

    async def list_actors(self, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise MirageaError("invalid_argument", "limit must be between 1 and 100.", param="limit")
        if offset < 0:
            raise MirageaError("invalid_argument", "offset must be 0 or greater.", param="offset")
        return await self._request("GET", "/actors", params={"limit": limit, "offset": offset})

    async def create_audio(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return await self._request("POST", "/audio/generations", json_body=payload, idempotency_key=idempotency_key)

    async def create_recognition(
        self, file_path: Path, expected_rate: Any, idempotency_key: str
    ) -> dict[str, Any]:
        filename = file_path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        content = file_path.read_bytes()
        return await self._request(
            "POST",
            "/subtitles/recognitions",
            files={"file": (filename, content, mime)},
            data={"expected_rate": str(expected_rate)},
            idempotency_key=idempotency_key,
        )

    async def get_task(self, task_id: str) -> dict[str, Any]:
        if not task_id or not task_id.strip():
            raise MirageaError("invalid_argument", "task_id is required.", param="task_id")
        return await self._request("GET", "/tasks/" + quote(task_id.strip(), safe=""))

    async def list_tasks(self, limit: int = 20) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise MirageaError("invalid_argument", "limit must be between 1 and 100.", param="limit")
        return await self._request("GET", "/tasks", params={"limit": limit})

    # ---------------------------------------------------------------- polling
    async def wait_for_task(
        self,
        task_id: str,
        timeout: Optional[float] = None,
        progress: Optional[ProgressFn] = None,
    ) -> dict[str, Any]:
        timeout = timeout or self.settings.wait_timeout
        deadline = time.monotonic() + timeout
        last_status = None
        while True:
            try:
                task = await self.get_task(task_id)
            except MirageaError as exc:
                exc.task_id = task_id
                raise
            status = task.get("status")
            if status in TERMINAL_STATES:
                return task
            if progress and status != last_status:
                await progress(f"Miragea task {task_id} is {status}")
            last_status = status
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MirageaError(
                    "wait_timeout",
                    f"Task is still {status or 'pending'} after {int(timeout)}s. It keeps running on Miragea.",
                    task_id=task_id,
                    retryable=True,
                    hint="Run the same tool again: it resumes saved task ids instead of submitting new jobs.",
                )
            pause = task.get("poll_after_seconds")
            pause = pause if isinstance(pause, (int, float)) and pause > 0 else 3
            await self._sleep(min(float(pause), max(0.0, remaining)))

    # --------------------------------------------------------------- download
    async def download(self, url: str, destination: Path) -> Path:
        """Stream a result URL to a new file. The API key is never sent to this URL."""
        if urlsplit(url).scheme not in ("http", "https"):
            raise MirageaError("invalid_result", "The task returned an invalid audio_url.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        final = unique_path(destination)
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=max(self.settings.http_timeout, 120.0),
            follow_redirects=True,
            transport=self._transport,
        ) as plain:
            try:
                async with plain.stream("GET", url) as response:
                    if response.status_code >= 400:
                        raise MirageaError(
                            "download_failed",
                            f"Downloading the audio returned HTTP {response.status_code}.",
                            http_status=response.status_code,
                            retryable=response.status_code in RETRYABLE_HTTP,
                        )
                    with final.open("xb") as fh:
                        try:
                            async for chunk in response.aiter_bytes():
                                fh.write(chunk)
                        except BaseException:
                            fh.close()
                            final.unlink(missing_ok=True)
                            raise
            except httpx.HTTPError as exc:
                final.unlink(missing_ok=True)
                raise MirageaError(
                    "download_failed", f"Downloading the audio failed ({type(exc).__name__}).", retryable=True
                ) from None
        return final


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


def unique_path(path: Path) -> Path:
    """Return `path`, or `name-2.ext`, `name-3.ext`... so existing files are never overwritten."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(2, 10_000):
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
    raise MirageaError("output_exists", f"Could not find a free file name next to {path}.")


def write_new_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    final = unique_path(path)
    with final.open("x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return final


__all__ = [
    "MirageaClient",
    "MirageaError",
    "USER_AGENT",
    "new_idempotency_key",
    "unique_path",
    "write_new_text",
]

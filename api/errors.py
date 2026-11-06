from __future__ import annotations

import asyncio
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from services.protocol.error_response import anthropic_error_response, openai_error_response
from services.runtime_configuration import env_int

# Slows every wrong key; FailedAuthLockout stops sustained guessing from one address.
FAILED_AUTH_DELAY_SECONDS = 0.5


class FailedAuthLockout:
    """Locks out a client address after repeated wrong keys within a window."""

    _MAX_TRACKED_ADDRESSES = 4096

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        lockout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return self.limit > 0 and self.lockout_seconds > 0

    def retry_after(self, address: str) -> int:
        """Whole seconds left on the address lockout; 0 when it is not locked."""
        if not self.enabled or not address:
            return 0
        now = self._clock()
        with self._lock:
            until = self._locked_until.get(address)
            if until is None:
                return 0
            if until <= now:
                del self._locked_until[address]
                return 0
            return max(1, math.ceil(until - now))

    def record_failure(self, address: str) -> None:
        if not self.enabled or not address:
            return
        now = self._clock()
        with self._lock:
            attempts = self._failures.setdefault(address, deque())
            while attempts and attempts[0] <= now - self.window_seconds:
                attempts.popleft()
            attempts.append(now)
            if len(attempts) >= self.limit:
                del self._failures[address]
                self._locked_until[address] = now + self.lockout_seconds
            if len(self._failures) > self._MAX_TRACKED_ADDRESSES:
                self._forget_idle_locked(now)

    def _forget_idle_locked(self, now: float) -> None:
        idle = [address for address, attempts in self._failures.items() if attempts[-1] <= now - self.window_seconds]
        for address in idle:
            del self._failures[address]
        while len(self._failures) > self._MAX_TRACKED_ADDRESSES:
            del self._failures[next(iter(self._failures))]
        for address in [address for address, until in self._locked_until.items() if until <= now]:
            del self._locked_until[address]


failed_auth_lockout = FailedAuthLockout(
    limit=env_int("CHATGPT2API_AUTH_FAILURE_LIMIT", 10, minimum=0),
    window_seconds=env_int("CHATGPT2API_AUTH_FAILURE_WINDOW_SECONDS", 300),
    lockout_seconds=env_int("CHATGPT2API_AUTH_LOCKOUT_SECONDS", 900, minimum=0),
)


def _presents_key(headers: Mapping[str, str]) -> bool:
    scheme, _, value = str(headers.get("authorization") or "").partition(" ")
    return (scheme.lower() == "bearer" and bool(value.strip())) or bool(str(headers.get("x-api-key") or "").strip())


def record_failed_key(request: Request) -> None:
    """Count a wrong key against the caller's address; requests without a key do not count."""
    address = request.client.host if request.client else ""
    if _presents_key(request.headers):
        failed_auth_lockout.record_failure(address)


class FailedAuthLockoutMiddleware:
    """Answers 429 to requests that carry a key from a locked-out address, without checking the key."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        address = client[0] if client else ""
        retry_after = failed_auth_lockout.retry_after(address) if _presents_key(Headers(scope=scope)) else 0
        if not retry_after:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        detail = {"error": f"密钥错误次数过多，请 {math.ceil(retry_after / 60)} 分钟后再试"}
        headers = {"Retry-After": str(retry_after)}
        if _is_openai_compatible_path(request.url.path):
            response = _compatible_error_response(request, detail, 429, headers)
        else:
            response = JSONResponse(status_code=429, content={"detail": detail}, headers=headers)
        await response(scope, receive, send)


def _is_openai_compatible_path(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


def _is_anthropic_messages_path(path: str) -> bool:
    return path == "/v1/messages"


def _compatible_error_response(
    request: Request,
    detail: object,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    if _is_anthropic_messages_path(request.url.path):
        return anthropic_error_response(detail, status_code, headers=headers)
    return openai_error_response(detail, status_code, headers=headers)


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 401:
            record_failed_key(request)
            await asyncio.sleep(FAILED_AUTH_DELAY_SECONDS)
        if _is_openai_compatible_path(request.url.path):
            return _compatible_error_response(request, exc.detail, exc.status_code, exc.headers)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": jsonable_encoder(exc.detail)},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        if _is_openai_compatible_path(request.url.path):
            return _compatible_error_response(request, exc.errors(), 422)
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

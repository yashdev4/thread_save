"""HTTP Security and Rate Limiting Middlewares for ThreadVault (§2, S8, Milestone X3)."""

from __future__ import annotations

import re
import time
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from thread_save.web.context import current_account_id

# Allowed origin patterns (§2 S8)
_ALLOWED_ORIGIN_PATTERNS = [
    re.compile(r"^https://claude\.ai$"),
    re.compile(r"^https://claude\.com$"),
    re.compile(r"^http://localhost(:\d+)?$"),
    re.compile(r"^http://127\.0\.0\.1(:\d+)?$"),
]

MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB limit


def is_allowed_origin(origin: str) -> bool:
    if not origin:
        return True
    return any(pattern.match(origin) for pattern in _ALLOWED_ORIGIN_PATTERNS)


class OriginValidatorMiddleware(BaseHTTPMiddleware):
    """Validate request Origin header against allowlist (§2, S8)."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        origin = request.headers.get("origin")
        if origin and not is_allowed_origin(origin):
            return JSONResponse(
                status_code=403,
                content={"detail": f"Origin '{origin}' is not allowed"},
            )
        return await call_next(request)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Enforce maximum body size per request (§2, S8)."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large"},
                    )
            except ValueError:
                pass
        return await call_next(request)


class RateLimiter:
    """Sliding-window in-memory rate limiter per key."""

    def __init__(self, max_requests: int = 60, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._history: dict[str, list[float]] = {}

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        records = self._history.setdefault(key, [])
        # Prune old records
        self._history[key] = [t for t in records if t > cutoff]
        if len(self._history[key]) >= self.max_requests:
            return False
        self._history[key].append(now)
        return True

    def reset(self):
        self._history.clear()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-account / per-IP rate limiting (§2, S8)."""

    def __init__(self, app, limiter: RateLimiter | None = None):
        super().__init__(app)
        self.limiter = limiter or RateLimiter(max_requests=60, window_seconds=60.0)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Key by account ID header if present, otherwise client IP
        key = (
            request.headers.get("x-account-id")
            or (request.client.host if request.client else "unknown")
        )
        if not self.limiter.is_allowed(key):
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": "60"},
            )
        return await call_next(request)


class AccountContextMiddleware(BaseHTTPMiddleware):
    """Bind account ID from headers or query params to request context."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        account = (
            request.headers.get("x-account-id")
            or request.query_params.get("account")
            or "default"
        )
        token = current_account_id.set(account)
        try:
            return await call_next(request)
        finally:
            current_account_id.reset(token)

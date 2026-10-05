"""Pre-deploy safety startup validation and environment verification.

Enforces:
1. Fail-closed authentication:
   - THREADVAULT_ENFORCE_AUTH defaults to true.
   - Refuses to start with auth disabled unless bound to localhost (127.0.0.1 / ::1 / localhost).
2. Server key security:
   - THREADVAULT_SERVER_KEY required unless running locally.
   - No built-in default key anywhere.
3. Production configuration integrity:
   - Explicit startup check that names every missing required environment variable.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("thread_save.web.startup")

REQUIRED_PROD_ENV_VARS = (
    "DATABASE_URL",
    "THREADVAULT_PUBLIC_URL",
    "THREADVAULT_JWT_SECRET",
    "THREADVAULT_VIEWER_SECRET",
    "THREADVAULT_SERVER_KEY",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "testserver")


def is_localhost_bound(host: Optional[str] = None) -> bool:
    """Check if the server is bound strictly to loopback/localhost."""
    # Cloud hosting environments are never considered loopback-bound
    if (
        os.environ.get("ENVIRONMENT") == "production"
        or os.environ.get("APP_ENV") == "production"
        or os.environ.get("FLY_APP_NAME")
        or os.environ.get("RENDER")
        or os.environ.get("THREADVAULT_FORCE_PRODUCTION")
    ):
        return False

    if host is not None:
        h = host.strip().lower()
        return h in LOOPBACK_HOSTS

    env_host = os.environ.get("THREADVAULT_HOST") or os.environ.get("HOST")
    if env_host is not None:
        h = env_host.strip().lower()
        return h in LOOPBACK_HOSTS

    # Default in local testing/development is loopback
    return True


def get_public_url() -> str:
    """Return configured canonical public URL (THREADVAULT_PUBLIC_URL).

    Used for OAuth issuer, all metadata URLs, and Google redirect URI.
    Never derived from the incoming request in production.
    """
    url = os.environ.get("THREADVAULT_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if url and url.strip():
        return url.strip().rstrip("/")

    if not is_localhost_bound():
        raise RuntimeError(
            "THREADVAULT_PUBLIC_URL is required in non-local environments; cannot derive from request"
        )

    return "http://localhost:8000"


def validate_startup_requirements(
    host: Optional[str] = None,
    enforce_auth: Optional[bool] = None,
) -> None:
    """Validate server startup safety rules before serving traffic.

    Raises RuntimeError with clear descriptions if safety rules are violated.
    """
    is_local = is_localhost_bound(host)

    # 1. Resolve enforce_auth (defaults to true)
    if enforce_auth is None:
        raw_val = os.environ.get("THREADVAULT_ENFORCE_AUTH", "true").strip().lower()
        enforce_auth_effective = raw_val in ("true", "1", "yes")
    else:
        enforce_auth_effective = bool(enforce_auth)

    # 2. Refuse to start with auth disabled unless bound to localhost
    if not enforce_auth_effective and not is_local:
        raise RuntimeError(
            "THREADVAULT_ENFORCE_AUTH cannot be disabled unless bound to localhost (127.0.0.1 / ::1)"
        )

    # 3. Non-local / production environment checks
    if not is_local:
        missing: list[str] = []
        for var in REQUIRED_PROD_ENV_VARS:
            val = os.environ.get(var)
            if var == "THREADVAULT_PUBLIC_URL" and not (val and val.strip()):
                val = os.environ.get("RENDER_EXTERNAL_URL")
            if not val or not val.strip():
                missing.append(var)

        if missing:
            raise RuntimeError(
                f"Production startup failed: missing required environment variables: {', '.join(missing)}"
            )

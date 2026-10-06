"""Render pre-deploy step: run database migrations only when Postgres is the store.

The web app uses FileStore when THREADVAULT_STORAGE_BACKEND=file or DATABASE_URL
is unset (web/app.py); then there is nothing to migrate, and alembic (not a
dependency) would fall back to the local test URL in alembic.ini and fail the
deploy. With Postgres, `alembic upgrade head` runs and its exit code is returned.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping


def uses_postgres(env: Mapping[str, str] = os.environ) -> bool:
    """Same store choice as web/app.py create_app."""
    backend = env.get("THREADVAULT_STORAGE_BACKEND", "").strip().lower()
    return backend != "file" and bool(env.get("DATABASE_URL"))


def main() -> int:
    if not uses_postgres():
        print("pre-deploy: file storage, no database migrations to run")
        return 0
    return subprocess.call([sys.executable, "-m", "alembic", "upgrade", "head"])


if __name__ == "__main__":
    sys.exit(main())

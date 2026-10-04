import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import shutil

from thread_save.config import VaultConfig
from thread_save.storage.path_resolver import (
    build_filename,
    resolve_thread_path,
    validate_slug,
)


class TestSlugValidation(unittest.TestCase):
    def test_slug_validation_evil(self):
        with self.assertRaises(ValueError):
            validate_slug("../evil")

    def test_slug_validation_evil_in_build_filename(self):
        now = datetime.now(timezone.utc)
        with self.assertRaises(ValueError):
            build_filename(now, "account", "abc123", "../evil", 1)

    def test_slug_validation_evil_in_resolve_thread_path(self):
        tmp = tempfile.mkdtemp()
        try:
            config = VaultConfig(vault_root=Path(tmp))
            now = datetime.now(timezone.utc)
            with self.assertRaises(ValueError):
                resolve_thread_path(config, "default", now, "abc123", "../evil", 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_slug_validation_valid(self):
        validate_slug("valid-slug-1")
        now = datetime.now(timezone.utc)
        fn = build_filename(now, "default", "abc123", "valid-slug", 1)
        self.assertIn("valid-slug", fn)


if __name__ == "__main__":
    unittest.main()

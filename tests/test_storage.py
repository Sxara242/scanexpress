import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storage import initialize_storage, resolve_data_dir


class StorageBootstrapTests(unittest.TestCase):
    def test_resolve_data_dir_uses_environment_override(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"SCANEXPRESS_DATA_DIR": temp_dir}, clear=False
        ):
            self.assertEqual(resolve_data_dir(), Path(temp_dir))

    def test_initialize_storage_creates_private_layout_and_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "data"
            paths = initialize_storage(root)

            self.assertEqual(paths.root, root)
            self.assertTrue(paths.database.is_file())
            self.assertTrue(paths.pending.is_dir())
            self.assertTrue(paths.working.is_dir())
            self.assertTrue(paths.previews.is_dir())

            with sqlite3.connect(paths.database) as connection:
                version = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }

            self.assertEqual(version, 1)
            self.assertTrue(
                {"scan_sessions", "pending_documents", "pending_metadata"}.issubset(
                    table_names
                )
            )

    def test_initialize_storage_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "data"
            first = initialize_storage(root)
            second = initialize_storage(root)

            self.assertEqual(first, second)
            with sqlite3.connect(second.database) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = 1"
                ).fetchone()[0]

            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()

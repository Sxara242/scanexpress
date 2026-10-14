import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storage import (
    apply_paperless_task_status,
    create_pending_document,
    get_pending_document,
    initialize_storage,
    list_pending_documents,
    resolve_data_dir,
    resolve_pending_pdf_path,
    set_pending_submission_state,
    update_pending_metadata,
)


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
                pending_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(pending_documents)"
                    )
                }

            self.assertEqual(version, 2)
            self.assertTrue(
                {"scan_sessions", "pending_documents", "pending_metadata"}.issubset(
                    table_names
                )
            )
            self.assertTrue(
                {"username", "original_filename", "device_name"}.issubset(
                    pending_columns
                )
            )

    def test_initialize_storage_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "data"
            first = initialize_storage(root)
            second = initialize_storage(root)

            self.assertEqual(first, second)
            with sqlite3.connect(second.database) as connection:
                versions = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()

            self.assertEqual(versions, [(1,), (2,)])


class PendingDocumentStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "data"
        self.paths = initialize_storage(self.root)

    def _source_pdf(self, name: str = "source.pdf") -> Path:
        path = Path(self.temp_dir.name) / name
        path.write_bytes(b"%PDF-1.4\n%fake pending pdf\n")
        return path

    def test_create_pending_document_moves_pdf_and_survives_new_storage_handle(self):
        source = self._source_pdf()
        pending = create_pending_document(
            source,
            username="alice",
            original_filename="Diploma.pdf",
            page_count=2,
            device_name="flatbed",
            title="Diploma",
            paths=self.paths,
        )

        self.assertFalse(source.exists())
        self.assertEqual(pending["state"], "ready")
        self.assertEqual(pending["page_count"], 2)
        self.assertEqual(pending["metadata"]["title"], "Diploma")

        reopened_paths = initialize_storage(self.root)
        reopened = get_pending_document(
            pending["id"],
            "alice",
            paths=reopened_paths,
        )
        pdf_path, download_name = resolve_pending_pdf_path(
            pending["id"],
            "alice",
            paths=reopened_paths,
        )

        self.assertEqual(reopened["id"], pending["id"])
        self.assertTrue(pdf_path.is_file())
        self.assertEqual(download_name, "Diploma.pdf")

    def test_pending_documents_are_scoped_by_username(self):
        pending = create_pending_document(
            self._source_pdf(),
            username="alice",
            original_filename="one.pdf",
            page_count=1,
            paths=self.paths,
        )

        self.assertEqual(len(list_pending_documents("alice", paths=self.paths)), 1)
        self.assertEqual(list_pending_documents("bob", paths=self.paths), [])
        with self.assertRaises(LookupError):
            get_pending_document(pending["id"], "bob", paths=self.paths)

    def test_update_pending_metadata_normalizes_ids_and_tags(self):
        pending = create_pending_document(
            self._source_pdf(),
            username="alice",
            original_filename="one.pdf",
            page_count=1,
            paths=self.paths,
        )

        updated = update_pending_metadata(
            pending["id"],
            "alice",
            {
                "title": " School Certificate ",
                "document_date": "2002-06-10",
                "correspondent_id": "4",
                "document_type_id": 7,
                "storage_path_id": "",
                "tag_ids": [3, "5", 3],
            },
            paths=self.paths,
        )

        self.assertEqual(updated["metadata"]["title"], "School Certificate")
        self.assertEqual(updated["metadata"]["correspondent_id"], 4)
        self.assertEqual(updated["metadata"]["document_type_id"], 7)
        self.assertIsNone(updated["metadata"]["storage_path_id"])
        self.assertEqual(updated["metadata"]["tag_ids"], [3, 5])

    def test_failed_submission_remains_pending_and_can_be_retried(self):
        pending = create_pending_document(
            self._source_pdf(),
            username="alice",
            original_filename="one.pdf",
            page_count=1,
            paths=self.paths,
        )

        failed = set_pending_submission_state(
            pending["id"],
            "alice",
            state="failed",
            error="Paperless unavailable",
            paths=self.paths,
        )
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["last_error"], "Paperless unavailable")
        self.assertEqual(len(list_pending_documents("alice", paths=self.paths)), 1)

        submitting = set_pending_submission_state(
            pending["id"],
            "alice",
            state="submitting",
            task_id="task-1",
            paths=self.paths,
        )
        self.assertEqual(submitting["state"], "submitting")

        submitted = apply_paperless_task_status(
            "alice",
            "task-1",
            task_status="SUCCESS",
            document_id="42",
            result_text="done",
            paths=self.paths,
        )
        self.assertEqual(submitted["state"], "submitted")
        self.assertEqual(submitted["paperless_document_id"], "42")
        self.assertEqual(list_pending_documents("alice", paths=self.paths), [])


if __name__ == "__main__":
    unittest.main()

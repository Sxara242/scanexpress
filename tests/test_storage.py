import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storage import (
    append_document_page,
    apply_paperless_task_status,
    apply_pending_metadata_scope,
    attach_pending_to_batch_session,
    cleanup_document_session_pages,
    complete_scan_session,
    create_pending_document,
    discard_pending_document,
    ensure_scan_session,
    get_active_scan_session,
    get_scan_session,
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

            self.assertEqual(version, 4)
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

            self.assertEqual(versions, [(1,), (2,), (3,), (4,)])


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

    def test_discard_pending_document_removes_record_pdf_and_preview(self):
        pending = create_pending_document(
            self._source_pdf(),
            username="alice",
            original_filename="bad-scan.pdf",
            page_count=1,
            paths=self.paths,
        )
        pdf_path, _ = resolve_pending_pdf_path(
            pending["id"], "alice", paths=self.paths
        )
        preview_path = self.paths.previews / f"{pending['id']}.png"
        preview_path.write_bytes(b"preview")
        with sqlite3.connect(self.paths.database) as connection:
            connection.execute(
                "UPDATE pending_documents SET preview_path = ? WHERE id = ?",
                (str(preview_path), pending["id"]),
            )
            connection.commit()

        discarded = discard_pending_document(
            pending["id"], "alice", paths=self.paths
        )

        self.assertEqual(discarded["id"], pending["id"])
        self.assertFalse(pdf_path.exists())
        self.assertFalse(preview_path.exists())
        self.assertEqual(list_pending_documents("alice", paths=self.paths), [])
        with self.assertRaises(LookupError):
            get_pending_document(pending["id"], "alice", paths=self.paths)

    def test_discard_pending_document_is_user_scoped(self):
        pending = create_pending_document(
            self._source_pdf(),
            username="alice",
            original_filename="private.pdf",
            page_count=1,
            paths=self.paths,
        )
        pdf_path, _ = resolve_pending_pdf_path(
            pending["id"], "alice", paths=self.paths
        )

        with self.assertRaises(LookupError):
            discard_pending_document(
                pending["id"], "bob", paths=self.paths
            )

        self.assertTrue(pdf_path.exists())
        self.assertEqual(
            get_pending_document(pending["id"], "alice", paths=self.paths)["id"],
            pending["id"],
        )

    def test_discard_rejects_submitting_and_submitted_documents(self):
        submitting = create_pending_document(
            self._source_pdf("submitting.pdf"),
            username="alice",
            original_filename="submitting.pdf",
            page_count=1,
            paths=self.paths,
        )
        set_pending_submission_state(
            submitting["id"],
            "alice",
            state="submitting",
            task_id="task-1",
            paths=self.paths,
        )
        with self.assertRaises(RuntimeError):
            discard_pending_document(
                submitting["id"], "alice", paths=self.paths
            )

        submitted = create_pending_document(
            self._source_pdf("submitted.pdf"),
            username="alice",
            original_filename="submitted.pdf",
            page_count=1,
            paths=self.paths,
        )
        set_pending_submission_state(
            submitted["id"],
            "alice",
            state="submitted",
            document_id="42",
            paths=self.paths,
        )
        with self.assertRaises(RuntimeError):
            discard_pending_document(
                submitted["id"], "alice", paths=self.paths
            )

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





class ScanSessionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "data"
        self.paths = initialize_storage(self.root)

    def _source_file(self, name: str, payload: bytes = b"scan-page") -> Path:
        path = Path(self.temp_dir.name) / name
        path.write_bytes(payload)
        return path

    def _source_pdf(self, name: str) -> Path:
        return self._source_file(name, b"%PDF-1.4\n%fake\n")

    def test_document_session_reuses_open_session_and_persists_pages(self):
        first = ensure_scan_session(
            "alice",
            "document",
            device_name="flatbed",
            paths=self.paths,
        )
        second = ensure_scan_session(
            "alice",
            "document",
            device_name="flatbed",
            paths=self.paths,
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["logical_state"], "DOCUMENT_OPEN")

        page1 = self._source_file("page1.tiff")
        page2 = self._source_file("page2.tiff")
        append_document_page(first["id"], "alice", page1, paths=self.paths)
        updated = append_document_page(first["id"], "alice", page2, paths=self.paths)

        self.assertEqual(updated["page_count"], 2)
        self.assertFalse(page1.exists())
        self.assertFalse(page2.exists())

        reopened = get_active_scan_session("alice", paths=initialize_storage(self.root))
        self.assertEqual(reopened["id"], first["id"])
        self.assertEqual(reopened["page_count"], 2)

    def test_mode_switch_is_rejected_until_open_session_is_finished(self):
        session = ensure_scan_session("alice", "document", paths=self.paths)

        with self.assertRaises(RuntimeError):
            ensure_scan_session("alice", "batch", paths=self.paths)

        completed = complete_scan_session(session["id"], "alice", paths=self.paths)
        self.assertEqual(completed["logical_state"], "IDLE")
        self.assertIsNone(get_active_scan_session("alice", paths=self.paths))

    def test_batch_pending_items_receive_stable_sequence(self):
        session = ensure_scan_session("alice", "batch", paths=self.paths)

        first = create_pending_document(
            self._source_pdf("one.pdf"),
            username="alice",
            original_filename="one.pdf",
            page_count=1,
            paths=self.paths,
        )
        second = create_pending_document(
            self._source_pdf("two.pdf"),
            username="alice",
            original_filename="two.pdf",
            page_count=1,
            paths=self.paths,
        )

        first = attach_pending_to_batch_session(
            first["id"], session["id"], "alice", paths=self.paths
        )
        second = attach_pending_to_batch_session(
            second["id"], session["id"], "alice", paths=self.paths
        )

        self.assertEqual(first["sequence"], 1)
        self.assertEqual(second["sequence"], 2)
        self.assertEqual(first["session_id"], session["id"])
        self.assertEqual(second["session_id"], session["id"])
        active = get_active_scan_session("alice", paths=self.paths)
        self.assertEqual(active["item_count"], 2)
        self.assertEqual(active["logical_state"], "BATCH_OPEN")

    def test_discarding_open_batch_item_keeps_sequence_monotonic(self):
        session = ensure_scan_session("alice", "batch", paths=self.paths)

        first = create_pending_document(
            self._source_pdf("first.pdf"),
            username="alice",
            original_filename="first.pdf",
            page_count=1,
            paths=self.paths,
        )
        second = create_pending_document(
            self._source_pdf("second.pdf"),
            username="alice",
            original_filename="second.pdf",
            page_count=1,
            paths=self.paths,
        )
        first = attach_pending_to_batch_session(
            first["id"], session["id"], "alice", paths=self.paths
        )
        second = attach_pending_to_batch_session(
            second["id"], session["id"], "alice", paths=self.paths
        )

        discard_pending_document(first["id"], "alice", paths=self.paths)

        third = create_pending_document(
            self._source_pdf("third.pdf"),
            username="alice",
            original_filename="third.pdf",
            page_count=1,
            paths=self.paths,
        )
        third = attach_pending_to_batch_session(
            third["id"], session["id"], "alice", paths=self.paths
        )

        self.assertEqual(second["sequence"], 2)
        self.assertEqual(third["sequence"], 3)
        active = get_active_scan_session("alice", paths=self.paths)
        self.assertEqual(active["item_count"], 3)

    def test_apply_metadata_to_remaining_keeps_titles_independent(self):
        session = ensure_scan_session("alice", "batch", paths=self.paths)
        items = []
        for index in range(1, 4):
            pending = create_pending_document(
                self._source_pdf(f"{index}.pdf"),
                username="alice",
                original_filename=f"{index}.pdf",
                page_count=1,
                title=f"Title {index}",
                paths=self.paths,
            )
            pending = attach_pending_to_batch_session(
                pending["id"], session["id"], "alice", paths=self.paths
            )
            items.append(pending)

        update_pending_metadata(
            items[0]["id"],
            "alice",
            {
                "title": "Custom first title",
                "document_date": "2026-10-14",
                "correspondent_id": 4,
                "document_type_id": 7,
                "storage_path_id": 9,
                "tag_ids": [1, 2],
            },
            paths=self.paths,
        )

        updated_count = apply_pending_metadata_scope(
            items[0]["id"],
            "alice",
            scope="remaining",
            paths=self.paths,
        )
        self.assertEqual(updated_count, 2)

        second = get_pending_document(items[1]["id"], "alice", paths=self.paths)
        third = get_pending_document(items[2]["id"], "alice", paths=self.paths)
        self.assertEqual(second["metadata"]["title"], "Title 2")
        self.assertEqual(third["metadata"]["title"], "Title 3")
        self.assertEqual(second["metadata"]["document_type_id"], 7)
        self.assertEqual(third["metadata"]["tag_ids"], [1, 2])

    def test_cleanup_document_session_pages_removes_spool_after_completion(self):
        session = ensure_scan_session("alice", "document", paths=self.paths)
        append_document_page(
            session["id"],
            "alice",
            self._source_file("page.tiff"),
            paths=self.paths,
        )
        complete_scan_session(session["id"], "alice", paths=self.paths)
        cleanup_document_session_pages(session["id"], "alice", paths=self.paths)

        session_dir = self.paths.working / session["id"]
        self.assertFalse(session_dir.exists())
        reopened = get_scan_session(session["id"], "alice", paths=self.paths)
        self.assertEqual(reopened["state"], "completed")


if __name__ == "__main__":
    unittest.main()

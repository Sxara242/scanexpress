import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as scan_app
from storage import create_pending_document, initialize_storage, set_pending_submission_state


class RecentUploadsPersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"SCANEXPRESS_DATA_DIR": str(self.root / "data")})
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = initialize_storage()
        self.config = Mock()
        self.config.get_default_user.return_value = "alice"
        config_patch = patch("app.get_config_manager", return_value=self.config)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.client = scan_app.app.test_client()

    def pending(self, username="alice", state="ready", task_id=None, document_id=None):
        source = self.root / "source.pdf"
        source.write_bytes(b"%PDF-1.4\nsynthetic test document\n")
        pending = create_pending_document(
            source, username=username, original_filename="test-upload.pdf",
            device_name="flatbed", page_count=1,
        )
        if state != "ready":
            pending = set_pending_submission_state(
                pending["id"], username, state=state, task_id=task_id,
                document_id=document_id, error="Upload failed" if state == "failed" else None,
            )
        return pending

    def history(self):
        response = self.client.get("/api/recent-uploads")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["recent_uploads"]

    def fresh_process_history(self):
        result = subprocess.run(
            [sys.executable, "-c", "import app,json; print(json.dumps(app._list_recent_uploads_for_user('alice')))"],
            cwd=Path(scan_app.__file__).parent, check=True, capture_output=True, text=True,
        )
        return json.loads(result.stdout)

    def test_existing_submitted_document_is_restored_without_ready_or_other_users(self):
        self.pending()
        submitted = self.pending(state="submitted", task_id="saved-task", document_id="42")
        self.pending(username="bob", state="submitted", task_id="bob-task", document_id="99")
        with sqlite3.connect(self.paths.database) as connection:
            before = connection.execute("SELECT * FROM pending_documents ORDER BY id").fetchall()
        entries = self.history()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["task_id"], "saved-task")
        self.assertEqual(entry["related_document"], "42")
        self.assertEqual(entry["task_status"], "SUCCESS")
        self.assertEqual(entry["file_name"], "test-upload.pdf")
        self.assertEqual(entry["device_name"], "flatbed")
        self.assertFalse(entry["is_polling"])
        self.assertEqual(self.fresh_process_history(), entries)
        with sqlite3.connect(self.paths.database) as connection:
            self.assertEqual(connection.execute("SELECT state FROM pending_documents WHERE id = ?", (submitted["id"],)).fetchone()[0], "submitted")
            self.assertEqual(connection.execute("SELECT * FROM pending_documents ORDER BY id").fetchall(), before)

    def test_version_3_upgrade_preserves_populated_legacy_records(self):
        legacy_root = self.root / "legacy"
        legacy_root.mkdir()
        database = legacy_root / "scanexpress.sqlite3"
        # Independent v3 fixture: no v4 initializer or current document helpers.
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript("""
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO schema_migrations VALUES
                    (1, '2026-09-01 10:00:00'), (2, '2026-09-02 10:00:00'),
                    (3, '2026-09-03 10:00:00');
                CREATE TABLE scan_sessions (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK (mode IN ('document', 'batch')),
                    state TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    username TEXT NOT NULL DEFAULT '', device_name TEXT,
                    page_count INTEGER NOT NULL DEFAULT 0, item_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                CREATE TABLE pending_documents (
                    id TEXT PRIMARY KEY, session_id TEXT,
                    sequence INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL,
                    pdf_path TEXT, preview_path TEXT,
                    page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
                    paperless_task_id TEXT, paperless_document_id TEXT, last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    username TEXT NOT NULL DEFAULT '', original_filename TEXT, device_name TEXT,
                    FOREIGN KEY (session_id) REFERENCES scan_sessions(id) ON DELETE SET NULL,
                    UNIQUE (session_id, sequence)
                );
                CREATE TABLE pending_metadata (
                    pending_document_id TEXT PRIMARY KEY, title TEXT, document_date TEXT,
                    correspondent_id INTEGER, document_type_id INTEGER, storage_path_id INTEGER,
                    tag_ids_json TEXT NOT NULL DEFAULT '[]',
                    FOREIGN KEY (pending_document_id) REFERENCES pending_documents(id) ON DELETE CASCADE
                );
                CREATE TABLE scan_session_pages (
                    session_id TEXT NOT NULL, sequence INTEGER NOT NULL, file_path TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (session_id, sequence),
                    FOREIGN KEY (session_id) REFERENCES scan_sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX idx_pending_documents_username_state
                    ON pending_documents(username, state, created_at);
                CREATE INDEX idx_pending_documents_task
                    ON pending_documents(username, paperless_task_id);
                CREATE UNIQUE INDEX idx_scan_sessions_one_open_per_user
                    ON scan_sessions(username) WHERE state = 'open';
                CREATE INDEX idx_scan_session_pages_session
                    ON scan_session_pages(session_id, sequence);
                INSERT INTO scan_sessions VALUES
                    ('batch-old', 'batch', 'completed', '2026-09-10 10:00:00',
                     '2026-09-10 11:00:00', 'alice', 'flatbed', 3, 2, NULL),
                    ('document-open', 'document', 'open', '2026-09-11 10:00:00',
                     '2026-09-11 10:05:00', 'bob', 'adf', 2, 0, 'Synthetic scan timeout');
            """)
            files = {}
            for sequence, state in enumerate(("ready", "submitted"), 1):
                pending_id = f"legacy-{state}"
                pdf = legacy_root / f"{pending_id}.pdf"
                preview = legacy_root / f"{pending_id}.png"
                files[pdf] = b"%PDF-1.4 synthetic legacy document"
                files[preview] = b"synthetic legacy preview"
                connection.execute(
                    "INSERT INTO pending_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pending_id, "batch-old", sequence, state, str(pdf), str(preview),
                     sequence, "migrated-task" if state == "submitted" else None,
                     "19" if state == "submitted" else None, None,
                     "2026-09-10 10:01:00", f"2026-09-10 10:0{sequence}:00",
                     "alice", f"{pending_id}.pdf", "flatbed"),
                )
                connection.execute(
                    "INSERT INTO pending_metadata VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (pending_id, f"Synthetic title {sequence}", "2026-09-09",
                     21, 11, 31, '[1, 2, 9]'),
                )
            for sequence in (1, 2):
                page = legacy_root / f"page-{sequence}.tiff"
                files[page] = f"synthetic page {sequence}".encode()
                connection.execute(
                    "INSERT INTO scan_session_pages VALUES (?, ?, ?, ?)",
                    ("document-open", sequence, str(page), "2026-09-11 10:01:00"),
                )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            tables = ("pending_documents", "pending_metadata", "scan_sessions", "scan_session_pages")
            before = {
                table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in tables
            }
            migrations = connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
        for path, contents in files.items():
            path.write_bytes(contents)
        os.environ["SCANEXPRESS_DATA_DIR"] = str(legacy_root)
        for _ in range(2):
            initialize_storage()
            with sqlite3.connect(database) as connection:
                for table in tables:
                    self.assertEqual(
                        connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall(),
                        before[table], table,
                    )
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(connection.execute(
                    "SELECT * FROM schema_migrations WHERE version <= 3 ORDER BY version"
                ).fetchall(), migrations)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = 4"
                ).fetchone()[0], 1)
        entry, = self.history()
        self.assertEqual(entry["task_id"], "migrated-task")
        self.assertEqual(entry["related_document"], "19")
        for path, contents in files.items():
            self.assertEqual(path.read_bytes(), contents)

    def test_duplicate_notifications_and_updates_preserve_original_order_after_restart(self):
        for task_id, timestamp in (("older", 1000), ("newer", 2000)):
            with patch("app._current_time_millis", return_value=timestamp):
                scan_app._register_recent_upload_from_scan_payload({
                    "status": "ok", "username": "alice", "paperless_task_id": task_id,
                    "filename": f"{task_id}.pdf", "device_name": "flatbed",
                })
        with patch("app._current_time_millis", return_value=3000):
            scan_app._register_recent_upload_from_scan_payload({
                "status": "ok", "username": "alice", "paperless_task_id": "older",
                "filename": "older.pdf", "device_name": "flatbed",
            })
        scan_app._upsert_recent_upload_for_user("alice", "older", {
            "submitted_at": 4000, "task_status": "SUCCESS", "is_polling": False,
            "related_document": "42",
        })
        entries = self.fresh_process_history()
        self.assertEqual([entry["task_id"] for entry in entries], ["newer", "older"])
        self.assertEqual([entry["submitted_at"] for entry in entries], [2000, 1000])
        self.assertEqual(entries[1]["task_status"], "SUCCESS")
        self.assertEqual(entries[1]["related_document"], "42")
        with sqlite3.connect(self.paths.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT task_id, submitted_at FROM recent_uploads ORDER BY submitted_at DESC"
            ).fetchall(), [("newer", 2000), ("older", 1000)])

    def test_first_history_update_preserves_existing_pending_submission_timestamp(self):
        self.pending(state="submitting", task_id="existing-task")
        original, = self.history()
        with patch("app._current_time_millis", return_value=original["submitted_at"] + 10000):
            scan_app._register_recent_upload_from_scan_payload({
                "status": "ok", "username": "alice", "paperless_task_id": "existing-task",
            })
        entry, = self.fresh_process_history()
        self.assertEqual(entry["submitted_at"], original["submitted_at"])
        self.assertEqual(entry["task_id"], "existing-task")

    def test_legacy_history_and_failures_survive_new_process_and_upserts_do_not_duplicate(self):
        for username in ("alice", "bob"):
            scan_app._register_recent_upload_from_scan_payload({
                "status": "ok", "username": username, "paperless_task_id": "same-task",
                "filename": f"{username}.pdf", "device_name": "flatbed",
            })
        scan_app._upsert_recent_upload_for_user("alice", "same-task", {"task_status": "SUCCESS", "is_polling": False, "related_document": "7"})
        scan_app._register_recent_upload_failure("alice", "Paperless upload request failed: timeout", "adf", "failure.pdf")
        entries = self.fresh_process_history()
        self.assertEqual(len(entries), 2)
        success = next(entry for entry in entries if entry["task_id"] == "same-task")
        self.assertEqual(success["file_name"], "alice.pdf")
        self.assertEqual(success["related_document"], "7")
        self.assertEqual(success["task_status"], "SUCCESS")
        self.assertEqual(sum(entry["task_status"] == "FAILURE" for entry in entries), 1)

    def test_latest_30_newest_first_and_polling_does_not_reorder_or_duplicate(self):
        for index in range(35):
            self.pending(state="submitted", task_id=f"task-{index:02}", document_id=str(index + 1))
            with sqlite3.connect(self.paths.database) as connection:
                connection.execute("UPDATE pending_documents SET updated_at = ? WHERE paperless_task_id = ?", (f"2026-10-01 12:00:{index:02}", f"task-{index:02}"))
        scan_app._upsert_recent_upload_for_user("alice", "task-10", {"task_status": "SUCCESS", "related_document": "11"})
        entries = self.history()
        self.assertEqual([entry["task_id"] for entry in entries], [f"task-{index:02}" for index in range(34, 4, -1)])
        self.assertEqual(len({entry["task_id"] for entry in entries}), 30)

    def test_direct_document_response_without_task_is_in_history(self):
        pending = self.pending()
        with patch("app._upload_pdf_to_paperless", return_value={"id": 81}):
            response = self.client.post(f"/api/pending/{pending['id']}/submit")
        self.assertEqual(response.status_code, 200)
        entry, = self.history()
        self.assertEqual(entry["related_document"], "81")
        self.assertEqual(entry["task_status"], "SUCCESS")
        self.assertFalse(entry["is_polling"])

    def test_lowercase_success_updates_pending_and_persists_valid_fallback_id(self):
        pending = self.pending(state="submitting", task_id="poll-task")
        response = Mock(status_code=200)
        response.json.return_value = [{"task_id": "poll-task", "status": "success", "related_document": "invalid", "result_data": {"document_id": 56}}]
        self.config.get_user_token.return_value = "synthetic-token"
        self.config.get_paperless_base_url.return_value = "http://paperless.invalid"
        with patch("app.requests.get", return_value=response):
            result = self.client.get("/api/paperless/tasks/poll-task")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["task_status"], "SUCCESS")
        entry, = self.fresh_process_history()
        self.assertEqual(entry["task_id"], "poll-task")
        self.assertEqual(entry["related_document"], "56")
        self.assertEqual(entry["file_name"], "test-upload.pdf")
        self.assertFalse(entry["is_polling"])
        with sqlite3.connect(self.paths.database) as connection:
            state, document_id = connection.execute("SELECT state, paperless_document_id FROM pending_documents WHERE id = ?", (pending["id"],)).fetchone()
        self.assertEqual((state, document_id), ("submitted", "56"))

    def test_pending_failure_and_inflight_states_are_restored(self):
        self.pending(state="failed", task_id="failed-task")
        self.pending(state="submitting", task_id="active-task")
        entries = {entry["task_id"]: entry for entry in self.history()}
        self.assertEqual(entries["failed-task"]["task_status"], "FAILURE")
        self.assertFalse(entries["failed-task"]["is_polling"])
        self.assertEqual(entries["failed-task"]["result_text"], "Upload failed")
        self.assertTrue(entries["active-task"]["is_polling"])


class PaperlessDocumentIdTests(unittest.TestCase):
    def test_extracts_first_valid_positive_id_in_source_priority_order(self):
        cases = [
            ({"related_document": 12, "result_data": {"document_id": 13}}, "12"),
            ({"related_document": " 0012 "}, "12"),
            ({"result_data": {"document_id": "13"}}, "13"),
            ({"related_document_ids": [None, -1, "bad", "14", 15]}, "14"),
            ({"related_document": 0, "result_data": {"document_id": -3}, "related_document_ids": ["16"]}, "16"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                normalized = scan_app._normalize_paperless_task_payload("task", {**payload, "status": "success"})
                self.assertEqual(normalized["related_document"], expected)
                self.assertEqual(normalized["task_status"], "SUCCESS")

    def test_rejects_invalid_ids_and_handles_malformed_optional_fields(self):
        for value in (None, True, False, 0, -1, 1.5, "0", "-2", "2.0", "1e3", "", "abc", {}, [], "https://paperless/documents/12"):
            with self.subTest(value=value):
                normalized = scan_app._normalize_paperless_task_payload("task", {"related_document": value, "result_data": [], "related_document_ids": "42"})
                self.assertIsNone(normalized["related_document"])


if __name__ == "__main__":
    unittest.main()

"""Safety checks for the read-only retention preview (no deletions)."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage import (
    create_pending_document,
    initialize_storage,
    preview_submitted_retention,
    set_pending_submission_state,
)

CURRENT = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)


class SubmittedRetentionPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = initialize_storage(Path(self.temp.name) / "data")
        self.index = 0

    def create(self, state, age_days, document_id="42"):
        self.index += 1
        source = Path(self.temp.name) / f"source-{self.index}.pdf"
        source.write_bytes(b"%PDF-1.4\nTest content\n")
        item = create_pending_document(
            source, username="owner", original_filename=source.name,
            page_count=1, paths=self.paths,
        )
        if state != "ready":
            set_pending_submission_state(
                item["id"], "owner", state=state,
                document_id=document_id, paths=self.paths,
            )
        stamp = (CURRENT - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.paths.database) as connection:
            connection.execute(
                "UPDATE pending_documents SET updated_at = ? WHERE id = ?",
                (stamp, item["id"]),
            )
        return item

    def preview(self, days=30):
        return preview_submitted_retention(
            days=days, now=CURRENT, data_dir=self.paths.root,
        )

    def test_only_old_confirmed_submitted_is_eligible(self):
        old = self.create("submitted", 40)
        self.create("submitted", 2)
        self.create("submitted", 45, document_id=None)
        ready = self.create("ready", 45)
        self.create("failed", 45)
        self.create("submitting", 45)

        result = self.preview()
        self.assertEqual(result["mode"], "preview-only")
        self.assertEqual(result["retention_days"], 30)
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual([row["id"] for row in result["items"]], [old["id"]])
        self.assertGreater(result["eligible_pdf_bytes"], 0)
        self.assertEqual(result["skipped_unsafe_or_missing"], 0)
        self.assertEqual(
            self.preview(days=50)["eligible_count"], 0,
        )
        with sqlite3.connect(self.paths.database) as connection:
            ready_path = connection.execute(
                "SELECT pdf_path FROM pending_documents WHERE id = ?",
                (ready["id"],),
            ).fetchone()[0]
        self.assertTrue(Path(ready_path).is_file())

    def test_preview_does_not_remove_files_or_change_database(self):
        self.create("submitted", 35)
        before = {p.name: p.stat().st_size for p in self.paths.pending.iterdir()}
        with sqlite3.connect(self.paths.database) as connection:
            rows_before = connection.execute(
                "SELECT id, state, pdf_path FROM pending_documents"
            ).fetchall()

        self.preview()

        after = {p.name: p.stat().st_size for p in self.paths.pending.iterdir()}
        with sqlite3.connect(self.paths.database) as connection:
            rows_after = connection.execute(
                "SELECT id, state, pdf_path FROM pending_documents"
            ).fetchall()
        self.assertEqual(before, after)
        self.assertEqual(rows_before, rows_after)

    def test_missing_or_external_pdf_is_excluded(self):
        first = self.create("submitted", 35)
        other = self.create("submitted", 35)
        outside = Path(self.temp.name) / "external.pdf"
        outside.write_bytes(b"do not touch")
        with sqlite3.connect(self.paths.database) as connection:
            first_file = Path(connection.execute(
                "SELECT pdf_path FROM pending_documents WHERE id = ?", (first["id"],)
            ).fetchone()[0])
            connection.execute(
                "UPDATE pending_documents SET pdf_path = ? WHERE id = ?",
                (str(outside), other["id"]),
            )
        first_file.unlink()
        result = self.preview()
        self.assertEqual(result["eligible_count"], 0)
        self.assertEqual(result["skipped_unsafe_or_missing"], 2)
        self.assertEqual(outside.read_bytes(), b"do not touch")

    def test_reject_invalid_retention_days_and_naive_clock(self):
        for invalid in [0, -1, True, 1.5]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                preview_submitted_retention(
                    days=invalid, now=CURRENT, data_dir=self.paths.root,
                )
        with self.assertRaisesRegex(ValueError, "timezone"):
            preview_submitted_retention(
                now=datetime(2026, 10, 15), data_dir=self.paths.root,
            )


if __name__ == "__main__":
    unittest.main()

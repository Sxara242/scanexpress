import argparse
import json
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


_SCHEMA_VERSION = 4
_DEFAULT_DATA_DIR = "~/.local/share/scanexpress"


@dataclass(frozen=True)
class StoragePaths:
    root: Path
    database: Path
    pending: Path
    working: Path
    previews: Path


def resolve_data_dir() -> Path:
    configured = os.getenv("SCANEXPRESS_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(_DEFAULT_DATA_DIR).expanduser()


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _column_names(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table_name})")
    }


def _apply_schema_v1(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS scan_sessions (
            id TEXT PRIMARY KEY,
            mode TEXT NOT NULL CHECK (mode IN ('document', 'batch')),
            state TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS pending_documents (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            sequence INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL,
            pdf_path TEXT,
            preview_path TEXT,
            page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
            paperless_task_id TEXT,
            paperless_document_id TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES scan_sessions(id) ON DELETE SET NULL,
            UNIQUE (session_id, sequence)
        );

        CREATE TABLE IF NOT EXISTS pending_metadata (
            pending_document_id TEXT PRIMARY KEY,
            title TEXT,
            document_date TEXT,
            correspondent_id INTEGER,
            document_type_id INTEGER,
            storage_path_id INTEGER,
            tag_ids_json TEXT NOT NULL DEFAULT '[]',
            FOREIGN KEY (pending_document_id) REFERENCES pending_documents(id) ON DELETE CASCADE
        );
        """
    )


def _apply_schema_v2(connection: sqlite3.Connection) -> None:
    columns = _column_names(connection, "pending_documents")
    additions = {
        "username": "TEXT NOT NULL DEFAULT ''",
        "original_filename": "TEXT",
        "device_name": "TEXT",
    }
    for column_name, declaration in additions.items():
        if column_name not in columns:
            connection.execute(
                f"ALTER TABLE pending_documents ADD COLUMN {column_name} {declaration}"
            )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pending_documents_username_state
        ON pending_documents(username, state, created_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pending_documents_task
        ON pending_documents(username, paperless_task_id)
        """
    )


def _apply_schema_v3(connection: sqlite3.Connection) -> None:
    session_columns = _column_names(connection, "scan_sessions")
    additions = {
        "username": "TEXT NOT NULL DEFAULT ''",
        "device_name": "TEXT",
        "page_count": "INTEGER NOT NULL DEFAULT 0",
        "item_count": "INTEGER NOT NULL DEFAULT 0",
        "last_error": "TEXT",
    }
    for column_name, declaration in additions.items():
        if column_name not in session_columns:
            connection.execute(
                f"ALTER TABLE scan_sessions ADD COLUMN {column_name} {declaration}"
            )

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS scan_session_pages (
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (session_id, sequence),
            FOREIGN KEY (session_id) REFERENCES scan_sessions(id) ON DELETE CASCADE
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_sessions_one_open_per_user
        ON scan_sessions(username)
        WHERE state = 'open';

        CREATE INDEX IF NOT EXISTS idx_scan_session_pages_session
        ON scan_session_pages(session_id, sequence);
        """
    )


def _apply_schema_v4(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS recent_uploads (
            username TEXT NOT NULL,
            task_id TEXT NOT NULL,
            submitted_at INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (username, task_id)
        )
        """
    )


def initialize_storage(data_dir: Path | None = None) -> StoragePaths:
    root = (data_dir or resolve_data_dir()).expanduser()
    pending = root / "pending"
    working = root / "working"
    previews = root / "previews"
    database = root / "scanexpress.sqlite3"

    for directory in (root, pending, working, previews):
        _ensure_private_directory(directory)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        applied_versions = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
        if 1 not in applied_versions:
            _apply_schema_v1(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        if 2 not in applied_versions:
            _apply_schema_v2(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
        if 3 not in applied_versions:
            _apply_schema_v3(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (3)")
        if 4 not in applied_versions:
            _apply_schema_v4(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (4)")
        connection.commit()

    try:
        database.chmod(0o600)
    except OSError:
        pass

    return StoragePaths(
        root=root,
        database=database,
        pending=pending,
        working=working,
        previews=previews,
    )


def _connect(paths: StoragePaths | None = None) -> tuple[sqlite3.Connection, StoragePaths]:
    resolved_paths = paths or initialize_storage()
    connection = sqlite3.connect(resolved_paths.database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection, resolved_paths


def _recent_upload_defaults(task_id: str) -> dict:
    now = int(time.time() * 1000)
    return {
        "task_id": task_id,
        "submitted_at": now,
        "device_name": None,
        "file_name": None,
        "task_status": "STARTED",
        "result_text": None,
        "related_document": None,
        "is_polling": True,
        "last_error": None,
        "last_updated_at": now,
        "poll_failure_count": 0,
    }


def _pending_recent_upload(row: sqlite3.Row, history: dict | None = None) -> dict:
    task_id = row["paperless_task_id"] or f"pending-{row['id']}"
    entry = {**_recent_upload_defaults(task_id), **(history or {})}
    updated_at = datetime.fromisoformat(row["updated_at"]).replace(tzinfo=timezone.utc)
    timestamp = int(updated_at.timestamp() * 1000)
    if history is None:
        entry["submitted_at"] = timestamp
        entry["last_updated_at"] = timestamp
    else:
        entry["last_updated_at"] = max(timestamp, entry["last_updated_at"])
    entry["device_name"] = row["device_name"] or entry["device_name"]
    entry["file_name"] = row["original_filename"] or entry["file_name"]
    entry["related_document"] = row["paperless_document_id"] or entry["related_document"]
    if row["state"] == "submitted":
        entry["task_status"] = "SUCCESS"
    elif row["state"] == "failed":
        entry["task_status"] = "FAILURE"
        entry["result_text"] = row["last_error"]
    entry["is_polling"] = bool(row["paperless_task_id"]) and entry["task_status"] not in {
        "SUCCESS", "FAILURE",
    }
    return entry


def upsert_recent_upload(
    username: str,
    task_id: str,
    updates: dict | None = None,
    *,
    paths: StoragePaths | None = None,
) -> dict:
    task_id = task_id.strip()
    if not username.strip() or not task_id:
        raise ValueError("username and task_id are required for recent upload history.")
    connection, _ = _connect(paths)
    try:
        # Serialize read/merge/write across workers, not just threads in one process.
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT payload_json FROM recent_uploads WHERE username = ? AND task_id = ?",
            (username, task_id),
        ).fetchone()
        existing_submission = row is not None
        if row is not None:
            entry = json.loads(row["payload_json"])
        else:
            pending = connection.execute(
                """SELECT * FROM pending_documents
                   WHERE username = ? AND paperless_task_id = ?
                   ORDER BY updated_at DESC, id DESC LIMIT 1""",
                (username, task_id),
            ).fetchone()
            existing_submission = pending is not None
            entry = _pending_recent_upload(pending) if pending else _recent_upload_defaults(task_id)
        next_updates = dict(updates or {})
        if existing_submission:
            # Duplicate notifications and polls must not move an upload in history.
            next_updates.pop("submitted_at", None)
        # Poll responses often omit filenames; preserve the known filename/device.
        for field in ("file_name", "device_name", "related_document"):
            if next_updates.get(field) is None:
                next_updates.pop(field, None)
        next_updates.setdefault("last_updated_at", int(time.time() * 1000))
        entry.update(next_updates)
        entry["task_id"] = task_id
        connection.execute(
            """INSERT INTO recent_uploads(username, task_id, submitted_at, payload_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(username, task_id) DO UPDATE SET
                   submitted_at = excluded.submitted_at, payload_json = excluded.payload_json""",
            (username, task_id, entry["submitted_at"], json.dumps(entry)),
        )
        connection.commit()
        return entry
    finally:
        connection.close()


def list_recent_uploads(
    username: str, *, limit: int = 30, paths: StoragePaths | None = None,
) -> list[dict]:
    connection, _ = _connect(paths)
    try:
        # One snapshot keeps the persistent pending state and legacy history in sync.
        connection.execute("BEGIN")
        entries = {
            row["task_id"]: json.loads(row["payload_json"])
            for row in connection.execute(
                "SELECT task_id, payload_json FROM recent_uploads WHERE username = ?",
                (username,),
            )
        }
        pending_rows = connection.execute(
            """SELECT * FROM pending_documents
               WHERE username = ? AND state IN ('submitting', 'submitted', 'failed')
               ORDER BY updated_at DESC, id DESC""",
            (username,),
        ).fetchall()
        seen = set()
        for row in pending_rows:
            task_id = row["paperless_task_id"] or f"pending-{row['id']}"
            if task_id in seen:
                continue
            seen.add(task_id)
            entries[task_id] = _pending_recent_upload(row, entries.get(task_id))
        return sorted(
            entries.values(), key=lambda entry: (entry["submitted_at"], entry["task_id"]),
            reverse=True,
        )[:limit]
    finally:
        connection.close()


def _normalize_tag_ids(tag_ids) -> list[int]:
    if tag_ids is None:
        return []
    if not isinstance(tag_ids, list):
        raise ValueError("tag_ids must be a list.")
    normalized: list[int] = []
    for value in tag_ids:
        if isinstance(value, bool):
            raise ValueError("tag_ids must contain integer IDs.")
        try:
            tag_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("tag_ids must contain integer IDs.") from exc
        if tag_id <= 0:
            raise ValueError("tag_ids must contain positive integer IDs.")
        if tag_id not in normalized:
            normalized.append(tag_id)
    return normalized


def _row_to_pending(row: sqlite3.Row, metadata: sqlite3.Row | None = None) -> dict:
    payload = dict(row)
    payload.pop("pdf_path", None)
    if metadata is None:
        payload["metadata"] = {
            "title": None,
            "document_date": None,
            "correspondent_id": None,
            "document_type_id": None,
            "storage_path_id": None,
            "tag_ids": [],
        }
    else:
        payload["metadata"] = {
            "title": metadata["title"],
            "document_date": metadata["document_date"],
            "correspondent_id": metadata["correspondent_id"],
            "document_type_id": metadata["document_type_id"],
            "storage_path_id": metadata["storage_path_id"],
            "tag_ids": json.loads(metadata["tag_ids_json"] or "[]"),
        }
    payload["file_url"] = f"/api/pending/{payload['id']}/file"
    payload["download_url"] = f"/api/pending/{payload['id']}/file?download=1"
    return payload


def create_pending_document(
    source_pdf_path: Path,
    *,
    username: str,
    original_filename: str,
    page_count: int,
    device_name: str | None = None,
    title: str | None = None,
    session_id: str | None = None,
    sequence: int = 1,
    paths: StoragePaths | None = None,
) -> dict:
    if not username.strip():
        raise ValueError("username is required.")
    if page_count <= 0:
        raise ValueError("page_count must be greater than zero.")
    if not source_pdf_path.is_file() or source_pdf_path.stat().st_size == 0:
        raise ValueError("source PDF is missing or empty.")

    connection, resolved_paths = _connect(paths)
    pending_id = uuid.uuid4().hex
    safe_filename = original_filename.strip() or f"scan-{pending_id}.pdf"
    if not safe_filename.lower().endswith(".pdf"):
        safe_filename = f"{safe_filename}.pdf"
    target_path = resolved_paths.pending / f"{pending_id}.pdf"

    try:
        shutil.move(str(source_pdf_path), str(target_path))
        try:
            target_path.chmod(0o600)
        except OSError:
            pass

        connection.execute(
            """
            INSERT INTO pending_documents(
                id, session_id, sequence, state, pdf_path, page_count, username,
                original_filename, device_name
            ) VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?)
            """,
            (
                pending_id,
                session_id,
                sequence,
                str(target_path),
                page_count,
                username,
                safe_filename,
                device_name,
            ),
        )
        connection.execute(
            """
            INSERT INTO pending_metadata(
                pending_document_id, title, tag_ids_json
            ) VALUES (?, ?, '[]')
            """,
            (pending_id, title),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        try:
            target_path.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        connection.close()

    return get_pending_document(pending_id, username, paths=resolved_paths)


def discard_pending_document(
    pending_id: str,
    username: str,
    *,
    paths: StoragePaths | None = None,
) -> dict:
    connection, resolved_paths = _connect(paths)
    try:
        row = connection.execute(
            """
            SELECT id, session_id, sequence, state, pdf_path, preview_path
            FROM pending_documents
            WHERE id = ? AND username = ?
            """,
            (pending_id, username),
        ).fetchone()
        if row is None:
            raise LookupError("Pending document not found.")

        if row["state"] not in {"ready", "failed"}:
            raise RuntimeError(
                "Only ready or failed Pending scans can be discarded. "
                "Submitted or in-progress Paperless documents must be managed in Paperless."
            )

        file_paths = []
        for column_name in ("preview_path", "pdf_path"):
            raw_path = row[column_name]
            if raw_path:
                file_paths.append(Path(raw_path))

        for file_path in file_paths:
            try:
                file_path.unlink()
            except FileNotFoundError:
                pass

        cursor = connection.execute(
            "DELETE FROM pending_documents WHERE id = ? AND username = ?",
            (pending_id, username),
        )
        if cursor.rowcount == 0:
            raise LookupError("Pending document not found.")
        connection.commit()

        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "sequence": row["sequence"],
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_pending_documents(
    username: str,
    *,
    include_submitted: bool = False,
    paths: StoragePaths | None = None,
) -> list[dict]:
    connection, resolved_paths = _connect(paths)
    try:
        query = (
            "SELECT * FROM pending_documents WHERE username = ? "
            + ("" if include_submitted else "AND state != 'submitted' ")
            + "ORDER BY created_at DESC, id DESC"
        )
        rows = connection.execute(query, (username,)).fetchall()
        result = []
        for row in rows:
            metadata = connection.execute(
                "SELECT * FROM pending_metadata WHERE pending_document_id = ?",
                (row["id"],),
            ).fetchone()
            result.append(_row_to_pending(row, metadata))
        return result
    finally:
        connection.close()


def get_pending_document(
    pending_id: str,
    username: str,
    *,
    paths: StoragePaths | None = None,
    include_pdf_path: bool = False,
) -> dict:
    connection, _ = _connect(paths)
    try:
        row = connection.execute(
            "SELECT * FROM pending_documents WHERE id = ? AND username = ?",
            (pending_id, username),
        ).fetchone()
        if row is None:
            raise LookupError("Pending document not found.")
        metadata = connection.execute(
            "SELECT * FROM pending_metadata WHERE pending_document_id = ?",
            (pending_id,),
        ).fetchone()
        payload = _row_to_pending(row, metadata)
        if include_pdf_path:
            payload["pdf_path"] = row["pdf_path"]
        return payload
    finally:
        connection.close()


def update_pending_metadata(
    pending_id: str,
    username: str,
    metadata: dict,
    *,
    paths: StoragePaths | None = None,
) -> dict:
    tag_ids = _normalize_tag_ids(metadata.get("tag_ids"))
    allowed_scalar_fields = {
        "title": metadata.get("title"),
        "document_date": metadata.get("document_date"),
        "correspondent_id": metadata.get("correspondent_id"),
        "document_type_id": metadata.get("document_type_id"),
        "storage_path_id": metadata.get("storage_path_id"),
    }

    for key in ("correspondent_id", "document_type_id", "storage_path_id"):
        value = allowed_scalar_fields[key]
        if value in (None, ""):
            allowed_scalar_fields[key] = None
            continue
        if isinstance(value, bool):
            raise ValueError(f"{key} must be an integer ID.")
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer ID.") from exc
        if normalized <= 0:
            raise ValueError(f"{key} must be a positive integer ID.")
        allowed_scalar_fields[key] = normalized

    for key in ("title", "document_date"):
        value = allowed_scalar_fields[key]
        if value is not None:
            value = str(value).strip()
            allowed_scalar_fields[key] = value or None

    connection, resolved_paths = _connect(paths)
    try:
        state_row = connection.execute(
            "SELECT state FROM pending_documents WHERE id = ? AND username = ?",
            (pending_id, username),
        ).fetchone()
        if state_row is None:
            raise LookupError("Pending document not found.")
        if state_row["state"] == "submitted":
            raise RuntimeError("Submitted documents are read-only.")

        connection.execute(
            """
            UPDATE pending_metadata
            SET title = ?, document_date = ?, correspondent_id = ?,
                document_type_id = ?, storage_path_id = ?, tag_ids_json = ?
            WHERE pending_document_id = ?
            """,
            (
                allowed_scalar_fields["title"],
                allowed_scalar_fields["document_date"],
                allowed_scalar_fields["correspondent_id"],
                allowed_scalar_fields["document_type_id"],
                allowed_scalar_fields["storage_path_id"],
                json.dumps(tag_ids),
                pending_id,
            ),
        )
        connection.execute(
            "UPDATE pending_documents SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (pending_id,),
        )
        connection.commit()
    finally:
        connection.close()

    return get_pending_document(pending_id, username, paths=resolved_paths)


def set_pending_submission_state(
    pending_id: str,
    username: str,
    *,
    state: str,
    task_id: str | None = None,
    document_id: str | None = None,
    error: str | None = None,
    paths: StoragePaths | None = None,
) -> dict:
    if state not in {"ready", "submitting", "submitted", "failed"}:
        raise ValueError("Invalid pending state.")

    connection, resolved_paths = _connect(paths)
    try:
        cursor = connection.execute(
            """
            UPDATE pending_documents
            SET state = ?, paperless_task_id = ?, paperless_document_id = ?,
                last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND username = ?
            """,
            (state, task_id, document_id, error, pending_id, username),
        )
        if cursor.rowcount == 0:
            raise LookupError("Pending document not found.")
        connection.commit()
    finally:
        connection.close()
    return get_pending_document(pending_id, username, paths=resolved_paths)


def apply_paperless_task_status(
    username: str,
    task_id: str,
    *,
    task_status: str | None,
    document_id: str | None,
    result_text: str | None,
    paths: StoragePaths | None = None,
) -> dict | None:
    connection, resolved_paths = _connect(paths)
    try:
        row = connection.execute(
            """
            SELECT id FROM pending_documents
            WHERE username = ? AND paperless_task_id = ?
            """,
            (username, task_id),
        ).fetchone()
        if row is None:
            return None

        normalized_status = (task_status or "").upper()
        if normalized_status == "SUCCESS":
            state = "submitted"
            error = None
        elif normalized_status == "FAILURE":
            state = "failed"
            error = result_text or "Paperless ingestion failed."
        else:
            state = "submitting"
            error = None

        connection.execute(
            """
            UPDATE pending_documents
            SET state = ?, paperless_document_id = COALESCE(?, paperless_document_id),
                last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (state, document_id, error, row["id"]),
        )
        connection.commit()
        pending_id = row["id"]
    finally:
        connection.close()

    return get_pending_document(pending_id, username, paths=resolved_paths)


def resolve_pending_pdf_path(
    pending_id: str,
    username: str,
    *,
    paths: StoragePaths | None = None,
) -> tuple[Path, str]:
    payload = get_pending_document(
        pending_id,
        username,
        paths=paths,
        include_pdf_path=True,
    )
    pdf_path = Path(payload["pdf_path"])
    if not pdf_path.is_file():
        raise FileNotFoundError("Pending PDF is missing.")
    return pdf_path, payload.get("original_filename") or f"{pending_id}.pdf"


def _row_to_session(row: sqlite3.Row) -> dict:
    payload = dict(row)
    mode = payload.get("mode")
    state = payload.get("state")
    if state == "open" and mode == "document":
        payload["logical_state"] = "DOCUMENT_OPEN"
    elif state == "open" and mode == "batch":
        payload["logical_state"] = "BATCH_OPEN"
    elif state == "completed":
        payload["logical_state"] = "IDLE"
    else:
        payload["logical_state"] = str(state).upper()
    return payload


def get_active_scan_session(
    username: str,
    *,
    paths: StoragePaths | None = None,
) -> dict | None:
    connection, _ = _connect(paths)
    try:
        row = connection.execute(
            """
            SELECT * FROM scan_sessions
            WHERE username = ? AND state = 'open'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (username,),
        ).fetchone()
        return _row_to_session(row) if row is not None else None
    finally:
        connection.close()


def get_scan_session(
    session_id: str,
    username: str,
    *,
    paths: StoragePaths | None = None,
) -> dict:
    connection, _ = _connect(paths)
    try:
        row = connection.execute(
            "SELECT * FROM scan_sessions WHERE id = ? AND username = ?",
            (session_id, username),
        ).fetchone()
        if row is None:
            raise LookupError("Scan session not found.")
        return _row_to_session(row)
    finally:
        connection.close()


def ensure_scan_session(
    username: str,
    mode: str,
    *,
    device_name: str | None = None,
    paths: StoragePaths | None = None,
) -> dict:
    if mode not in {"document", "batch"}:
        raise ValueError("mode must be 'document' or 'batch'.")
    if not username.strip():
        raise ValueError("username is required.")

    connection, resolved_paths = _connect(paths)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM scan_sessions WHERE username = ? AND state = 'open' LIMIT 1",
            (username,),
        ).fetchone()
        if existing is not None:
            if existing["mode"] != mode:
                raise RuntimeError(
                    f"{existing['mode'].capitalize()} session is already open; finish it first."
                )
            if (
                device_name
                and existing["device_name"]
                and existing["device_name"] != device_name
            ):
                raise RuntimeError(
                    "The open scan session belongs to a different scanner device."
                )
            connection.commit()
            return _row_to_session(existing)

        session_id = uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO scan_sessions(
                id, mode, state, username, device_name, page_count, item_count
            ) VALUES (?, ?, 'open', ?, ?, 0, 0)
            """,
            (session_id, mode, username, device_name),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    _ensure_private_directory(resolved_paths.working / session_id)
    return get_scan_session(session_id, username, paths=resolved_paths)


def append_document_page(
    session_id: str,
    username: str,
    source_page_path: Path,
    *,
    paths: StoragePaths | None = None,
) -> dict:
    if not source_page_path.is_file() or source_page_path.stat().st_size == 0:
        raise ValueError("Captured page is missing or empty.")

    connection, resolved_paths = _connect(paths)
    target_path: Path | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT * FROM scan_sessions
            WHERE id = ? AND username = ? AND state = 'open'
            """,
            (session_id, username),
        ).fetchone()
        if row is None:
            raise LookupError("Open scan session not found.")
        if row["mode"] != "document":
            raise RuntimeError("Pages can only be appended to a document session.")

        sequence = int(row["page_count"]) + 1
        session_dir = resolved_paths.working / session_id
        _ensure_private_directory(session_dir)
        suffix = source_page_path.suffix.lower() or ".tiff"
        target_path = session_dir / f"page-{sequence:04d}{suffix}"
        shutil.move(str(source_page_path), str(target_path))
        try:
            target_path.chmod(0o600)
        except OSError:
            pass

        connection.execute(
            """
            INSERT INTO scan_session_pages(session_id, sequence, file_path)
            VALUES (?, ?, ?)
            """,
            (session_id, sequence, str(target_path)),
        )
        connection.execute(
            """
            UPDATE scan_sessions
            SET page_count = ?, last_error = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (sequence, session_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        if target_path is not None:
            try:
                target_path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        connection.close()

    return get_scan_session(session_id, username, paths=resolved_paths)


def list_document_page_paths(
    session_id: str,
    username: str,
    *,
    paths: StoragePaths | None = None,
) -> list[Path]:
    connection, _ = _connect(paths)
    try:
        session_row = connection.execute(
            """
            SELECT mode FROM scan_sessions
            WHERE id = ? AND username = ? AND state = 'open'
            """,
            (session_id, username),
        ).fetchone()
        if session_row is None:
            raise LookupError("Open scan session not found.")
        if session_row["mode"] != "document":
            raise RuntimeError("Session is not a document session.")
        rows = connection.execute(
            """
            SELECT file_path FROM scan_session_pages
            WHERE session_id = ?
            ORDER BY sequence
            """,
            (session_id,),
        ).fetchall()
        return [Path(row["file_path"]) for row in rows]
    finally:
        connection.close()


def attach_pending_to_batch_session(
    pending_id: str,
    session_id: str,
    username: str,
    *,
    paths: StoragePaths | None = None,
) -> dict:
    connection, resolved_paths = _connect(paths)
    try:
        connection.execute("BEGIN IMMEDIATE")
        session_row = connection.execute(
            """
            SELECT * FROM scan_sessions
            WHERE id = ? AND username = ? AND state = 'open'
            """,
            (session_id, username),
        ).fetchone()
        if session_row is None:
            raise LookupError("Open scan session not found.")
        if session_row["mode"] != "batch":
            raise RuntimeError("Pending items can only be attached to a batch session.")

        pending_row = connection.execute(
            """
            SELECT id FROM pending_documents
            WHERE id = ? AND username = ? AND session_id IS NULL
            """,
            (pending_id, username),
        ).fetchone()
        if pending_row is None:
            raise LookupError("Unattached Pending document not found.")

        sequence = int(session_row["item_count"]) + 1
        connection.execute(
            """
            UPDATE pending_documents
            SET session_id = ?, sequence = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (session_id, sequence, pending_id),
        )
        connection.execute(
            """
            UPDATE scan_sessions
            SET item_count = ?, last_error = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (sequence, session_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return get_pending_document(pending_id, username, paths=resolved_paths)


def complete_scan_session(
    session_id: str,
    username: str,
    *,
    paths: StoragePaths | None = None,
) -> dict:
    connection, resolved_paths = _connect(paths)
    try:
        cursor = connection.execute(
            """
            UPDATE scan_sessions
            SET state = 'completed', last_error = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND username = ? AND state = 'open'
            """,
            (session_id, username),
        )
        if cursor.rowcount == 0:
            raise LookupError("Open scan session not found.")
        connection.commit()
    finally:
        connection.close()
    return get_scan_session(session_id, username, paths=resolved_paths)


def record_scan_session_error(
    session_id: str,
    username: str,
    error: str,
    *,
    paths: StoragePaths | None = None,
) -> dict:
    connection, resolved_paths = _connect(paths)
    try:
        cursor = connection.execute(
            """
            UPDATE scan_sessions
            SET last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND username = ? AND state = 'open'
            """,
            (error, session_id, username),
        )
        if cursor.rowcount == 0:
            raise LookupError("Open scan session not found.")
        connection.commit()
    finally:
        connection.close()
    return get_scan_session(session_id, username, paths=resolved_paths)


def cleanup_document_session_pages(
    session_id: str,
    username: str,
    *,
    paths: StoragePaths | None = None,
) -> None:
    connection, resolved_paths = _connect(paths)
    try:
        session_row = connection.execute(
            "SELECT id FROM scan_sessions WHERE id = ? AND username = ?",
            (session_id, username),
        ).fetchone()
        if session_row is None:
            raise LookupError("Scan session not found.")
        rows = connection.execute(
            "SELECT file_path FROM scan_session_pages WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        connection.execute(
            "DELETE FROM scan_session_pages WHERE session_id = ?",
            (session_id,),
        )
        connection.commit()
    finally:
        connection.close()

    for row in rows:
        try:
            Path(row["file_path"]).unlink()
        except FileNotFoundError:
            pass
    session_dir = resolved_paths.working / session_id
    try:
        session_dir.rmdir()
    except OSError:
        pass


def apply_pending_metadata_scope(
    pending_id: str,
    username: str,
    *,
    scope: str,
    paths: StoragePaths | None = None,
) -> int:
    if scope not in {"remaining", "all"}:
        raise ValueError("scope must be 'remaining' or 'all'.")

    connection, _ = _connect(paths)
    try:
        source = connection.execute(
            """
            SELECT p.session_id, p.sequence, m.document_date, m.correspondent_id,
                   m.document_type_id, m.storage_path_id, m.tag_ids_json
            FROM pending_documents p
            JOIN pending_metadata m ON m.pending_document_id = p.id
            WHERE p.id = ? AND p.username = ?
            """,
            (pending_id, username),
        ).fetchone()
        if source is None:
            raise LookupError("Pending document not found.")
        if source["session_id"] is None:
            raise RuntimeError("Apply-to-batch requires a Batch session item.")

        if scope == "remaining":
            target_rows = connection.execute(
                """
                SELECT id FROM pending_documents
                WHERE username = ? AND session_id = ? AND sequence > ?
                  AND state IN ('ready', 'failed')
                ORDER BY sequence
                """,
                (username, source["session_id"], source["sequence"]),
            ).fetchall()
        else:
            target_rows = connection.execute(
                """
                SELECT id FROM pending_documents
                WHERE username = ? AND session_id = ?
                  AND state IN ('ready', 'failed')
                ORDER BY sequence
                """,
                (username, source["session_id"]),
            ).fetchall()

        target_ids = [row["id"] for row in target_rows]
        for target_id in target_ids:
            connection.execute(
                """
                UPDATE pending_metadata
                SET document_date = ?, correspondent_id = ?, document_type_id = ?,
                    storage_path_id = ?, tag_ids_json = ?
                WHERE pending_document_id = ?
                """,
                (
                    source["document_date"],
                    source["correspondent_id"],
                    source["document_type_id"],
                    source["storage_path_id"],
                    source["tag_ids_json"],
                    target_id,
                ),
            )
            connection.execute(
                "UPDATE pending_documents SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (target_id,),
            )
        connection.commit()
        return len(target_ids)
    finally:
        connection.close()


def preview_submitted_retention(
    days: int = 30,
    *,
    now: datetime | None = None,
    data_dir: Path | None = None,
) -> dict:
    """Read-only preview. Never remove PDFs or modify the ScanExpress database.

    Only confirmed submitted items with a Paperless document ID and an old
    enough last-state-change timestamp are considered. This is a conservative
    starting point, not an executable deletion policy.
    """
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("Retention days must be a positive integer.")
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("The current timestamp must include a timezone.")
    cutoff = (current.astimezone(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    root = (data_dir or resolve_data_dir()).expanduser().resolve()
    database = root / "scanexpress.sqlite3"
    pending_root = root / "pending"
    # Explicit read-only URI: no bootstrap, migrations, chmod, or writes.
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT id, pdf_path, updated_at
            FROM pending_documents
            WHERE state = 'submitted'
              AND paperless_document_id IS NOT NULL
              AND paperless_document_id != ''
              AND pdf_path IS NOT NULL
              AND datetime(updated_at) <= datetime(?)
            ORDER BY updated_at, id
            """,
            (cutoff,),
        ).fetchall()
    finally:
        connection.close()

    candidates = []
    skipped = 0
    for row in rows:
        pdf = Path(row["pdf_path"])
        # Fail closed on missing PDFs, path escapes and symlinks.
        if (
            pdf.is_symlink()
            or not pdf.is_file()
            or not pdf.resolve().is_relative_to(pending_root)
        ):
            skipped += 1
            continue
        candidates.append({
            "id": row["id"],
            "submitted_timestamp_utc": row["updated_at"],
            "local_pdf_bytes": pdf.stat().st_size,
        })

    return {
        "mode": "preview-only",
        "retention_days": days,
        "cutoff_utc": cutoff,
        "eligible_count": len(candidates),
        "eligible_pdf_bytes": sum(item["local_pdf_bytes"] for item in candidates),
        "skipped_unsafe_or_missing": skipped,
        "items": candidates,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="ScanExpress storage maintenance.")
    parser.add_argument(
        "command", nargs="?", default="init", choices=["init", "retention-preview"]
    )
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    if args.command == "init":
        paths = initialize_storage()
        print(f"Initialized ScanExpress storage at {paths.root}")
        return 0
    if args.command == "retention-preview":
        if args.days < 1:
            parser.error("--days must be at least 1")
        print(json.dumps(preview_submitted_retention(days=args.days), indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_main())

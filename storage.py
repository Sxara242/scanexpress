import argparse
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path


_SCHEMA_VERSION = 2
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
                id, state, pdf_path, page_count, username,
                original_filename, device_name
            ) VALUES (?, 'ready', ?, ?, ?, ?, ?)
            """,
            (
                pending_id,
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


def _main() -> int:
    parser = argparse.ArgumentParser(description="Initialize ScanExpress persistent storage.")
    parser.add_argument("command", nargs="?", default="init", choices=["init"])
    args = parser.parse_args()

    if args.command == "init":
        paths = initialize_storage()
        print(f"Initialized ScanExpress storage at {paths.root}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_main())

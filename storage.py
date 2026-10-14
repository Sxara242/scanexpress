import argparse
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


_SCHEMA_VERSION = 1
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
        # Best effort for platforms/filesystems without POSIX permissions.
        pass


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
            connection.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (_SCHEMA_VERSION,),
            )
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

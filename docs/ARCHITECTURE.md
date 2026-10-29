# ScanExpress Architecture (Draft)

## Intended Flow

1. User opens the Flask-served web page and clicks the scan button.
2. Frontend JavaScript sends a request to the Flask progress-stream endpoint.
3. Flask backend resolves current user and active device via `ConfigManager` (`config.ini` first, env fallback), supporting both user-specific device templates and shared global templates.
4. Flask backend executes configured scanner command/wrapper (`scanimage` compatible) with batch output (`--batch=.../scan_output%d.tiff`).
5. Backend converts all generated TIFF output files/pages to a PDF.
6. Backend uploads PDF to Paperless-ngx (`/api/documents/post_document/`).
7. Flask API returns success/failure status for the UI.

### API Notes

- `GET /`: renders the main UI only when `global.default_user` is configured (or an authenticated session exists); otherwise renders a configuration error page (no browser auth prompt).
- `POST /api/scan`: synchronous JSON response (compatible with scripts and curl).
- `POST /api/scan/stream`: streaming NDJSON progress updates for live UI status during scanning.
- Successful responses include `username`, selected template `device_name`, configured `device_id`, and runtime `scanimage_device_name` (the value passed to scanner `-d`) for traceability.
- Successful completion payloads include `timing_metrics` with `total_seconds`, `scan_seconds`, `paperless_seconds`, and scan/upload per-page timing values.

## Notes

- Scanner model should be configurable (not hardcoded).
- Credentials and scanner settings should come from `config.ini` (`ConfigManager`), with environment variable fallback for backward compatibility.
- API and frontend are kept in one project for simplicity.
- Playwright smoke tests (`tests/e2e/smoke.spec.js`) validate the UI/API contract against the local Flask server.
- E2E avoids real hardware by using a fake scan wrapper command and a local fake Paperless endpoint.

## Persistent recent uploads

`GET /api/recent-uploads` reads the existing `pending_documents` rows for the active user in submitting, submitted, or failed state. Ready documents stay in Pending. The saved Paperless Task ID and Document ID, original filename, device, and state restore history after process restarts without contacting Paperless. Submitted maps to SUCCESS, failed maps to FAILURE, and in-flight tasks resume polling. A direct Document ID response uses a stable `pending-<pending_id>` history key and never polls.

SQLite schema version 4 adds `recent_uploads` for legacy upload/failure records and task-poll details previously held in memory. Upserts use a `(username, task_id)` primary key and a database write transaction, so workers share history without duplicate entries or lost field updates. Reads merge these details with authoritative Pending records in a single snapshot and return the newest 30 unique entries using the existing JSON fields. Missing poll filenames do not erase saved names. Existing Pending rows use `updated_at` as a submission-time fallback; the first history write preserves that time. Later updates and duplicate upload notifications preserve the original `submitted_at` and historical ordering. No existing document rows or retention policies are changed by history reads or the additive migration.

The Paperless task normalizer accepts positive integer Document IDs from `related_document`, `result_data.document_id`, or the first valid `related_document_ids` entry, in that order. It normalizes task status to uppercase before history and Pending-state updates, preserving the existing frontend contract.

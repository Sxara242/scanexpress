# AGENTS.md

Guidance for coding agents working in ScanExpress.

## Safety and workflow

- Preserve user data and existing functionality. Never discard existing changes.
- Never modify the production Dell, its SQLite database, or its services without approval.
- Use a dedicated fix branch for each bug fix.
- Never commit, push, merge, or deploy without explicit approval.
- Never store credentials or sensitive document data in Git. Keep databases and scan files outside the working tree.
- Finish each task with a short summary of changes, tests, and outstanding issues.

## Implementation

- Read existing code and relevant documentation before editing; read `CODEX_HANDOFF.md` and `docs/SCANNER_MANAGER_MASTER_SPEC.md` before architectural changes.
- Keep the Flask service, server-rendered templates, and vanilla JavaScript simple. Prefer incremental changes and preserve routes and frontend/backend contracts unless asked.
- Follow existing Python style; keep functions small and explicit. Use `config.ini` for endpoints, credentials, device paths, and scanner settings. Use `SCANEXPRESS_CONFIG_FILE` only to override its path.
- Isolate scanner invocation for testing, set timeouts, validate outputs before upload, and return structured JSON errors with actionable messages.
- Handle frontend network and parse errors gracefully; keep controls accessible and status text concise.
- Never log sensitive headers or tokens. Do not run destructive scanner commands unless requested.

## Testing

- Add regression tests for bug fixes. Write or update relevant tests first when possible, run them to confirm failure, then implement the change.
- Activate the repository virtual environment before every project test command:

    source .venv/bin/activate

- Use temporary databases and synthetic documents for tests; never use production data.
- Run focused tests and appropriate existing checks. Verify app startup with `python app.py`, page loading, scan-button status updates, and the expected `/api/scan` response using fake hardware/services.
- For UI or `/api/scan` contract changes, also run `npm run test:e2e`. Keep tests lightweight.

## Documentation

- Update `README.md` for setup or behavior changes and `docs/ARCHITECTURE.md` for flow or component changes.
- Use four-space-indented Markdown command examples, never fenced code blocks.
- Leave one blank line around headings and lists; avoid consecutive blank lines and trailing spaces.

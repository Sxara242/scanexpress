# User Login Specification

## Purpose

Define authentication and user identity behavior for ScanExpress so that:

- the UI requires `global.default_user` to be configured,
- a configured default user is used as the resolved user identity,
- each authenticated user uses their own configuration and recent upload history,
- scanner device locking remains shared across users for the same physical device.

This specification is authoritative for login behavior and intentionally introduces breaking configuration changes.

## Scope

In scope:

- Flask-Login based authentication and session handling.
- HTTP Basic authentication for credential submission.
- User identity source for all backend operations.
- Config file schema updates for login credentials.
- User-scoped recent upload history.
- Cross-user shared scan lock behavior.

Out of scope:

- External identity providers (OIDC, LDAP, SSO).
- Role-based access control.
- Password reset UI.

## Library and Auth Mechanism

Required libraries:

- `flask-login` for session management and `login_required` protection.
- Werkzeug password hash utilities (`generate_password_hash`, `check_password_hash`) for password verification.

Authentication mechanism:

- Credentials are provided through HTTP Basic Authentication (`Authorization: Basic ...`).
- Passwords are never stored in plaintext.
- Stored credential value is a password hash string compatible with Werkzeug `check_password_hash`.

Notes:

- Apache `htpasswd` style files are not required.
- This implementation uses hash verification provided by Flask/Werkzeug tooling.

## Breaking Configuration Changes (No Backward Compatibility)

The previous single-user key `global.current_user` is removed.

New global keys:

- `default_user` (required for UI): username to use for UI requests.
  - When set, UI requests resolve to this user.
  - When absent or empty, `GET /` must render a configuration error page.
- existing global keys such as `paperless_base_url`, `scan_timeout_seconds`, and `paperless_timeout_seconds` remain.

User section changes (`[user:<username>]`):

- `password_hash` (required unless `default_user` is used as the only enabled account): Werkzeug-compatible hash.
- `paperless_api_token` (required, unchanged).
- `default_device` (required when user has devices, unchanged).
- `default_scanimage_params_device` (optional, unchanged).

Example configuration:

    [global]
    paperless_base_url = https://paperless.example.com
    scan_timeout_seconds = 30
    paperless_timeout_seconds = 5
    default_user = alice

    [user:alice]
    password_hash = scrypt:32768:8:1$example$example
    paperless_api_token = replace-with-alice-token
    default_device = brother-color

    [user:bob]
    password_hash = pbkdf2:sha256:600000$example$example
    paperless_api_token = replace-with-bob-token
    default_device = canon-default

Password hash generation guidance:

    python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('change-me'))"

## Identity Resolution Rules

Request user resolution order:

1. If `global.default_user` is configured: use that user for all requests.
2. Otherwise require an authenticated Flask-Login session and use `current_user.id` for protected API routes.
3. For `GET /`, when neither condition is met: return configuration error page.
4. For protected API routes, when neither condition is met: return unauthorized response.

All current calls that resolve user via configuration must be updated to use the resolved request user identity instead of a global active user.

## Route Protection and Auth API

Protected routes:

- `GET /`
- `POST /api/scan`
- `POST /api/scan/stream`
- `GET /api/device-configurations`
- `GET /api/scan/status`
- `GET /api/recent-uploads`
- `GET /api/paperless/tasks/<task_id>`

Authentication routes:

- `POST /auth/login`
  - Accepts HTTP Basic credentials.
  - On success, creates Flask-Login session.
  - Returns `200` JSON `{ "status": "ok", "username": "..." }`.
  - On failure, returns `401` with `WWW-Authenticate: Basic realm="ScanExpress"`.
- `POST /auth/logout`
  - Clears session.
  - Returns `200` JSON `{ "status": "ok" }`.

Unauthorized response behavior:

- `GET /` returns a configuration error page when `global.default_user` is missing.
- API endpoints return `401` JSON with `status=error` and a concise message.
- Include `WWW-Authenticate` header for Basic auth discovery.

## Authenticated Header UX

When a request is authenticated (session user or `global.default_user`), the main UI header must expose account controls.

Required behavior:

- Show the resolved username in the top-right area of the header.
- Provide a visible logout action next to the username.
- Logout action must call `POST /auth/logout` and clear the active session.
- With configured `global.default_user`, the UI always resolves to that user after reload.

## User-Scoped Configuration Behavior

After authentication, all user-dependent configuration must be read using the authenticated username:

- Paperless token via `get_user_token(authenticated_username)`.
- Device list/default device from that user section.
- Device scan params from that user section.
- Upload and task polling operations tied to that user.

No global mutable "current user" runtime state is allowed.

## Recent Upload History Requirements

Recent uploads are per authenticated user.

Required behavior:

- Keep separate history buckets by username.
- `GET /api/recent-uploads` returns only entries for the resolved request user.
- Upload/task polling updates write only to that user bucket.

Current readiness assessment:

- Already prepared: SQLite-backed recent-upload helpers and Pending queries isolate entries by username; the API returns the latest 30 unique uploads.
- Required adjustment: all call sites must pass the authenticated username rather than deriving user from global config.

## Device Locking Requirements

Device locks must remain shared across users for the same physical device.

Required behavior:

- Lock key should be canonical device identity (`scanimage_device_name` preferred, then configured `device_id`).
- If physical identity is unavailable, fallback lock key must still be cross-user for same logical device name, not user-scoped.

Current readiness assessment:

- Already prepared for configured device IDs: lock key currently resolves to `scanimage_device_name` or `device_id`, which is shared.
- Gap to fix: fallback currently includes username (`username:device_name`), which prevents cross-user lock sharing when no device ID is configured.
- Required change: replace username-based fallback with a cross-user key, for example `__device_name__:<device_name-or-default>`.

## Session and Security Requirements

- Set `app.secret_key` from configuration or environment appropriate for deployment.
- Session cookie settings:
  - `SESSION_COOKIE_HTTPONLY = True`
  - `SESSION_COOKIE_SAMESITE = Lax`
  - `SESSION_COOKIE_SECURE = True` when served over HTTPS
- Never log passwords or Authorization headers.
- Error payloads must not disclose whether username or password was wrong.


## Compatibility and Migration

This change is breaking by design.

Migration steps:

1. Remove `global.current_user` from config.
2. Add `password_hash` to each `[user:<username>]` section.
3. Optionally set `global.default_user` for single-user auto-login deployments.
4. Restart service.

Startup validation must fail fast when required auth keys are missing or invalid.

## Acceptance Criteria

- When `global.default_user` is empty, `GET /` returns a configuration error page.
- Protected API routes still require auth and return `401` otherwise.
- Valid Basic credentials can create session via `POST /auth/login`.
- After login, scan/upload/task operations use that user's Paperless token and device configuration.
- `GET /api/recent-uploads` only shows the logged-in user's entries.
- If user A starts a scan on device X, user B cannot start another scan on the same device X until completion.
- Config without `password_hash` for an active login-enabled user fails validation at startup.

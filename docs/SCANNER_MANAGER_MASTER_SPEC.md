# Paperless Scanner Manager — Master Specification

Status: initial fork design baseline
Upstream base: ScanExpress
License baseline: preserve upstream MIT license and copyright notices

## 1. Product goal

Evolve ScanExpress into a small, local-first scanner workflow companion for Paperless-ngx.

The product is not a replacement for Paperless-ngx. Paperless remains the system of record for stored documents, OCR, indexing, metadata, search, retention, permissions, and the final original/archive files.

This project owns the workflow between a physical scanner and Paperless:

1. physical-button capture,
2. safe temporary/pending storage,
3. preview and review,
4. metadata selection,
5. explicit submission to Paperless,
6. task-status confirmation.

The primary reference hardware is a Canon CanoScan LiDE 400 on Linux using SANE.

## 2. Upstream functionality to preserve

ScanExpress already provides useful foundations and these should be reused instead of rewritten without a reason:

- Flask backend and browser UI.
- Configurable SANE/scanimage command execution.
- Per-device scan settings.
- TIFF output handling.
- Multi-page TIFF-to-PDF conversion.
- Temporary-file cleanup.
- Device locking.
- Scan timeout/error handling.
- Paperless upload transport.
- Paperless task-ID parsing and task polling.
- Per-user recent-upload tracking.
- Python unit tests.
- Playwright end-to-end tests.
- systemd and Docker deployment examples.

Changes should be incremental and test-driven where practical.

## 3. Major behavior change

Current upstream flow is effectively:

    Scan -> build PDF -> upload immediately to Paperless

Target flow is:

    Capture -> persistent Pending item -> preview/review -> metadata -> submit -> Paperless confirmation

A successful scan must never disappear merely because the browser was closed, the app restarted, or Paperless was temporarily unavailable.

## 4. Physical-button reference mapping

The Canon LiDE 400 has been validated under SANE/scanbd with distinct target values.

Reference mapping:

- PDF Start -> SCANBD_TARGET=5
- PDF Finish -> SCANBD_TARGET=6
- Auto Scan -> SCANBD_TARGET=2
- Copy -> SCANBD_TARGET=1
- Send -> SCANBD_TARGET=3

Initial product mapping:

- target 5: Document mode / scan next page
- target 6: Finish the current Document or Batch session
- target 2: Batch mode / scan one independent item
- target 1: reserved
- target 3: reserved

Do not hardcode these values into generic business logic. They belong in a configurable hardware-event mapping with the LiDE 400 mapping supplied as a documented preset/example.

## 5. Duplicate-event safety

Real hardware testing showed that one physical press can result in more than one scanbd trigger for some buttons.

Therefore the application must guarantee:

    one intended physical press -> at most one scan operation

Required safeguards:

- atomic global/device scan lock,
- monotonic-time debounce for hardware events,
- state-aware event acceptance,
- idempotent Finish handling,
- never start a second scan while the first scan operation is active.

The exact debounce interval should be configurable; an initial default around 1000–1500 ms is reasonable. Correctness is more important than minimizing the interval.

## 6. Scanner state machine

### IDLE

Accepted events:

- Document Start:
  - create a new document session,
  - scan one page,
  - enter DOCUMENT_OPEN.

- Batch:
  - create a new batch session,
  - scan one independent item,
  - enter BATCH_OPEN.

- Finish:
  - no destructive action,
  - ignore safely and record a diagnostic event.

### DOCUMENT_OPEN

Accepted events:

- Document Start:
  - scan one additional page into the same logical document.

- Finish:
  - finalize all captured pages into one PDF,
  - create one persistent Pending document,
  - return to IDLE.

Rejected/ignored safely:

- Batch while a document session is open.

### BATCH_OPEN

Accepted events:

- Batch:
  - scan one independent item,
  - create/retain it as a separate Pending item in the current batch session.

- Finish:
  - close the batch session,
  - keep every item as a separate Pending document,
  - return to IDLE.

Rejected/ignored safely:

- Document Start while a batch session is open.

### Failure behavior

A scanner, conversion, or filesystem error must not silently discard previously completed pages/items.

The state machine must expose a recoverable error state and make it clear what was preserved.

## 7. Document mode

Use case: one logical multi-page document.

Example:

    PDF Start -> page 1
    PDF Start -> page 2
    PDF Start -> page 3
    PDF Finish

Result:

- one Pending item,
- one three-page PDF,
- one metadata form,
- one Paperless document after submission.

The daily workflow must not require opening a terminal or browser before pressing the scanner button.

## 8. Batch mode

Use case: many unrelated one-page items, for example 50 photographs or receipts.

Example:

    Auto Scan -> item 1
    Auto Scan -> item 2
    ...
    Auto Scan -> item 50
    PDF Finish

Result:

- 50 separate Pending items,
- not one 50-page PDF,
- one review queue in the UI.

Batch-review UI requirements:

- current position such as 1 / 50,
- large preview,
- previous/next controls,
- zoom,
- rotate,
- per-item title and metadata,
- Apply to remaining/all for shared metadata,
- independent title per item,
- clear submitted/pending/error state for each item.

## 9. Pending storage

Pending captures must be persistent across service/browser restarts.

Do not keep pending jobs only in Python process memory.

Recommended design:

- SQLite for job/session/state metadata.
- A private application data directory for captured images/PDFs and generated previews.
- Atomic file moves when finalizing a capture.
- Separate transient working area from persistent pending area.

Suggested logical model:

- scan_session
  - id
  - mode: document | batch
  - state
  - created_at
  - updated_at

- pending_document
  - id
  - session_id
  - sequence
  - state: captured | ready | submitting | submitted | failed
  - source_path / pdf_path
  - preview_path
  - page_count
  - created_at
  - paperless_task_id
  - paperless_document_id
  - last_error

- pending_metadata
  - title
  - document_date
  - correspondent_id
  - document_type_id
  - storage_path_id
  - tag_ids

Exact schema may evolve, but persistence and recoverability are required.

## 10. Preview

The current upstream UI has no document preview. Add one.

Minimum preview capabilities:

- PDF/image preview for the selected Pending item,
- previous/next item,
- zoom,
- rotate,
- page count,
- visible scan status.

Rotation should be non-destructive until submission or be represented explicitly in persistent state.

Do not reduce source scan quality merely to make previews faster. Generate separate lightweight preview assets where useful.

## 11. Paperless metadata integration

The app must fetch Paperless metadata dynamically through the backend. The browser must never receive the Paperless API token.

Fields:

- Title
- Document date
- Document Type
- Correspondent
- Storage Path
- Tags, multi-select

Required behavior:

- searchable selectors,
- display current Paperless values,
- refresh capability,
- support many tags,
- preserve selected IDs while editing.

Owner/admin-only create actions:

- Create new tag
- Create new document type
- Create new correspondent
- Create new storage path where supported and appropriate

Creation must happen via Paperless API and the newly created object should immediately become selectable.

Do not model Paperless storage paths as arbitrary host filesystem paths. Paperless owns its media storage; this UI selects Paperless Storage Path metadata.

## 12. Category suggestions

Optional category/navigation helpers such as:

    Personal -> Education

may suggest metadata such as tags, document type, or storage path.

Suggestions must never lock the user out of additional tags or other metadata.

The source of truth remains the explicit Paperless metadata selected for the Pending item.

## 13. Titles and filenames

Important documents should support a deliberate human-readable Paperless title, for example:

    High School Diploma - Person - 2002

The local temporary/original filename may remain generated and unique.

Do not conflate:

- local filesystem filename,
- Paperless document title.

Searchability should not depend exclusively on OCR.

## 14. Paperless submission

Extend the existing upload code rather than replacing it unnecessarily.

A submission should send, when selected:

- document
- title
- created/document date
- correspondent
- document_type
- storage_path
- tags

After upload:

- retain the returned task UUID,
- poll Paperless through the existing backend proxy approach,
- do not expose the token to the browser,
- only mark a Pending document successfully submitted when Paperless reports success,
- preserve failed submissions for retry,
- expose the Paperless document link when available.

A transient Paperless outage must not require rescanning.

## 15. Original download

The owner must be able to download the locally captured/finalized PDF while it is Pending.

After confirmed Paperless ingestion, the UI should link to the Paperless document. Local post-submit retention should be configurable rather than silently deleting immediately.

## 16. scanbd integration

scanbd is the physical-button event source.

Important: scanbd may hold the SANE device while polling. Scanner execution must therefore be integrated in a way that does not race scanbd for the device.

Do not assume that a scanbd action script can always invoke the existing direct scanimage command safely.

The deployment must validate the scanbd/scanbm/SANE-net coordination path before declaring physical-button scanning production-ready.

Recommended software boundary:

- scanbd adapter receives hardware events,
- adapter sends a normalized event to the application,
- application state machine decides whether the event is accepted,
- scanner executor performs the actual scan through the validated SANE transport.

The event transport should be local-only and authenticated or otherwise protected against untrusted local requests.

## 17. Internal hardware-event API

A possible implementation is a localhost-only endpoint used by the scanbd action adapter.

Example logical payload:

    {
      "source": "scanbd",
      "target": "5",
      "device": "canon-lide-400"
    }

Requirements:

- loopback-only by default,
- shared secret or equivalent local authentication,
- never expose this endpoint directly through the public/Tailscale-facing reverse proxy,
- fast response,
- enqueue/dispatch safely,
- duplicate protection,
- structured event logging without secrets.

A Unix-domain-socket adapter is also acceptable if it simplifies security.

## 18. Authentication and permissions

Do not weaken upstream authentication.

For the reference household deployment:

- owner account: full Scanner Manager rights,
- read-only/viewer accounts, if enabled later: view/download only,
- no upload, metadata edit, delete, or metadata-object creation for read-only users.

Paperless itself remains authoritative for final document permissions.

MVP may remain owner-only if that produces a safer and simpler first release.

## 19. Security requirements

Before production deployment:

- no Flask debug server,
- no debug=True,
- no binding the development server directly to 0.0.0.0 for the reference deployment,
- run behind a production WSGI server,
- bind application service to loopback by default,
- expose remotely through an authenticated/restricted reverse proxy such as Tailscale Serve,
- secure session cookies,
- no tokens or passwords in logs,
- no Paperless token in frontend JavaScript,
- config/secrets excluded from Git,
- .env/config examples contain placeholders only,
- uploaded/pending documents stored outside the source tree,
- pending document download endpoints require authorization,
- validate paths and IDs server-side.

## 20. Upstream security cleanup to perform

The fork should correct these upstream deployment concerns before production use:

- upstream app.py runs the Flask development server with debug=True when executed directly,
- production service examples should use a production WSGI server,
- explicit session-cookie hardening described in upstream docs should be implemented/verified in code,
- remove any development-secret fallback from production mode or fail closed when authentication is required.

## 21. Deployment target

Primary deployment target:

- Debian/Ubuntu Linux server,
- USB SANE scanner,
- systemd-managed services,
- web UI accessed from desktop/mobile over a trusted private network.

Docker support may remain, but direct host SANE/scanbd integration is the first-class reference path for the LiDE 400 deployment.

## 22. Testing requirements

Preserve the upstream test suite and add focused tests.

Required new unit/integration coverage:

- target -> logical action mapping,
- duplicate-event debounce,
- event during active scan,
- Finish idempotency,
- Document state transitions,
- Batch state transitions,
- persistent restart recovery,
- batch creates N separate Pending items,
- document mode creates one N-page Pending PDF,
- metadata list proxying,
- metadata creation,
- Paperless multipart metadata payload,
- Paperless failure/retry,
- authorization on pending downloads,
- no token exposure.

Required E2E coverage:

- browser review of one Pending document,
- batch navigation,
- Apply to remaining/all,
- metadata save,
- submit -> STARTED -> SUCCESS,
- submit -> FAILURE -> retry,
- browser refresh preserves queue.

Hardware acceptance tests remain separate because CI cannot emulate the real physical scanner buttons.

## 23. Reference hardware acceptance gates

For Canon LiDE 400 deployment, require all of these before calling the physical workflow DONE:

1. non-root SANE scan works,
2. 300 dpi real scan works,
3. scanbd daemon remains running correctly,
4. all five physical buttons are mapped,
5. one button press never produces two scans,
6. PDF Start can start scanning with no pre-run terminal command,
7. repeated PDF Start adds pages to the same document,
8. PDF Finish finalizes that document,
9. Auto Scan repeatedly creates separate Batch items,
10. PDF Finish closes Batch mode,
11. browser/server restart does not lose Pending scans,
12. Paperless outage does not lose a scan,
13. successful submit appears in Paperless with selected metadata.

Items 1, 2 and raw button mapping have already been validated on the reference LiDE 400 environment. scanbd/scanbm production coordination still requires completion.

## 24. Non-goals for initial MVP

Do not add these unless required by the core workflow:

- AI document classification,
- AI OCR,
- cloud scanning,
- mobile native app,
- arbitrary filesystem document manager,
- replacement Paperless search,
- replacement Paperless permissions system,
- complex image editor,
- automatic deletion of originals before Paperless success.

Paperless OCR remains sufficient for initial searchable text. OCR language setup can be handled in Paperless itself.

## 25. Implementation phases

### Phase 0 — Fork hardening and architecture

- preserve upstream MIT attribution,
- production server configuration,
- security defaults,
- add persistent app-data layout,
- introduce SQLite migration/bootstrap,
- keep upstream tests green.

### Phase 1 — Pending workflow without physical buttons

- split capture from upload,
- persistent Pending documents,
- preview,
- metadata form,
- dynamic Paperless metadata,
- explicit Submit,
- retry behavior.

This phase should work from the existing browser Scan button first.

### Phase 2 — Document and Batch state machine

- persistent session model,
- multi-page Document mode,
- independent-item Batch mode,
- queue navigation,
- Apply to remaining/all.

### Phase 3 — scanbd physical-button adapter

- normalized hardware events,
- configurable target mapping,
- duplicate-event suppression,
- verified scanbd/scanbm device handoff,
- no-terminal daily workflow.

### Phase 4 — polish and deployment

- owner-facing settings,
- robust status/history,
- retention policy,
- backup/restore notes,
- production systemd unit,
- Tailscale/reverse-proxy guidance,
- final real-hardware acceptance tests.

## 26. Design principle

Never trade away document safety for convenience.

If uncertain whether a scan was successfully stored or submitted, preserve the local Pending copy and require explicit review/retry rather than deleting or guessing.

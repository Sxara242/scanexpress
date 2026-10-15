# CODEX_HANDOFF.md

## Mission

This repository is a fork of baztian/scanexpress.

Do not rewrite ScanExpress from scratch. Preserve useful upstream behavior and tests while evolving it into the workflow defined in:

    docs/SCANNER_MANAGER_MASTER_SPEC.md

Read that file before making architectural changes.

## Current verified upstream baseline

Useful existing components include:

- Flask backend.
- SANE/scanimage execution.
- configurable per-device scanimage arguments.
- device scan locking.
- TIFF -> PDF conversion.
- Paperless upload.
- Paperless task polling.
- recent upload UI/history.
- Flask-Login/config infrastructure.
- Python unit tests and Playwright E2E tests.

The fork must preserve MIT attribution for upstream code.


## Current implementation status — 2026-10-14

Merged to `main`:

- Phase 0: production hardening, Gunicorn/loopback deployment, persistent app-data layout, SQLite bootstrap, CI hardening.
- Phase 1: persistent Pending PDFs, review page, PDF preview/download, dynamic Paperless metadata, create-new metadata objects, explicit Paperless submit, retry/task-state handling.
- Phase 2: persistent Document/Batch session state machine.

Phase 2 behavior now implemented:

- one open scan session per user,
- Document mode persists each captured page until Finish,
- repeated Document captures append to the same persistent session,
- Finish creates one multi-page Pending PDF,
- Batch mode creates independent Pending documents per capture,
- Finish closes Batch without merging its items,
- switching Document <-> Batch is rejected until Finish,
- open session/page spool survives process restarts,
- scan errors are recorded without deleting already captured pages/items,
- batch Pending navigation is session-aware,
- Apply to remaining / Apply to all copies shared metadata but deliberately preserves independent titles.

Phase 2 PR CI passed before merge. Main post-merge CI should also remain green before starting production deployment changes.

Next engineering phase:

**Phase 3 — physical button adapter and scanbd/scanbm coordination.**

Do not bypass the existing session APIs. Hardware events should map into the Phase 2 state machine rather than duplicating capture/business logic.

## Phase 3 software status — 2026-10-15

Merged to `main`:

- local Unix-domain hardware event socket,
- configurable target -> action mapping,
- serialized background hardware worker,
- duplicate-inflight suppression,
- monotonic debounce,
- scanbd action adapter,
- LiDE 400 deployment mapping,
- scanbd/scanbm/SANE-net deployment documentation,
- unit tests for event mapping, debounce, socket transport and app dispatch.

Reference mapping remains:

- target 5 -> Document page,
- target 6 -> Finish,
- target 2 -> Batch item,
- targets 1 and 3 -> reserved/ignored.

Important: Phase 3 is **software-complete but not hardware-accepted**. The Dell + Canon LiDE 400 still requires real scanbd/scanbm validation. Do not mark the physical-button workflow DONE until the real-device acceptance gates pass.

Next stopping point:

1. Inspect the Ubuntu scanbd package layout and service/socket units on the Dell.
2. Confirm where the package expects action scripts and how scanbm is socket-activated.
3. Prove actual action-script invocation count per physical press with a logger.
4. Configure SANE net-backend -> scanbm and direct pixma backend separation.
5. Install the ScanExpress adapter and then run the real Document / Batch / Finish acceptance tests.

## Reference hardware

Canon CanoScan LiDE 400 on Linux/SANE.

Validated physical scanbd target mapping:

- PDF Start -> target 5
- PDF Finish -> target 6
- Auto Scan -> target 2
- Copy -> target 1
- Send -> target 3

Observed real-hardware behavior: some single physical button presses can generate duplicate scanbd triggers. Treat this as a correctness requirement, not an edge case.

Do not hardcode the LiDE 400 mapping into generic core logic. Implement configurable event mapping.

## Desired workflow

### Document mode

- target 5 while idle: create/open document session and scan page 1.
- target 5 while document open: scan next page.
- target 6: finish and create one multi-page Pending PDF.

### Batch mode

- target 2 while idle: open batch and scan independent item 1.
- every later target 2: scan another independent item.
- target 6: close the batch.
- every batch item remains a separate Pending document.

Targets 1 and 3 are reserved initially.

## Critical architecture change

Upstream currently scans, converts, then uploads immediately.

New architecture must split this into:

    capture/finalize -> persistent Pending -> review/metadata -> explicit Paperless submit

Pending jobs must survive:

- browser refresh,
- service restart,
- temporary Paperless failure.

Use persistent storage, expected to be SQLite plus an application-data spool directory.

## Metadata UI

Pending review must support:

- Title
- Document date
- Document Type
- Correspondent
- Storage Path
- Tags (multi-select)

Fetch values dynamically from Paperless through backend proxy endpoints.

Support owner/admin Create New for relevant metadata objects.

Never expose Paperless API token to browser JavaScript.

## Preview and queue

Required:

- large preview,
- zoom,
- rotate,
- previous/next,
- page count,
- batch position such as 1 / 50,
- Apply to remaining/all for shared batch metadata.

Do not alter source scan quality just to make previews lighter; generate separate preview assets.

## Physical button integration warning

scanbd may hold the scanner open while polling.

Do not simply add a scanbd action that blindly launches the existing direct scanimage path.

The scanner execution path must validate scanbd/scanbm/SANE-net coordination so polling and scanning do not race for the USB device.

Keep hardware events and scan execution behind adapters so this can be tested without real hardware.

## Duplicate safety

Guarantee one intended press cannot produce two scans.

Use:

- atomic scan lock,
- monotonic debounce,
- state-aware acceptance,
- idempotent Finish.

Add automated tests for duplicate events.

## Security

Before reference production deployment:

- remove debug=True production behavior,
- use production WSGI,
- loopback bind by default,
- implement/verify secure session cookies,
- fail safely on missing production secrets,
- do not log tokens/passwords,
- protect pending-document download endpoints,
- keep secrets and captured documents out of Git.

## Implementation order

Do not start with scanbd integration.

Recommended order:

1. ~~Add persistent Pending model/spool and migrations.~~ DONE
2. ~~Split scan/finalize from Paperless upload.~~ DONE
3. ~~Add preview + metadata + explicit submit.~~ DONE
4. ~~Add Document/Batch state machine.~~ DONE
5. **NEXT:** Add hardware-event adapter and scanbd/scanbm coordination.
6. Do real LiDE 400 acceptance testing.

This order keeps most logic testable without hardware.

## Definition of first milestone

A browser-triggered scan should:

1. use existing scanner code,
2. create a persistent Pending PDF instead of uploading immediately,
3. survive app restart,
4. appear in a review screen with preview,
5. allow title + Paperless metadata selection,
6. submit explicitly,
7. retain retryable state until Paperless confirms success.

Keep existing upstream tests green and add focused tests for every new state transition.

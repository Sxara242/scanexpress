# ScanExpress submitted-PDF retention — Phase 4 safety policy

## Current implementation: inspection only

`storage retention-preview` prints a **read-only** JSON report of local
submitted PDFs that would satisfy a proposed retention age.

**This command never deletes any file, edits the SQLite database or modifies
Paperless.** There is no cleanup scheduler, systemd timer, retention setting
enabled in production, or executable deletion command in this release.

Example on the reference Dell installation:

```sh
sudo env SCANEXPRESS_DATA_DIR=/var/lib/scanexpress /opt/scanexpress/.venv/bin/python -m storage retention-preview --days 30
```

No commands should be run to remove `/var/lib/scanexpress/pending` by hand.

## Proposed safeguards for a later opt-in cleanup feature

- Keep `ready`, `failed` and `submitting` copies indefinitely.
- Delete **only** a local copy with `state='submitted'`, a non-empty
  `paperless_document_id`, and a confirmed state timestamp beyond the
  configured retention interval.
- Reconfirm success/status immediately before any actual deletion.
- Reject paths outside the app-controlled Pending directory and symlinks;
  make sure no other document references the same file.
- Keep the metadata/history row, including the Paperless document ID.
- Log every proposed and completed action; never remove Paperless originals
  or archived copies. Ensure an independent tested backup exists.
- No automation by default. Require separate owner approval to introduce
  or activate an actual deletion capability.

## Report limitations

The preview uses the local ScanExpress `updated_at` timestamp as a
conservative proxy for submission age. Future cleanup must record an
unambiguous `submitted_at` value because `updated_at` can also reflect
later application state changes.

The report includes IDs and byte counts, but does **not** verify Paperless
documents are still available. This is one reason deletion remains disabled.

# Penfield Import 2.0.1 release candidate

This branch is for external review. Version 2.0.0 could exit successfully after a rejected write and could skip data when reusing a checkpoint. Keep the original archives and all existing checkpoints when investigating an earlier restore.

## Changes for operators

- Run a local preview before the first write. It reads the ZIP or vault and reports supported transformations and graph problems. A ZIP preview can succeed while reporting partial work that a restore will surface.
- Keep the checkpoint when a restore stops. It binds receipts to the source and destination and records uncertain writes. A normal retry preserves confirmed successes.
- A document upload can return HTTP 400 after creating a failed server record. Inspect the destination before any replay. A successful upload may still have pending vector indexing.
- Git vault imports enumerate and check the complete committed tree before writing siblings or advancing the checkpoint. Preview and the saved checkpoint identify each excluded path and reason. If a previously imported path was edited, deleted or renamed, reconcile its destination record.

## Restoring with the candidate

1. Create a virtual environment and install the package from the source root. Follow the installation and authentication instructions in the [README](README.md).
2. Run `penfield-import INPUT --dry-run`. Review protected types, graph references and unsupported data. Preview makes no API calls or checkpoint changes.
3. Choose the destination explicitly with `--base-url` or export `PENFIELD_BASE_URL`. Use a dedicated checkpoint directory per input and destination. For internal dev, set the dev origin before login or API-key exchange. Production users can sign in with the device code or use a production API key.
4. Run the import and retain its output and checkpoint. A partial ZIP returns 2. A stopped ZIP or incomplete vault returns nonzero. A protected identity exclusion is reported separately.
5. Retry a definite rejection using the same input, destination and state. If a write has an uncertain outcome, inspect its source keys and compare the destination before any replay. A fresh checkpoint can create duplicates.

## Compatibility and verification

The candidate refuses automatic reuse of older, unbound ledgers. An existing 2.0.0 success summary is insufficient evidence of completeness. Compare mapped records and source payloads against the destination, especially artifact collisions, skipped or failed relationships and saved context membership. Use separate destination state for any fresh test restore. The bundled verifier requires a checkpoint bound to a source digest and a verified destination. A v2.0.0 checkpoint alone cannot establish either. Preserve the old ledger and check its mapped objects separately.

At write time, the client reads back accepted memories and edges, compares text artifacts and checks original document bytes. Processing and embedding completion are separate from byte verification. Service-managed fields, protected memory types and binary artifacts have the preservation limits described in the README. A no-op resume relies on the saved receipts.

Context verification checks the name and member count in the checkpoint content and preserves caller-supplied checkpoint metadata. Document downloads use the same client identity header as uploads. The report checks chunk-level embedding progress, retains pending outcomes for a later status refresh, and identifies document tags the API cannot store.

The source distribution includes the dev-only content reset helper. It inventories inactive memories, relationships, documents and recursive artifact paths. After an authorized reset it checks each original ID or path for HTTP 404 and enumerates the destination again. Usage and required scopes appear in the README.

## Recovering a nested vault after a Git error

R6 (`2f692df`) could import only the visible files from a nested vault when the enclosing repository’s HEAD was damaged, then record completion with no commit SHA. The candidate checks enclosing repository markers before accepting plain-directory mode. Preview, import and reset stop on an unverifiable enclosing repository.

Repair the repository first. From the vault directory, check `git rev-parse --show-toplevel` and `git rev-parse --verify HEAD`. Confirm the intended commit, restore missing tracked inputs and rerun preview and import using the original checkpoint and destination. A matching earlier receipt is retained. A changed previously restored file requires reconciliation.

## Earlier review builds

R5b (`0d00652`) could advance past a protected note whose committed version had become importable. With the correct working copy, repeating the import now restores the missing note and retains confirmed siblings. R5b could also accept unexpected tags on an untagged memory or a different media type for a document explicitly declared as `application/octet-stream`. Resuming rechecks affected receipts against the source.

On dev, a PDF with no extractable text returned HTTP 400 and left a document with failed processing. ZIP and vault imports now preserve the upload intent and stop on retry. Operators upgrading from an older checkpoint with an unreceipted document failure should reconcile it against the destination before another upload.

The first 2.0.1 review build, `675da708`, could accept stale context references and could equate JSON booleans with numbers when comparing relationship evidence. For a ZIP restored with that build, retain the original input and checkpoint and run the current read-only verifier. It reports affected rows, including repeated document IDs. It also checks explicit UUID references in context descriptions. An absent referenced source can produce an incomplete result even when every context member listed in the archive has been restored. Repeating an import against a populated destination with a fresh checkpoint can create duplicates.

Incremental Git imports check new notes, artifacts and documents against the selected commit before sending them. Same-note heading and block links remain in the original text. Preview lists contexts blocked by a cycle and the source rows involved. Nullable optional edge fields use the API defaults.

R2 (`d4d14e1`) could advance past a missing committed file, record a skipped vault artifact as uploaded and trust an incorrect older edge receipt. This revision checks the complete eligible Git additions, hashes the artifact bytes used for upload and compares legacy edge receipts with a fresh destination read. A completed edge without an ID requires reconciliation. Markdown code examples no longer create graph edges. UUIDs in graph references resolve regardless of letter case.

R3 (`fc0924c`) could omit a committed file on a first Git import. Older ZIP receipts could also hide a Boolean/number change in stored metadata. This revision checks the first Git snapshot before writes and rechecks metadata on upgrade for memories, contexts and documents. Two distinct vault notes must receive distinct IDs. An unavailable artifact media type is a partial result even when its bytes match.

R4 (`840bda1`) could advance its saved Git commit past an edited input while the working copy still held the previously imported text. This revision checks the commit diff before writing a sibling or advancing the checkpoint, and applies the same eligibility check in preview. Completed and interrupted legacy Git imports must pass a full snapshot check on their first resume. Older ZIP memory receipts now qualify all comparable source fields, including numeric importance and confidence. A self-relationship with case-variant UUID endpoints fails ZIP preflight before any write.

## External review before merge

Exercise the installed wheel against disposable dev destination state. Include a rejected write followed by retry, an occupied artifact path with different content, a malformed late record, the same archive with a second destination, nested contexts, document download, duplicate context names and OAuth account rotation. Verify fixture cleanup by ID. Keep private exports, credentials and local reconciliation logs outside the repository.

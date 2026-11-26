# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [2.0.1] - 2026-11-26

### Fixed

- Stop preview, import and reset when a nested vault’s enclosing Git repository cannot supply a valid HEAD. Preserve prior receipts and reject an unrelated outer repository as the source of the commit.
- Detect tracked notes becoming eligible after a protected baseline. Recover the omitted note with its committed bytes and the original checkpoint. Compare empty memory tag lists and explicitly declared generic document media types on fresh restores and upgrades.
- Validate the entire ZIP before writes, including record identities, graph references and payload files. Preview preserves checkpoints, including when combined with `--reset`.
- Bind ZIP recovery to the archive digest and destination identity. Keep uncertain creates journaled, retry definite failures and report an incomplete restore with a nonzero exit.
- Verify memory fields, relationship evidence, context membership and descriptions, artifact contents and document downloads. Report conflicting objects, unverifiable fields, omitted tags and incomplete context references per source record.
- Resolve nested contexts and case-variant UUID references. Reject self-relationships in ZIP preflight. Identify cycles and missing references during preview. Use API defaults for null optional direction, strength and confidence.
- Check older receipts for incorrect memory fields, JSON type mismatches and conflicting destination IDs. Compare a declared artifact media type on readback and retain partial results on repeat.
- Check committed Git inputs before the first vault import, before resuming older checkpoints and before advancing to a newer commit. A missing or edited tracked input stops the run before sibling writes.
- Preserve original oversized notes under distinct paths. Recheck planned vault artifacts before upload. Exclude Markdown code examples and same-note navigation from graph edges. Handle Unicode paths, attachment-only additions and ambiguous note names.
- Preserve evidence of uncertain document uploads, including HTTP 400 when extraction may already have created a failed document. Guard retries from older checkpoints with a document failure and no receipt. Track chunk indexing separately from verified original bytes.
- Honor `PENFIELD_BASE_URL`, isolate cached grants by origin, verify the destination on token renewal and restrict credentialed redirects. Raise the minimum build backend to setuptools 61 and include the test collection configuration in source distributions.

### Added

- A read-only ZIP verifier that produces one JSONL result per source record.
- A dev-only content reset helper with recursive artifact discovery, paginated inventories, tenant verification and a post-delete read for every ID or path.
- The README in the wheel's package description and the changelog, release notes and environment example in the source distribution.

## [2.0.0] - 2026-06-03

### Added

- **ZIP/JSONL import** — Import Penfield portal exports (v1 ZIP archives) directly.
  Processes memories, relationships, contexts, artifacts, and documents in phase order
  with full checkpoint/resume support.
- `PenfieldClient.create_memory()` extended with `memory_type`, `importance`,
  `confidence`, `source_type`, and `metadata` parameters.
- `PenfieldClient.upload_document_bytes()` for in-memory document uploads (used by
  ZIP import; existing `upload_document()` delegates to it).
- Separate ZIP checkpoint file (`.penfield_zip_import_checkpoint.json`) so vault and
  ZIP imports don't interfere.
- Dry-run support for ZIP imports (`--dry-run` shows manifest counts and type handling).
  ZIP dry-run works without authentication.
- 41 new tests covering all ZIP import phases and extended client methods (195 total).

### Fixed

- **ZIP import OAuth authentication** — ZIP imports now work with OAuth device code
  flow (`--login`), not just API keys. Previously only `PENFIELD_API_KEY` worked for
  ZIP imports due to missing `access_token`/refresh callback in client construction.
- **ZIP dry-run required auth** — `--dry-run` for ZIP imports no longer requires
  authentication, matching vault dry-run behavior.
- **ZIP relationship circuit breaker** — The ZIP relationship individual fallback now
  aborts after 3 consecutive server errors, matching the vault path's behavior.
  Previously it would retry every relationship in the batch regardless of failures.
- **ZIP re-run early exit** — Re-running a completed ZIP import now prints "Previous
  ZIP import completed" and exits, matching vault behavior. Previously it silently
  re-ran the verification phase.
- **Binary artifact detection** — Artifacts containing null bytes in the first 8 KB
  are identified and skipped. The original release announcement incorrectly described an upload.
- **Artifact size validation** — Artifacts exceeding the 1 MB API limit are skipped
  with a warning and checkpointed so they are never re-attempted.
- **Ambiguous filename collision** — When multiple vault files share the same filename,
  relationship targets for that name are now skipped with a warning instead of
  silently resolving to an arbitrary match.

### Changed

- Input path now accepts `.zip` files in addition to directories. The CLI routes
  automatically based on file extension.
- Integration test collection fixed via `conftest.py` (`collect_ignore` for standalone
  integration test files).

## [1.0.2] - 2026-05-20

### Added

- Path safety validation on all file upload paths (artifacts, vault artifacts,
  documents). Rejects null bytes, colons, directory traversal (`..`), Windows
  reserved device names (CON, PRN, NUL, AUX, COM1–9, LPT1–9 regardless of
  extension count), and paths exceeding 1024 UTF-8 bytes.
- Skipped paths are recorded as checkpoint sentinels — never re-attempted on
  resume.
- Verify and report phases correctly exclude skipped entries from counts.

## [1.0.1] - 2026-05-19

### Fixed

- Bulk relationship batch errors (409 conflict, 5xx server error) no longer mark the
  entire batch as failed. The tool falls back to individual creates, skipping duplicates
  and checkpointing after each item. A circuit breaker aborts the batch after 3
  consecutive server errors to avoid hammering a sick server. Fixed #2.

## [1.0.0] - 2026-04-13

Initial public release.

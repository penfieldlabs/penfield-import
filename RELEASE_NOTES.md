# Penfield Import 2.0.1 release candidate

This branch is for external review. Version 2.0.0 could exit successfully after a rejected write and could skip data when reusing a checkpoint. Keep the original archives and all existing checkpoints when investigating an earlier restore.

## Restoring with the candidate

1. Create a virtual environment and install the package as described in the README.
2. Run `penfield-import INPUT --dry-run`. Review protected types, graph references and unsupported data. Preview makes no API calls or checkpoint changes.
3. Choose the destination explicitly with `--base-url` or export `PENFIELD_BASE_URL`. Use a dedicated checkpoint directory per input and destination. Set credentials through the environment or use the device code displayed by `--login`.
4. Run the import and retain its output and checkpoint. A partial ZIP returns 2. A stopped ZIP or incomplete vault returns nonzero. A protected identity exclusion is reported separately.
5. Retry a definite rejection using the same input, destination and state. If a write has an uncertain outcome, inspect its source keys and compare the destination before any replay. A fresh checkpoint can create duplicates.

## Compatibility and verification

The candidate refuses automatic reuse of older, unbound ledgers. An existing 2.0.0 success summary is insufficient evidence of completeness. Compare mapped records and source payloads against the destination, especially artifact collisions, skipped or failed relationships and saved context membership. Use separate destination state for any fresh test restore.

At write time, the client reads back accepted memories and edges, compares text artifacts and checks original document bytes. Processing and embedding completion are separate from byte verification. Service-managed fields, protected memory types and binary artifacts have the preservation limits described in the README. A no-op resume relies on the saved receipts.

## External review before merge

Exercise the installed wheel against disposable dev destination state. Include a rejected write followed by retry, an occupied artifact path with different content, a malformed late record, the same archive with a second destination, nested contexts, document download, duplicate context names and OAuth account rotation. Verify fixture cleanup by ID. Keep private exports, credentials and local reconciliation logs outside the repository.

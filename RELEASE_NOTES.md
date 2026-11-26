# Penfield Import 2.0.1

Version 2.0.0 could exit successfully after a rejected write and could skip data when reusing a checkpoint. Version 2.0.1 validates input before writing, binds checkpoints to the input and destination, verifies stored records, and reports partial restores so operators can recover without duplicating successful writes. If you restored with 2.0.0, keep the original archives and all existing checkpoints when investigating that restore.

## Changes for operators

- Run a local preview before the first write. It reads the ZIP or vault and reports supported transformations and graph problems. A ZIP preview can succeed while reporting partial work that a restore will surface.
- Keep the checkpoint when a restore stops. It binds receipts to the source and destination and records uncertain writes. A normal retry preserves confirmed successes.
- A document upload can return HTTP 400 after creating a failed server record. Inspect the destination before any replay. A successful upload may still have pending vector indexing.
- Git vault imports enumerate and check every committed path within the selected vault before writing siblings or advancing the checkpoint. Preview and the saved checkpoint identify each excluded path and reason. If a previously imported path was edited, deleted or renamed, reconcile its destination record.

## Restoring with 2.0.1

1. Create a virtual environment and install the package from the source root. Follow the installation and authentication instructions in the [README](README.md).
2. Run `penfield-import INPUT --dry-run`. Review protected types, graph references and unsupported data. Preview makes no API calls or checkpoint changes.
3. Choose the destination explicitly with `--base-url` or export `PENFIELD_BASE_URL`. Use a dedicated checkpoint directory per input and destination. Production users can sign in with the device code or use a production API key.
4. Run the import and retain its output and checkpoint. A partial ZIP returns 2. A stopped ZIP or incomplete vault returns nonzero. A protected identity exclusion is reported separately.
5. Retry a definite rejection using the same input, destination and state. If a write has an uncertain outcome, inspect its source keys and compare the destination before any replay. A fresh checkpoint can create duplicates.

## Compatibility and verification

Version 2.0.1 refuses automatic reuse of older, unbound ledgers. An existing 2.0.0 success summary is insufficient evidence of completeness. Compare mapped records and source payloads against the destination, especially artifact collisions, skipped or failed relationships and saved context membership. Use separate destination state for any fresh test restore. The bundled verifier requires a checkpoint bound to a source digest and a verified destination. A 2.0.0 checkpoint alone cannot establish either. Preserve the old ledger and check its mapped objects separately.

At write time, the client reads back accepted memories and edges, compares text artifacts and checks original document bytes. Processing and embedding completion are separate from byte verification. Service-managed fields, protected memory types and binary artifacts have the preservation limits described in the README. A no-op resume relies on the saved receipts.

Context verification checks the name and member count in the checkpoint content and preserves caller-supplied checkpoint metadata. Document downloads use the same client identity header as uploads. The report checks chunk-level embedding progress, retains pending outcomes for a later status refresh, and identifies document tags the API cannot store.

The source distribution includes the dev-only content reset helper. It inventories inactive memories, relationships, documents and recursive artifact paths. After an authorized reset it checks each original ID or path for HTTP 404 and enumerates the destination again. Usage and required scopes appear in the README.

## Recovering a nested vault after a Git error

Version 2.0.1 checks enclosing repository markers before accepting plain-directory mode. Preview, import and reset stop when a nested vault's enclosing repository cannot be verified.

Repair the repository first. From the vault directory, check `git rev-parse --show-toplevel` and `git rev-parse --verify HEAD`. Confirm the intended commit, restore missing tracked inputs and rerun preview and import using the original checkpoint and destination. A matching earlier receipt is retained. A changed previously restored file requires reconciliation.

The full list of changes is in the [CHANGELOG](CHANGELOG.md).

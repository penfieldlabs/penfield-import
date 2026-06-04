# Penfield Import v2.0.0

Import Penfield portal exports or Obsidian vaults into [Penfield](https://penfield.app).

## What's new in v2.0.0

- **ZIP/JSONL import** — Penfield portal exports (v1 ZIP archives) can now be imported directly. The tool validates the manifest, processes all five JSONL data files in phase order (memories → relationships → contexts → artifacts → documents), and checkpoints after each record for crash safety.
- **Protected type handling** — `identity_core` memories are skipped (the API does not allow creation). `personality_trait` memories are downgraded to `fact` with a prefix note.
- **Context reconstruction** — Exported contexts are recreated as `checkpoint` memories via the memories API, preserving checkpoint names and remapped memory references.
- **Binary artifact detection** — Artifacts with null bytes in the first 8 KB are correctly uploaded as binary instead of being misidentified as UTF-8 text.
- **Artifact size validation** — Artifacts exceeding the 1 MB API limit are skipped with a warning and checkpointed so they are never re-attempted on resume.
- **Ambiguous filename collision fix** — Vault imports with duplicate filenames across directories now skip ambiguous relationship targets with a warning instead of resolving them arbitrarily.
- **Extended memory creation** — `memory_type`, `importance`, `confidence`, `source_type`, and `metadata` are now passed through to the API when present in export data.

## Fields not preserved on round-trip

The following fields are present in exports but assigned server-side and cannot be set via the API:

- Timestamps (`created_at`, `updated_at`) on memories, relationships, and documents
- Ownership (`user_id` on memories, `created_by` on relationships, `uploaded_by` on documents)
- `is_auto_detected` on relationships

## Authentication

Two options:
- **OAuth device code flow** (recommended) — `penfield-import --login` opens a browser for authorization. Tokens are cached and refreshed automatically.
- **API key** — set `PENFIELD_API_KEY` environment variable. The key is exchanged for a short-lived token before any requests.

## Crash safety

Every operation is checkpointed. If the import is interrupted, re-run the same command and it picks up where it left off. ZIP and vault imports use separate checkpoint files so they don't interfere.

## Requirements

- Python 3.9+
- [PyYAML](https://pypi.org/project/PyYAML/) (installed automatically via `pip install .`)

## License

AGPL-3.0

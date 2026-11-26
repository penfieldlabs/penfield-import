# Penfield Import

Import into [Penfield](https://penfield.app) from two sources:

1. **Penfield portal exports** — ZIP archives produced by the Penfield web portal (EXPORT_FORMAT_SPEC_v1). Restores memories, relationships, contexts, artifacts, and documents into a new tenant.
2. **Obsidian vaults / markdown directories** — Any collection of `.md` or `.txt` files (Obsidian, Foam, Logseq, Zettelkasten, or plain folders).

Built for Obsidian users and the [obsidian-wikilink-types](https://github.com/penfieldlabs/obsidian-wikilink-types) plugin. Also works on any collection of `.md` or `.txt` files - Foam workspaces, Logseq exports, Zettelkasten directories, or plain folders of notes. More file types coming soon.

For the richest vault import, use [obsidian-wikilink-types](https://github.com/penfieldlabs/obsidian-wikilink-types) to add typed relationships to your notes before importing. The plugin syncs typed wikilinks (`[[Note|display @supports]]`) to YAML frontmatter, which this tool reads and sends to Penfield as graph relationships. No relationships? No problem - the tool imports everything without them.

## ZIP import (Penfield portal exports)

The tool auto-detects `.zip` files and processes them as Penfield portal exports.

```bash
# Preview what's in the export
penfield-import /path/to/export.zip --dry-run

# Run the import
penfield-import /path/to/export.zip

# Specify a checkpoint directory
penfield-import /path/to/export.zip --checkpoint-dir /tmp/import-state
```

### How ZIP import works

Preview reads the archive locally. It checks the manifest and JSONL rows, record identities and referenced payloads. The manifest and each JSONL row are limited to 16 MiB. Review the counts, protected types, and unresolved references in context memberships and descriptions before choosing a destination. Use a different checkpoint directory for each archive and destination.

1. **Memories.** The importer creates ordinary memories with their supported metadata. It excludes protected `identity_core` records and converts `personality_trait` to `fact`. The attribution prefix is added only when it fits.
2. **Contexts.** Saved contexts become checkpoint memories. The checkpoint name and restored member count are verified in the stored JSON content. The memory metadata preserves fields from the export’s `checkpoint_metadata` object. Members can point to ordinary memories or other contexts, including later rows. UUID references in descriptions, memberships and relationship endpoints accept either case. The importer resolves these dependencies and preserves membership order. An absent member, a dependency cycle or a destination conflict remains visible in the result.
3. **Relationships.** The importer remaps endpoints and submits batches of up to 100. A definite batch validation rejection can be isolated to the offending row. The importer forwards nonempty `traversal_properties` objects and compares them on readback. If the API omits the field or returns different properties, the checkpoint retains the edge ID and records the source row as partial. A rerun reads that edge again without recreating it. An existing edge qualifies as identical only after its supplied properties match. [Relationship API](https://docs.penfield.app/api/endpoints/relationships/)
4. **Artifacts.** UTF-8 text up to 1 MiB is eligible. Occupied paths require matching destination content. Different bytes at the same path are a conflict. Binary artifacts require a separate preservation decision.
5. **Documents.** Eligible originals up to 20 MiB are sent with their metadata. The importer checks the returned ID and downloads the original bytes for comparison. Indexing is asynchronous. The report distinguishes verified original bytes from pending, ready, unknown or failed chunk indexing. A rerun refreshes unresolved indexing by document ID. Failed chunks result in an incomplete restore even when the top-level status says completed.

Successful writes receive a destination readback. Each completed ZIP relationship needs a destination ID. On upgrade, the importer reads older relationship receipts and compares the stored edge to the source, including JSON types in metadata and evidence. A mismatch stays incomplete on retry. Upgrades also read back source-declared metadata for memories, contexts and documents whose prior receipts lack a typed comparison. A Boolean and a number are distinct even when nested inside arrays. A missing or different source-declared artifact media type leaves the artifact partial, and a retry reads the existing path without uploading it again. A subsequent no-op uses the checkpoint's verified receipts. It does not scan for objects that somebody later edited or deleted.

### Verify a restored ZIP against the destination

Run the verifier from a checkout or unpacked source distribution in the importer's virtual environment. Supply the exact ZIP, its checkpoint directory, and a new output file in an existing directory.

```bash
python tools/verify_restore.py /path/to/export.zip \
  --checkpoint-dir /path/to/import-state \
  --base-url https://api.penfield.app \
  --output /path/to/review/restore-readback.jsonl
```

The verifier uses `PENFIELD_API_KEY` or a cached OAuth grant for that origin. It checks the archive digest and the verified destination identity, then fetches every mapped object by ID or artifact path. It checks document download bytes and reports chunk indexing separately. Source-managed fields are compared using the documented import transformations. Source timestamps and ownership can differ. Additional destination records outside the checkpoint are outside this comparison.

The JSONL report has one row per source record and is created with owner-only permissions. Keep it private because it contains record identifiers. Exit `0` means every row matched or qualified for the protected identity exclusion. Exit `2` indicates a difference, absent receipt, partial context, unobservable relationship properties, omitted document tags, indexing still pending or unconfirmed, a read failure, or an unresolved write. For a relationship, `unobservable_fields` lists supplied properties omitted by the destination read and `mismatched_fields` lists returned fields whose values differ. Exit `1` indicates the check could not start or finish. The verifier leaves the checkpoint untouched.

### What completion means

A ZIP run exits `0` when the supported restore is accounted for, including the documented protected-memory exclusion. Exit `2` indicates an incomplete ZIP restore. Exit `1` indicates that the importer stopped, for example on invalid input, a mismatched checkpoint or an uncertain write. Read the per-section results and keep the checkpoint for reconciliation.

Ownership, server timestamps, evolution state, artifact modification dates, document tags and other server-managed fields can differ from the archive. Artifact media types are determined by the service. UUIDs in ordinary free text retain their original spelling. A context description token of the form `memory_id: <UUID>` is a dependency, just like a listed context member. Forward tokens use the new destination ID even when the target context appears later in the ZIP. Preview identifies self references, cycles and dependent rows by source line and ID. Restore defers these rows and continues with independent contexts. An absent or excluded description ID stays verbatim and is recorded in `context_unresolved_descriptions`, separately from missing memberships. The restore exits 2. Other contexts can still refer to a partially restored context with a confirmed destination ID.

When an older checkpoint lacks description verification, a resume reads the existing destination context and compares its description with the current source-to-destination map. A stale description remains incomplete until reconciled; the importer retains the existing receipt and avoids creating a duplicate.

A tagged document can have a verified upload and an incomplete restore. The ZIP checkpoint lists its original tags under `document_tag_omissions` and the command exits 2. The documents API accepts custom metadata and has no native document tag field.

Each source document needs its own destination ID, even when two files have identical names and bytes. ZIP relationship rows can share a destination ID only when their submitted payloads match and the service confirmed an identical existing edge for every row beyond the first. Two fresh creates returning the same ID stop the import. The verifier marks every row involved in an invalid shared receipt.

ZIP relationships with a null direction, strength, or confidence use the service default for that field. Metadata and evidence checks preserve JSON types throughout nested objects and arrays. For example, a boolean changed to a number is a mismatch. Numeric values such as `1` and `1.0` compare equally.

### When a create has an uncertain outcome

An interruption or lost response can leave the server with a new object and the client with an unconfirmed write. Keep the original input, checkpoint and command output together. A normal rerun stops at that write.

1. Read `in_flight` in a ZIP checkpoint or `pending_write` in a vault checkpoint. It identifies the operation and source keys. When the service returned an ID or path before verification stopped, `acknowledgement` contains the destination collection and keys that it returned. A batch can return multiple IDs, including automatically generated inverse relationships.
2. Verify the account and origin from the checkpoint. Retrieve each acknowledged object there. Compare memories and contexts to their transformed content and metadata, relationships to their mapped endpoints and evidence, artifacts to their exact path and contents, and documents to their metadata and original download. A server receipt alone establishes only that the service acknowledged the request.
3. If the response never supplied an ID, use the service request logs and an inventory taken before the restore to establish ownership and outcome. A content match or an empty search is insufficient to identify an interrupted create. Preserve an inconclusive checkpoint for service-assisted recovery.
4. On a disposable destination, the reproducible recovery path is to verify that it is empty, keep the original checkpoint for audit, and rerun from a separate checkpoint directory. For a populated destination, retain the checkpoint and verified objects for targeted reconciliation with the service operator. This version has no command to adopt a manually matched object or replay an uncertain create. Starting fresh against populated state can duplicate earlier successes.

## Vault import (Obsidian / markdown)

### How vault import works

The tool runs in seven phases, checkpointing after each for crash safety:

1. **Parse** - Reads all `.md`, `.txt`, `.markdown`, and `.text` files, extracts YAML frontmatter and typed relationships
2. **Oversized note artifacts** - Upload and verify the original full text for notes exceeding the 10K character memory limit.
3. **Memories** - Create a memory for each nonempty note. Oversized memories use a truncated or summarized copy that references the verified artifact.
4. **Exported Artifacts** - Uploads pre-existing artifact files from the `Artifacts/` directory (if present)
5. **Documents** - Uploads document files from the `Documents/` directory (if present) — supports `.pdf`, `.md`, `.txt`, `.epub`, `.json`, `.yaml`, `.yml`, `.py`, `.js`, `.ts`, `.csv`, `.xml`, `.html`, `.htm`
6. **Relationships** - Bulk-creates relationships between memories (batches of 100)
7. **Verify** - Reconciles planned source keys with verified receipts and checks the destination memory total. The report lists document processing status separately from the uploaded byte count. Failed processing makes the run incomplete. A retry refreshes the status of the existing document.

### Important: fresh Penfield knowledge base

This tool **creates** memories and relationships. It does not update or deduplicate against existing data. Run it against a fresh Penfield knowledge base, or expect duplicates.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

This installs the `penfield-import` command. All examples below use it. If you prefer to run without installing, substitute `python penfield_import.py` wherever you see `penfield-import`.

## Quick start

```bash
# Authenticate with Penfield (follow the displayed URL and code)
penfield-import --login

# Import a Penfield portal export ZIP
penfield-import /path/to/export.zip --dry-run   # preview
penfield-import /path/to/export.zip              # run

# Import an Obsidian vault or markdown directory
penfield-import /path/to/notes --dry-run         # preview
penfield-import /path/to/notes                   # run

# Check version
penfield-import --version
```

## Authentication

The tool supports two authentication methods:

Production defaults to `https://api.penfield.app`. For internal dev testing, set `PENFIELD_BASE_URL=https://api-dev.penfield.app` before supplying a dev credential, or pass `--base-url https://api-dev.penfield.app` on every command, including login and logout. Use the key issued by the selected environment. OAuth grants are cached separately by origin. The CLI reads exported environment variables.

### OAuth device code flow (recommended)

```bash
# Log in (open the displayed URL for authorization)
python penfield_import.py --login

# Check token status
python penfield_import.py --auth-status

# Force re-authentication
python penfield_import.py --login --reauth

# Log out of the selected API origin
python penfield_import.py --logout
```

Tokens are cached at `~/.config/penfield-import/tokens.json` (owner-only permissions) and refreshed automatically when they expire. No manual token management needed.

### API key (legacy)

```bash
export PENFIELD_API_KEY="tm_your_tenant_ak_your_key"
python penfield_import.py /path/to/notes
```

If `PENFIELD_API_KEY` is set, it takes priority over cached OAuth tokens. The API key is exchanged for a short-lived JWT before any requests are made. **The API key is never accepted as a CLI argument** (keys passed as arguments are visible in `ps aux`).

Both auth methods handle token refresh automatically. After token refresh the importer checks the destination identity again before continuing.

## What gets imported

### Memory content

Each note becomes one Penfield memory. By default, only the **note body** (text after the YAML frontmatter) becomes the memory content. YAML frontmatter metadata like `id`, `type`, `created`, `updated` is dropped — it's export scaffolding, not knowledge.

Relationships and tags are always extracted from frontmatter regardless of this setting.

For example, a note with:

```markdown
---
author: Jane Doe
status: reviewed
supports: ["[[Other Note]]"]
tags: [psychology, memory]
---

The actual note content here...
```

Becomes a memory with content:

```
The actual note content here...
```

With tags `["psychology", "memory"]`, a `supports` relationship to "Other Note", and `memory_type` set to the frontmatter `type:` value — all mapped directly to their Penfield API fields.

Use `--include-frontmatter` to preserve frontmatter metadata (author, status, etc.) as a readable header in the memory content. Relationship and tag keys are always excluded even with this flag.

If note bodies contain stale metadata blocks from prior import/export cycles (e.g., `id:`, `type:`, `created:` lines followed by `---`), these are automatically stripped before import.

### Memory type mapping

The `type:` field in frontmatter is mapped directly to Penfield's `memory_type`. Three types get special treatment:

- **`type: identity_core`** — skipped entirely. Identity core memories are managed by Penfield's personality system and cannot be created via the memories API.
- **`type: personality_trait`** — imported as `fact` with an `[Imported personality_trait]` prefix. Personality traits are also managed by the personality system; this is the closest equivalent that the memories API accepts.
- **`type: checkpoint`** — imported as `reference`. The `checkpoint` memory type is the persistence layer for MCP cognitive handoffs created by [`save_context`](https://penfield.app/docs/mcp/mcp-integration.md#save_context); each record stores a JSON `{"checkpoint_name", "description", "memory_ids"}` blob that `restore_context` and `list_contexts` parse at read time. A vault note lacks the structured membership data available in a portal ZIP. A checkpoint note in an exported vault has had its JSON unwrapped into a plain description and `references:` wikilinks during export, so re-importing it as a checkpoint would produce a malformed record that is no longer restorable. Downgrading to `reference` preserves its content and `references:` relationships as an ordinary summary memory that is still discoverable via search and graph traversal.

All other frontmatter types (`fact`, `insight`, `conversation`, `correction`, `reference`, `task`, `strategy`, `relationship`) are passed through to Penfield as-is.

### Relationship extraction

Relationships are optional. If your files have no typed frontmatter links, the tool imports everything as memories and artifacts - you still get a fully functional Penfield brain, just without graph edges connecting your notes.

For a much richer import, add typed relationships before importing. Two approaches:

**Option A: obsidian-wikilink-types plugin** - Install [obsidian-wikilink-types](https://github.com/penfieldlabs/obsidian-wikilink-types) in your Obsidian vault and use inline typed wikilinks (`[[Note @supports]]`). The plugin syncs these to YAML frontmatter automatically. See the plugin's [SKILL.md](https://github.com/penfieldlabs/obsidian-wikilink-types/blob/main/SKILL.md) for the full relationship type reference.

**Option B: LLM pre-processing** - Use Claude Code, Cursor, or any LLM with access to your files to analyze your notes and add typed relationship frontmatter before running the import. Point the LLM at the [SKILL.md](https://github.com/penfieldlabs/obsidian-wikilink-types/blob/main/SKILL.md) for the relationship type definitions and ask it to add appropriate frontmatter links. This works on any `.md` or `.txt` collection, not just Obsidian vaults.

**Option C: Inline wikilinks** - If your notes already have `[[wikilinks]]` in the body text (standard Obsidian practice), use `--relationships inline` to import them as `references` relationships. Inline code and fenced code examples stay in the note text and create no relationships.

### Relationship modes (`--relationships`)

| Mode | Description |
|------|-------------|
| `frontmatter` | Extract typed relationships from YAML frontmatter only (default) |
| `inline` | Extract `[[wikilinks]]` from body text as `references` relationships |
| `both` | Frontmatter + inline, deduplicated |

Use `frontmatter` (default) when importing from penfield-export or vaults with typed frontmatter. Use `inline` for human-built Obsidian vaults with no typed frontmatter. Use `both` when your vault has a mix of typed frontmatter and inline wikilinks.

Same-note heading and block links such as `[[#Heading]]` and `[[Current Note#^block]]` stay in the Markdown. A link to a heading in another note creates a relationship to that note. An explicit relationship to the current note without a fragment fails validation before any writes.

Frontmatter keys matching a standard relationship type with wikilink values are imported:

```yaml
---
supports: ["[[Note A]]", "[[Note B]]"]
contradicts: ["[[Note C]]"]
---
```

**The 24 standard types:**

| Category | Types |
|----------|-------|
| Knowledge Evolution | `supersedes`, `updates`, `evolution_of` |
| Evidence & Support | `supports`, `contradicts`, `disputes` |
| Hierarchy & Structure | `parent_of`, `child_of`, `sibling_of`, `composed_of`, `part_of` |
| Cause & Prerequisites | `causes`, `influenced_by`, `prerequisite_for` |
| Implementation & Testing | `implements`, `documents`, `tests`, `example_of` |
| Conversation & Attribution | `responds_to`, `references`, `inspired_by` |
| Sequence & Flow | `follows`, `precedes` |
| Dependencies | `depends_on` |

Non-standard types are silently skipped.

### Tags

If your frontmatter has a `tags` array, those are passed to Penfield's memory tags field (max 10, auto-normalized to lowercase-hyphenated by Penfield).

## Handling oversized notes

Notes over 10,000 characters can't fit in a single Penfield memory. The tool handles this by creating two records for each oversized note:

1. **A memory** — a truncated or summarized version of the note, with a reference to the artifact path where the full content is stored. This memory is searchable and appears in recall results.
2. **An artifact** — the full, unmodified note content stored at `/oversize-notes/<encoded-relative-path>`.

The memory is how you find the note. The artifact is how you read the full thing.

### Default mode (no LLM)

The note is smart-truncated at the nearest section heading or paragraph break (~9,500 chars). A notice with the artifact path is appended so you can retrieve the full document.

### LLM mode (`--llm`)

The formatted memory text and note filename are sent to the configured API. The frontmatter option controls which metadata appears in that text. The summary becomes the memory content, with the artifact path appended. Falls back to smart truncation if the LLM fails.

```bash
export LLM_API_KEY="your-key"
python penfield_import.py /path/to/vault --llm
```

Works with any OpenAI-compatible API — OpenRouter (default), OpenAI, local models, etc.

### Claude mode (`--claude`)

Uses [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude -p`) for summarization. Requires Claude Code installed and authenticated on your machine — no separate API key needed.

```bash
python penfield_import.py /path/to/vault --claude
python penfield_import.py /path/to/vault --claude --claude-model sonnet
```

The subprocess receives the same formatted text as LLM mode. It runs in a private temporary directory with built-in and MCP tools disabled. An unsupported CLI flag triggers the local truncation fallback. Defaults to `haiku`. Any model name accepted by `claude --model` works.

### Why artifacts, not documents?

Penfield documents have strict tier-based count limits. Artifacts don't. Artifacts also preserve your vault's directory structure via their path.

Artifacts are not searchable — that's why every oversized note also gets a memory. The memory makes the content discoverable via search; the artifact stores the full text for retrieval by path.

## CLI reference

```
penfield-import [INPUT_PATH] [options]
```

### Import options

| Flag | Default | Description |
|------|---------|-------------|
| `INPUT_PATH` | *(required for import)* | Path to a Penfield export `.zip` file or a directory of `.md`/`.txt` files |
| `--base-url` | `https://api.penfield.app` | Penfield API base URL |
| `--checkpoint-dir` | vault path | Where checkpoint/report files go |
| `--llm` | off | Enable LLM summarization (OpenAI-compatible API) |
| `--llm-base-url` | `https://openrouter.ai/api/v1` | LLM API base URL |
| `--llm-model` | `openai/gpt-4o-mini` | LLM model name |
| `--claude` | off | Use Claude Code CLI for summarization |
| `--claude-model` | `haiku` | Claude model name |
| `--relationships` | `frontmatter` | How to extract relationships: `frontmatter`, `inline`, or `both` |
| `--include-frontmatter` | off | Include YAML metadata in memory content |
| `--skip-dirs` | `_meta _templates _penfield _config Artifacts Documents` | Directory names to skip |
| `--dry-run` | off | Parse and report stats, no API calls |
| `--reset` | off | Start fresh after preflight and destination verification |
| `-v` / `--verbose` | off | Debug logging |
| `--version` | | Print version and exit |

### Authentication options

| Flag | Description |
|------|-------------|
| `--login` | Authenticate via OAuth device code flow, then exit |
| `--logout` | Clear cached OAuth tokens, then exit |
| `--auth-status` | Show OAuth token cache status, then exit |
| `--reauth` | Force re-authentication even if token is cached |

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PENFIELD_BASE_URL` | No | API origin. An explicit `--base-url` takes precedence. Export variables in your shell. |
| `PENFIELD_API_KEY` | No (alternative to `--login`) | Penfield API key (`tm_<tenant>_ak_<key>`). Takes priority over OAuth if set. |
| `LLM_API_KEY` | Only with `--llm` | OpenAI-compatible API key |

## Checkpoint and crash recovery

A ZIP checkpoint records the SHA-256 of the archive and the verified destination. A vault checkpoint records the source file hashes, import options and verified destination identity. Re-running retries rejected writes and skips confirmed successes. When an interruption leaves an unknown write outcome, the checkpoint identifies the record or batch. Reconcile it against the destination before continuing.

- Vault imports use `.penfield_import_checkpoint.json`
- ZIP imports use `.penfield_zip_import_checkpoint.json`
- Checkpoints are written atomically. Concurrent writers using the same checkpoint are rejected.
- `--reset` starts with fresh local tracking and can create duplicates at a populated destination. A dry run leaves the checkpoint untouched.
- Use `--checkpoint-dir` to store the checkpoint somewhere other than the source directory

### Incremental import (git vaults)

If the vault is in a git repository, the tool automatically tracks the commit SHA at import time. On the next run it detects files added since that commit and imports only the new ones — no flags needed.

```bash
# First run: imports everything, records commit SHA
python penfield_import.py /path/to/vault

# Later: add new notes to the vault, commit, then re-run
python penfield_import.py /path/to/vault
```

- On the first import and on increments, the importer checks all eligible committed inputs in scope. A missing committed file or a working copy that differs from the pinned Git blob stops the run before any destination write. The first import can also include eligible untracked working files. Restore them from the commit and retry with the same checkpoint. Vault artifacts are hashed again immediately before upload.
- Before advancing a saved Git commit, preview and import compare the committed changes with the paths already recorded. An edit, removal, or rename of a recorded input requires reconciliation even if the working copy still contains the previously imported bytes. Upgrades check the entire pinned tree before retrying a partial import or certifying an older baseline. The Git validation marker lives in the local importer checkpoint. Portal exports declare their format separately in the ZIP’s `manifest.json` under `schema_version`.
- Incremental imports compare each new note, artifact and document with its Git blob at the recorded commit. Text files may use CRLF working-copy line endings for an LF blob. Other differences stop the run before any writes. Commit the edit or restore the committed content before retrying. Changes to previously imported inputs require reconciliation with the destination.
- The checkpoint stores the full `{rel_path: memory_id}` map from all prior runs, so relationships from new notes to existing notes resolve correctly.
- For non-Git vaults, repeating a completed import checks the source snapshot and destination identity. A changed snapshot requires a separate restore decision.

## Directory structure

- Recursively finds all `.md` and `.txt` files under the root directory
- Directories named `_meta`, `_templates`, `_penfield`, `_config`, `Artifacts`, and `Documents` are skipped by default (configurable with `--skip-dirs`). `Artifacts/` and `Documents/` have their own dedicated import phases and should not be processed as memories.
- No specific directory structure is required - works with any folder layout
- Files are identified by their relative path (e.g., `concepts/trust.md`), so duplicate filenames in different directories are stored as separate memories
- **Duplicate filename caveat:** Relationship targets in frontmatter use note names without paths (e.g., `[[trust]]`). If multiple files share the same stem, use a vault-relative path such as `[[projects/trust.md]]` to identify the intended note. The preview reports ambiguous and unresolved links before import.

### Supported layouts

The tool recognizes the Penfield export layout but works with any subset:

- `Memories/` — markdown notes that become Penfield memories
- `Contexts/` — exported context checkpoints (imported as `reference` memories, see [Memory type mapping](#memory-type-mapping))
- `Artifacts/` — files uploaded to Penfield as artifacts, preserving directory structure as the artifact path
- `Documents/` — files uploaded to Penfield's documents system (PDFs, etc.) for chunked full-text search

Vault note and text artifact inputs must be valid UTF-8. Previews also inspect attachments and graph links. Regular files are read within the selected vault. Symlinked files and nested symlink directories are excluded, and symlinked attachment roots are rejected.

A vault may contain any subset of these directories. An `Artifacts/`-only or `Documents/`-only vault is a valid import — the tool skips phases that have no content without aborting.

## Rate limiting

The tool uses a sliding-window rate limiter (200 requests/minute) and handles server responses:

- **429 (rate limited):** Honors a valid Retry-After delay, otherwise uses exponential backoff. The last refusal is returned after the retry budget.
- **401 (unauthorized):** Automatically refreshes the token (OAuth refresh or API key re-exchange)
- **5xx (server error):** Read requests can retry with backoff. An ambiguous create stops for reconciliation.

The LLM client has independent retry logic.

## Requirements

- Python 3.9+
- The installed Markdown parser, markdown-it-py, identifies prose wikilinks.
- [PyYAML](https://pypi.org/project/PyYAML/) (installed automatically via `pip install .`)

> **Note:** PyYAML follows the YAML 1.1 spec, which treats `yes`, `no`, `on`, `off` as booleans. If your frontmatter contains these as literal string values, they will be parsed as `True`/`False`. This is standard PyYAML behavior, not a bug in this tool. Use quoted strings (`"yes"`, `"no"`) in your YAML if you need them as literal values.

## Reset a disposable dev tenant's content

Maintainers running restore tests can use `tools/reset_dev_content.py` from a checkout or source distribution in the importer's Python environment. First preview the counts for the expected dev tenant.

```sh
python tools/reset_dev_content.py --tenant "$EXPECTED_DEV_TENANT"
```

Adding `--apply` permanently deletes the inventoried relationships, documents, artifacts and memories (including context checkpoints and inactive memories). Use this only for a disposable dev tenant whose content you intend to remove. The tool authenticates exclusively against `https://api-dev.penfield.app`, checks the specified tenant, and preserves the tenant account and API keys. It walks every artifact folder and every list page before deleting anything. After deletion, the tool reads each inventoried record by ID or artifact path. The `confirmed_404` counts show how many records in each category returned HTTP 404. Exit zero requires all those reads to return 404, empty final inventories, and no unresolved errors. Newly added records remain visible in the final counts and require a new preview.

The command uses `PENFIELD_API_KEY` when set. The key needs read access for preview and delete access to apply. Otherwise it requests a dedicated OAuth grant with read, delete and offline access. It stores that grant separately in `~/.config/penfield-dev-content-reset/tokens.json`. The importer's ordinary login and its `--reset` checkpoint option serve separate purposes.

## License

AGPL-3.0 - see [LICENSE](LICENSE).

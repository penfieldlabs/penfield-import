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

The tool validates the manifest, then processes five phases in the order mandated by the export spec:

1. **Memories** — Creates memories from `memories.jsonl`. Passes through `memory_type`, `importance`, `confidence`, `source_type`, `metadata`, and `tags`. Skips `identity_core` (API does not allow creation). Downgrades `personality_trait` to `fact`.
2. **Relationships** — Remaps source/target IDs to the new tenant's IDs, then bulk-creates relationships in batches of 100 with individual fallback on batch failure.
3. **Contexts** — Recreates saved contexts as `checkpoint` memories via the memories API, remapping all memory references. Handles 409 conflict (duplicate checkpoint name) gracefully.
4. **Artifacts** — Extracts text artifacts from the ZIP and uploads them. Binary files (detected via null bytes) are skipped. Validates path format (no spaces or special chars) and enforces the 1 MB size limit.
5. **Documents** — Extracts document files and uploads them via multipart form with metadata.

Each record is checkpointed individually. Resume by re-running the same command.

### Fields not preserved on round-trip

The following fields are present in the export for archival purposes but cannot be set via the public API: `surprise_score`, `user_id`, `is_evolution`, `evolution_id`, `evolution_type`, `parent_memory_id`, `lifecycle_state`, `created_at`, `updated_at` (memories); `is_auto_detected` (relationships); `tags` (documents).

## Vault import (Obsidian / markdown)

### How vault import works

The tool runs in seven phases, checkpointing after each for crash safety:

1. **Parse** - Reads all `.md`, `.txt`, `.markdown`, and `.text` files, extracts YAML frontmatter and typed relationships
2. **Memories** - Creates one Penfield memory per note (content + tags). Oversized notes get a truncated or summarized memory with a reference to the artifact path.
3. **Artifacts** - Uploads full content for notes exceeding the 10K character memory limit
4. **Exported Artifacts** - Uploads pre-existing artifact files from the `Artifacts/` directory (if present)
5. **Documents** - Uploads document files from the `Documents/` directory (if present) — supports `.pdf`, `.md`, `.txt`, `.epub`, `.json`, `.yaml`, `.yml`, `.py`, `.js`, `.csv`, `.xml`, `.html`, `.htm`
6. **Relationships** - Bulk-creates relationships between memories (batches of 100)
7. **Verify** - Queries Penfield to confirm import counts match

### Important: fresh Penfield knowledge base

This tool **creates** memories and relationships. It does not update or deduplicate against existing data. Run it against a fresh Penfield knowledge base, or expect duplicates.

## Installation

```bash
pip install .
```

This installs the `penfield-import` command. All examples below use it. If you prefer to run without installing, substitute `python penfield_import.py` wherever you see `penfield-import`.

## Quick start

```bash
# Authenticate with Penfield (one-time, opens browser)
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

### OAuth device code flow (recommended)

```bash
# Log in (opens browser for authorization)
python penfield_import.py --login

# Check token status
python penfield_import.py --auth-status

# Force re-authentication
python penfield_import.py --login --reauth

# Log out (clear cached tokens)
python penfield_import.py --logout
```

Tokens are cached at `~/.config/penfield-import/tokens.json` (owner-only permissions) and refreshed automatically when they expire. No manual token management needed.

### API key (legacy)

```bash
export PENFIELD_API_KEY="tm_your_tenant_ak_your_key"
python penfield_import.py /path/to/notes
```

If `PENFIELD_API_KEY` is set, it takes priority over cached OAuth tokens. The API key is exchanged for a short-lived JWT before any requests are made. **The API key is never accepted as a CLI argument** (keys passed as arguments are visible in `ps aux`).

Both auth methods handle token refresh automatically. If a token expires mid-import, it refreshes transparently.

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
- **`type: checkpoint`** — imported as `reference`. The `checkpoint` memory type is the persistence layer for MCP cognitive handoffs created by [`save_context`](https://penfield.app/docs/mcp/mcp-integration.md#save_context); each record stores a JSON `{"checkpoint_name", "description", "memory_ids"}` blob that `restore_context` and `list_contexts` parse at read time. There is no public API path that reproduces that JSON shape — the only legitimate way to create a checkpoint is through `save_context` itself. A checkpoint note in an exported vault has had its JSON unwrapped into a plain description and `references:` wikilinks during export, so re-importing it as a checkpoint would produce a malformed record that is no longer restorable. Downgrading to `reference` preserves its content and `references:` relationships as an ordinary summary memory that is still discoverable via search and graph traversal.

All other frontmatter types (`fact`, `insight`, `conversation`, `correction`, `reference`, `task`, `strategy`, `relationship`) are passed through to Penfield as-is.

### Relationship extraction

Relationships are optional. If your files have no typed frontmatter links, the tool imports everything as memories and artifacts - you still get a fully functional Penfield brain, just without graph edges connecting your notes.

For a much richer import, add typed relationships before importing. Two approaches:

**Option A: obsidian-wikilink-types plugin** - Install [obsidian-wikilink-types](https://github.com/penfieldlabs/obsidian-wikilink-types) in your Obsidian vault and use inline typed wikilinks (`[[Note @supports]]`). The plugin syncs these to YAML frontmatter automatically. See the plugin's [SKILL.md](https://github.com/penfieldlabs/obsidian-wikilink-types/blob/main/SKILL.md) for the full relationship type reference.

**Option B: LLM pre-processing** - Use Claude Code, Cursor, or any LLM with access to your files to analyze your notes and add typed relationship frontmatter before running the import. Point the LLM at the [SKILL.md](https://github.com/penfieldlabs/obsidian-wikilink-types/blob/main/SKILL.md) for the relationship type definitions and ask it to add appropriate frontmatter links. This works on any `.md` or `.txt` collection, not just Obsidian vaults.

**Option C: Inline wikilinks** - If your notes already have `[[wikilinks]]` in the body text (standard Obsidian practice), use `--relationships inline` to import them as `references` relationships. No frontmatter needed.

### Relationship modes (`--relationships`)

| Mode | Description |
|------|-------------|
| `frontmatter` | Extract typed relationships from YAML frontmatter only (default) |
| `inline` | Extract `[[wikilinks]]` from body text as `references` relationships |
| `both` | Frontmatter + inline, deduplicated |

Use `frontmatter` (default) when importing from penfield-export or vaults with typed frontmatter. Use `inline` for human-built Obsidian vaults with no typed frontmatter. Use `both` when your vault has a mix of typed frontmatter and inline wikilinks.

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
2. **An artifact** — the full, unmodified note content stored at `/oversize-notes/<filename>`.

The memory is how you find the note. The artifact is how you read the full thing.

### Default mode (no LLM)

The note is smart-truncated at the nearest section heading or paragraph break (~9,500 chars). A notice with the artifact path is appended so you can retrieve the full document.

### LLM mode (`--llm`)

The full note is sent to an OpenAI-compatible API for summarization. The summary becomes the memory content, with the artifact path appended. Falls back to smart truncation if the LLM fails.

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

Defaults to `haiku` for speed and cost. Any model name accepted by `claude --model` works.

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
| `--reset` | off | Delete checkpoint before starting |
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
| `PENFIELD_API_KEY` | No (alternative to `--login`) | Penfield API key (`tm_<tenant>_ak_<key>`). Takes priority over OAuth if set. |
| `LLM_API_KEY` | Only with `--llm` | OpenAI-compatible API key |

## Checkpoint and crash recovery

The tool writes a checkpoint file after each individual operation. If the import is interrupted, re-run the same command and it picks up where it left off.

- Vault imports use `.penfield_import_checkpoint.json`
- ZIP imports use `.penfield_zip_import_checkpoint.json`
- Checkpoint is written atomically (write to `.tmp`, then `os.replace()`)
- Use `--reset` to delete the checkpoint and start fresh
- Use `--checkpoint-dir` to store the checkpoint somewhere other than the source directory

### Incremental import (git vaults)

If the vault is in a git repository, the tool automatically tracks the commit SHA at import time. On the next run it detects files added since that commit and imports only the new ones — no flags needed.

```bash
# First run: imports everything, records commit SHA
python penfield_import.py /path/to/vault

# Later: add new notes to the vault, commit, then re-run
python penfield_import.py /path/to/vault
# → "Incremental import: 3 new file(s) since commit abc1234..."
```

- Only **new files** (git status `A`) are imported. Modified or deleted files are ignored — use `--reset` for a full re-import if you need to refresh everything.
- The checkpoint stores the full `{rel_path: memory_id}` map from all prior runs, so relationships from new notes to existing notes resolve correctly.
- Non-git vaults are unaffected — they run a full import each time. Re-running without `--reset` exits with "Previous import completed."

## Directory structure

- Recursively finds all `.md` and `.txt` files under the root directory
- Directories named `_meta`, `_templates`, `_penfield`, `_config`, `Artifacts`, and `Documents` are skipped by default (configurable with `--skip-dirs`). `Artifacts/` and `Documents/` have their own dedicated import phases and should not be processed as memories.
- No specific directory structure is required - works with any folder layout
- Files are identified by their relative path (e.g., `concepts/trust.md`), so duplicate filenames in different directories are stored as separate memories
- **Duplicate filename caveat:** Relationship targets in frontmatter use note names without paths (e.g., `[[trust]]`). If multiple files share the same filename in different directories, relationships targeting that name are skipped with a warning (the target is ambiguous). For best results, use unique filenames

### Supported layouts

The tool recognizes the Penfield export layout but works with any subset:

- `Memories/` — markdown notes that become Penfield memories
- `Contexts/` — exported context checkpoints (imported as `reference` memories, see [Memory type mapping](#memory-type-mapping))
- `Artifacts/` — files uploaded to Penfield as artifacts, preserving directory structure as the artifact path
- `Documents/` — files uploaded to Penfield's documents system (PDFs, etc.) for chunked full-text search

A vault may contain any subset of these directories. An `Artifacts/`-only or `Documents/`-only vault is a valid import — the tool skips phases that have no content without aborting.

## Rate limiting

The tool uses a sliding-window rate limiter (200 requests/minute) and handles server responses:

- **429 (rate limited):** Exponential backoff (2^n seconds, max 60s), resets on success
- **401 (unauthorized):** Automatically refreshes the token (OAuth refresh or API key re-exchange)
- **5xx (server error):** Retries up to 3 times with backoff

The LLM client has independent retry logic.

## Requirements

- Python 3.9+
- [PyYAML](https://pypi.org/project/PyYAML/) (installed automatically via `pip install .`)

> **Note:** PyYAML follows the YAML 1.1 spec, which treats `yes`, `no`, `on`, `off` as booleans. If your frontmatter contains these as literal string values, they will be parsed as `True`/`False`. This is standard PyYAML behavior, not a bug in this tool. Use quoted strings (`"yes"`, `"no"`) in your YAML if you need them as literal values.

## License

AGPL-3.0 - see [LICENSE](LICENSE).

# Penfield Import v1.0.0

Import your Obsidian vault, markdown collection, or any folder of `.md` and `.txt` files into [Penfield](https://penfield.app) as searchable memories with a full knowledge graph.

## What it does

- Imports markdown and text files as Penfield memories
- Extracts typed relationships from YAML frontmatter (using the [obsidian-wikilink-types](https://github.com/penfieldlabs/obsidian-wikilink-types) vocabulary) and builds a knowledge graph
- Handles notes over 10,000 characters by creating a searchable summary memory with the full content stored as a retrievable artifact
- Optionally summarizes oversized notes with an LLM (OpenAI-compatible APIs or Claude Code CLI) instead of truncating
- Preserves YAML frontmatter metadata in memory content with `--include-frontmatter`
- Uploads pre-existing artifact files and documents from the vault
- Supports incremental imports for vaults in a git repository — only new files are imported on subsequent runs

## Authentication

Two options:
- **OAuth device code flow** (recommended) — `penfield-import --login` opens a browser for authorization. Tokens are cached and refreshed automatically.
- **API key** — set `PENFIELD_API_KEY` environment variable. The key is exchanged for a short-lived token before any requests.

## Crash safety

Every operation is checkpointed. If the import is interrupted, re-run the same command and it picks up where it left off. No duplicate memories, no lost progress.

## Requirements

- Python 3.9+
- [PyYAML](https://pypi.org/project/PyYAML/) (installed automatically via `pip install .`)

## License

AGPL-3.0

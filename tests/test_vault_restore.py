"""Regression cases at the vault CLI boundary. The destination is an in-memory stub."""
import subprocess
import sys
from unittest import mock

import pytest

import penfield_import as importer


@pytest.fixture
def vault(tmp_path):
    path = tmp_path / "vault"
    path.mkdir()
    return path


def run(monkeypatch, vault, *options):
    monkeypatch.setattr(sys, "argv", ["penfield-import", str(vault), *map(str, options)])
    return importer.main()


@pytest.fixture
def destination(monkeypatch):
    client = mock.Mock(spec=importer.PenfieldClient)
    client.destination_identity.return_value = "verified-destination"
    client.get_memory_count.return_value = 100
    client.create_memory.side_effect = lambda **_: f"memory-{client.create_memory.call_count}"
    client.upload_document_bytes.return_value = {"id": "document-1"}
    monkeypatch.setattr(importer, "_resolve_auth", lambda args: importer._AuthInfo(api_key="local-stub"))
    monkeypatch.setattr(importer, "PenfieldClient", lambda **_: client)
    return client


def test_preview_counts_attachment_only_and_never_mutates(monkeypatch, vault, capsys):
    (vault / "Artifacts").mkdir()
    (vault / "Artifacts" / "info.txt").write_text("info")
    (vault / "Documents").mkdir()
    (vault / "Documents" / "report.pdf").write_bytes(b"%PDF")
    cp = vault / importer.CHECKPOINT_FILENAME
    cp.write_bytes(b"existing checkpoint")
    assert run(monkeypatch, vault, "--reset", "--dry-run") == 0
    assert cp.read_bytes() == b"existing checkpoint"
    output = capsys.readouterr().out
    assert "Artifacts to upload:     1" in output and "Documents to upload:     1" in output
    state = vault.parent / "new-state"
    assert run(monkeypatch, vault, "--checkpoint-dir", state, "--dry-run") == 0
    assert not state.exists()


def test_oversized_paths_and_original_bytes(monkeypatch, vault, destination):
    original = b"---\r\nauthor: Example\r\n---\r\n" + b"a" * 11_000
    for folder in ["first", "second"]:
        (vault / folder).mkdir()
        (vault / folder / "large.md").write_bytes(original + folder.encode())
    assert run(monkeypatch, vault) == 0
    uploads = {path: text.encode() for (path, text), _ in destination.create_artifact.call_args_list}
    assert uploads == {f"/oversize-notes/{name}/large.md": original + name.encode()
                       for name in ["first", "second"]}


def test_rejected_write_resumes_only_missing_records(monkeypatch, vault, destination):
    (vault / "a.md").write_text("a")
    (vault / "b.md").write_text("b")
    destination.create_memory.side_effect = ["accepted", importer.APIError(422, "validation", "/memories")]
    assert run(monkeypatch, vault) == 1
    state = importer.Checkpoint.load(vault / importer.CHECKPOINT_FILENAME)
    assert state.phase != "done" and state.failed_memories == ["b.md"] and state.pending_write is None
    destination.create_memory.reset_mock()
    destination.create_memory.side_effect = ["recovered"]
    assert run(monkeypatch, vault) == 0
    destination.create_memory.assert_called_once_with(content="b", tags=[], memory_type=None)


def test_uncertain_response_requires_reconciliation(monkeypatch, vault, destination):
    (vault / "a.md").write_text("a")
    destination.create_memory.side_effect = importer.APIError(0, "uncertain", "/memories")
    assert run(monkeypatch, vault) == 1
    assert run(monkeypatch, vault) == 1
    destination.create_memory.assert_called_once()
    saved = importer.Checkpoint.load(vault / importer.CHECKPOINT_FILENAME)
    assert saved.pending_write == {"kind": "memory", "keys": ["a.md"]}


def test_changed_target_and_snapshot_cannot_reuse_receipts(monkeypatch, vault, destination):
    note = vault / "a.md"
    note.write_text("before")
    assert run(monkeypatch, vault) == 0
    destination.destination_identity.return_value = "another-destination"
    assert run(monkeypatch, vault) == 1
    destination.destination_identity.return_value = "verified-destination"
    note.write_text("after")
    assert run(monkeypatch, vault) == 1
    destination.create_memory.assert_called_once()


@pytest.mark.parametrize("invalid", [b"\xff", b"---\nmap: [\n---\ntext", b"---\n? [a, b]\n: value\n---\ntext"])
def test_invalid_note_rejected_before_auth(monkeypatch, vault, destination, invalid):
    (vault / "a.md").write_text("valid")
    (vault / "b.md").write_bytes(invalid)
    assert run(monkeypatch, vault) == 1
    destination.destination_identity.assert_not_called()
    assert not (vault / importer.CHECKPOINT_FILENAME).exists()


def test_incremental_unicode_and_document_only(monkeypatch, vault, destination):
    def git(*args):
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
                       cwd=vault, check=True, capture_output=True)
    git("init")
    (vault / "base.md").write_text("base")
    git("add", "base.md")
    git("commit", "-m", "base")
    assert run(monkeypatch, vault) == 0
    (vault / "café.md").write_text("café")
    git("add", "café.md")
    git("commit", "-m", "note")
    assert run(monkeypatch, vault) == 0
    (vault / "Documents").mkdir()
    (vault / "Documents" / "paper.pdf").write_bytes(b"%PDF")
    git("add", "Documents/paper.pdf")
    git("commit", "-m", "document")
    assert run(monkeypatch, vault) == 0
    assert destination.create_memory.call_count == 2
    destination.upload_document_bytes.assert_called_once_with(
        "paper.pdf", b"%PDF", content_type="application/pdf", metadata={"vault_path": "paper.pdf"},
    )


def test_qualified_links_resolve_and_ambiguous_links_block(monkeypatch, vault, destination):
    for name in ("a", "b"):
        (vault / name).mkdir()
        (vault / name / "note.md").write_text(name)
    source = vault / "new.md"
    source.write_text("[[note]]")
    assert run(monkeypatch, vault, "--relationships", "inline") == 1
    destination.create_memory.assert_not_called()
    source.write_text("[[a/note.md#heading]]")
    assert run(monkeypatch, vault, "--relationships", "inline") == 0
    payload = destination.create_relationships_bulk.call_args.args[0]
    assert payload == [{"from_id": "memory-3", "to_id": "memory-1", "relationship_type": "references"}]


def test_yaml_boolean_property_names_keep_their_spelling(vault):
    (vault / "note.md").write_text("---\non: enabled\noff: disabled\n---\ncontent")
    note, = importer.parse_vault(vault, set())
    assert note.frontmatter == {"on": "enabled", "off": "disabled"}


def test_failed_indexing_preserves_upload_and_refreshes_on_retry(monkeypatch, vault, destination):
    (vault / "Documents").mkdir()
    (vault / "Documents" / "paper.pdf").write_bytes(b"%PDF")
    destination.upload_document_bytes.return_value = {"id": "doc-1", "processing_status": "failed"}
    assert run(monkeypatch, vault) != 0
    checkpoint = importer.Checkpoint.load(vault / importer.CHECKPOINT_FILENAME)
    assert checkpoint.documents == {"paper.pdf": "doc-1"}
    assert checkpoint.document_processing == {"paper.pdf": "failed"}
    assert "paper.pdf: failed" in (vault / ".penfield_import_report.txt").read_text()
    destination.document_status.return_value = {"id": "doc-1", "processing_status": "completed"}
    assert run(monkeypatch, vault) == 0
    destination.upload_document_bytes.assert_called_once()
    destination.document_status.assert_called_once_with("doc-1")


def test_empty_vault_exits_before_auth(monkeypatch, vault, destination):
    assert run(monkeypatch, vault) == 0
    destination.destination_identity.assert_not_called()
    assert list(vault.iterdir()) == []


def test_conflicting_oversized_artifact_prevents_pointer_creation(monkeypatch, vault, destination):
    (vault / "big.md").write_text("a" * 11_000)
    (vault / "small.md").write_text("small")
    destination.create_artifact.side_effect = importer.APIError(412, "different", "/artifacts")
    assert run(monkeypatch, vault) != 0
    destination.create_memory.assert_called_once_with(content="small", tags=[], memory_type=None)
    checkpoint = importer.Checkpoint.load(vault / importer.CHECKPOINT_FILENAME)
    assert "big.md" not in checkpoint.memories and "big.md" in checkpoint.failed_artifacts


@pytest.mark.parametrize("category", ["Documents", "Artifacts"])
@pytest.mark.parametrize("mutate_existing", [False, True])
def test_attachment_changes_after_preflight_are_not_uploaded(monkeypatch, vault, destination,
                                                            category, mutate_existing):
    folder = vault / category
    folder.mkdir()
    (folder / "a.txt").write_text("planned")

    def change():
        path = folder / ("a.txt" if mutate_existing else "0.txt")
        path.write_text("unplanned")
        return "verified-destination"

    destination.destination_identity.side_effect = change
    assert run(monkeypatch, vault) != 0
    destination.create_artifact.assert_not_called()
    destination.upload_document_bytes.assert_not_called()


def test_uncertain_vault_write_cannot_be_overwritten(tmp_path):
    path = tmp_path / "checkpoint.json"
    cp = importer.Checkpoint(pending_write={"kind": "memory", "keys": ["a.md"]})
    cp.save(path)
    saved = path.read_bytes()
    with pytest.raises(ValueError, match="unconfirmed write"):
        with cp.writing(path, "memory", ["b.md"]):
            pytest.fail("second write started with an unresolved write pending")
    assert path.read_bytes() == saved


def test_equivalent_unicode_links_share_literal_receipts_on_full_resume_and_incremental(monkeypatch, vault, destination):
    def git(*args):
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
                       cwd=vault, check=True, capture_output=True)
    git("init")
    folder = vault / "cafe\u0301"
    folder.mkdir()
    target = folder / "the\u0301.md"
    target.write_text("target")
    (vault / "source.md").write_text("[[thé]] [[café/thé.md#topic]] [[the\u0301]]")
    git("add", ".")
    git("commit", "-m", "initial notes")
    destination.create_relationships_bulk.side_effect = [importer.APIError(422, "rejected", "/relationships"), None]
    options = ("--relationships", "inline")
    assert run(monkeypatch, vault, *options) == 1
    assert run(monkeypatch, vault, *options) == 0
    state = importer.Checkpoint.load(vault / importer.CHECKPOINT_FILENAME)
    literal = next(key for key in state.memories if "/" in key)
    assert literal == next(folder.glob("*.md")).relative_to(vault).as_posix()
    assert state.relationships_done == {f"source.md|{literal}|references"}
    assert [len(c.args[0]) for c in destination.create_relationships_bulk.call_args_list] == [1, 1]
    destination.create_relationships_bulk.side_effect = None
    added = "ele\u0301ve.md"
    (vault / added).write_text("[[café/thé]]")
    git("add", added)
    git("commit", "-m", "new link to existing target")
    assert run(monkeypatch, vault, *options) == 0
    state = importer.Checkpoint.load(vault / importer.CHECKPOINT_FILENAME)
    added_on_disk = next(vault.glob("ele*.md")).relative_to(vault).as_posix()
    assert state.relationships_done == {f"{name}|{literal}|references" for name in ("source.md", added_on_disk)}
    assert destination.create_memory.call_count == 3
    assert destination.create_relationships_bulk.call_count == 3


def test_canonically_equivalent_stems_are_ambiguous_until_qualified(monkeypatch, vault, destination):
    for folder, spelling in [("a", "the\u0301"), ("b", "thé")]:
        (vault / folder).mkdir()
        (vault / folder / (spelling + ".md")).write_text(folder)
    source = vault / "source.md"
    source.write_text("[[thé]]")
    assert run(monkeypatch, vault, "--relationships", "inline") == 1
    destination.create_memory.assert_not_called()
    source.write_text("[[a/thé]]")
    assert run(monkeypatch, vault, "--relationships", "inline") == 0
    state = importer.Checkpoint.load(vault / importer.CHECKPOINT_FILENAME)
    literal = next(key for key in state.memories if key.startswith("a/"))
    assert state.relationships_done == {f"source.md|{literal}|references"}


def _git_at(root, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
                          cwd=root, check=True, capture_output=True).stdout.decode().strip()


@pytest.mark.parametrize("dirty", ["new.md", "Artifacts/new.txt", "Documents/new.txt"])
def test_incremental_requires_committed_bytes_before_writes(monkeypatch, vault, destination, dirty):
    destination.upload_document_bytes.return_value = {"id": "document-1", "processing_status": "complete"}
    _git_at(vault, "init")
    (vault / "base.md").write_text("base")
    _git_at(vault, "add", "base.md")
    _git_at(vault, "commit", "-qm", "base")
    assert run(monkeypatch, vault) == 0
    cp = vault / importer.CHECKPOINT_FILENAME
    baseline = cp.read_bytes()
    paths = ["new.md", "Artifacts/new.txt", "Documents/new.txt"]
    for name in paths:
        p = vault / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"committed")
    _git_at(vault, "add", "--", *paths)
    _git_at(vault, "commit", "-qm", "additions")
    (vault / dirty).write_bytes(b"private working copy")
    _git_at(vault, "update-index", "--assume-unchanged", "--", dirty)
    assert run(monkeypatch, vault) == 1
    assert cp.read_bytes() == baseline
    assert destination.create_memory.call_count == 1
    destination.create_artifact.assert_not_called()
    destination.upload_document_bytes.assert_not_called()
    (vault / dirty).write_bytes(b"committed")
    assert run(monkeypatch, vault) == 0
    assert run(monkeypatch, vault) == 0
    assert destination.create_memory.call_count == 2
    assert destination.create_memory.call_args.kwargs["content"] == "committed"
    destination.create_artifact.assert_called_once_with("/new.txt", "committed")
    assert destination.upload_document_bytes.call_count == 1
    assert destination.upload_document_bytes.call_args.args == ("new.txt", b"committed")


def test_incremental_checkout_eol_and_unicode_in_subdirectory(monkeypatch, vault, destination):
    _git_at(vault.parent, "init")
    (vault.parent / ".gitattributes").write_text("*.md text eol=crlf\n")
    (vault / "base.md").write_bytes(b"base\r\n")
    _git_at(vault.parent, "add", ".gitattributes", "vault/base.md")
    _git_at(vault.parent, "commit", "-qm", "base")
    assert run(monkeypatch, vault) == 0
    p = vault / "intro - cafe\u0301.md"
    p.write_bytes(b"clean\r\n")
    _git_at(vault.parent, "add", "--", str(p.relative_to(vault.parent)))
    _git_at(vault.parent, "commit", "-qm", "new note")
    assert run(monkeypatch, vault) == 0
    assert run(monkeypatch, vault) == 0
    assert destination.create_memory.call_count == 2


def test_incremental_retry_keeps_verified_commit_when_head_moves(monkeypatch, vault, destination):
    _git_at(vault, "init")
    (vault / "base.md").write_text("base")
    _git_at(vault, "add", "base.md")
    _git_at(vault, "commit", "-qm", "base")
    assert run(monkeypatch, vault) == 0
    (vault / "new.md").write_text("new")
    _git_at(vault, "add", "new.md")
    _git_at(vault, "commit", "-qm", "new")
    pinned = _git_at(vault, "rev-parse", "HEAD")

    def advance_head_and_reject(**_):
        _git_at(vault, "commit", "--allow-empty", "-qm", "concurrent update")
        raise importer.APIError(422, "rejected", "/memories")

    destination.create_memory.side_effect = advance_head_and_reject
    assert run(monkeypatch, vault) == 1
    assert importer.Checkpoint.load(vault / importer.CHECKPOINT_FILENAME).commit_sha == pinned
    destination.create_memory.side_effect = None
    destination.create_memory.return_value = "recovered"
    assert run(monkeypatch, vault) == 0
    assert importer.Checkpoint.load(vault / importer.CHECKPOINT_FILENAME).commit_sha == pinned


def test_local_headings_preserve_text_and_only_post_cross_note_edge(monkeypatch, vault, destination, capsys):
    source = "## Heading\n[[#Heading]] [[#^block|there]] [[index#Heading]] [[other#Heading]]"
    (vault / "index.md").write_text(source)
    (vault / "other.md").write_text("## Heading")
    opts = ("--relationships", "inline")
    assert run(monkeypatch, vault, *opts, "--dry-run") == 0
    assert "Relationship batches:    1 (1 total)" in capsys.readouterr().out
    assert run(monkeypatch, vault, *opts) == 0
    assert run(monkeypatch, vault, *opts) == 0
    assert destination.create_memory.call_count == 2
    assert destination.create_memory.call_args_list[0].kwargs["content"] == source
    destination.create_relationships_bulk.assert_called_once_with(
        [{"from_id": "memory-1", "to_id": "memory-2", "relationship_type": "references"}])
    assert importer.Checkpoint.load(vault / importer.CHECKPOINT_FILENAME).relationships_done == {"index.md|other.md|references"}


def test_typed_local_heading_and_rejected_legacy_self_edge_can_resume(monkeypatch, vault, destination):
    (vault / "nested").mkdir()
    (vault / "nested" / "index.md").write_text(
        '---\nsupports: ["[[#here]]", "[[index#here]]", "[[nested/index.md#^block]]"]\n---\n## here')
    assert run(monkeypatch, vault) == 0
    path = vault / importer.CHECKPOINT_FILENAME
    state = importer.Checkpoint.load(path)
    state.phase = "memories"
    state.failed_relationships = ["nested/index.md|nested/index.md|supports"]
    state.save(path)
    assert run(monkeypatch, vault) == 0
    assert importer.Checkpoint.load(path).failed_relationships == []
    destination.create_memory.assert_called_once()
    destination.create_relationships_bulk.assert_not_called()


def test_explicit_typed_bare_self_reference_fails_before_writes(monkeypatch, vault, destination, caplog):
    (vault / "index.md").write_text('---\nsupports: ["[[index]]"]\n---\nBody')
    assert run(monkeypatch, vault) == 1
    assert "self-relationships are unsupported" in caplog.text
    destination.create_memory.assert_not_called()
    destination.create_relationships_bulk.assert_not_called()


def test_incremental_edits_after_preflight_never_send_dirty_bytes(monkeypatch, vault, destination):
    _git_at(vault, "init")
    (vault / "base.md").write_text("base")
    _git_at(vault, "add", "base.md")
    _git_at(vault, "commit", "-qm", "base")
    assert run(monkeypatch, vault) == 0
    paths = [vault / "new.md", vault / "Artifacts" / "new.txt", vault / "Documents" / "new.txt"]
    for p in paths:
        p.parent.mkdir(exist_ok=True)
        p.write_text("committed")
    _git_at(vault, "add", "--", *(str(p.relative_to(vault)) for p in paths))
    _git_at(vault, "commit", "-qm", "add")
    def edit_during_auth():
        for p in paths:
            p.write_text("private working copy")
        return "verified-destination"
    destination.destination_identity.side_effect = edit_during_auth
    assert run(monkeypatch, vault) == 1
    # Notes are immutable preflight content; attachments are rechecked when read.
    assert destination.create_memory.call_args.kwargs["content"] == "committed"
    destination.create_artifact.assert_not_called()
    destination.upload_document_bytes.assert_not_called()


def test_incremental_never_uses_a_smudge_filter_to_authorize_uncommitted_content(monkeypatch, vault, destination):
    _git_at(vault, "init")
    _git_at(vault, "config", "filter.reviewer.clean", "cat")
    _git_at(vault, "config", "filter.reviewer.smudge", "touch filter-ran && cat private-bytes")
    (vault / ".gitattributes").write_text("new.md text eol=crlf filter=reviewer\n")
    (vault / "base.md").write_text("base")
    _git_at(vault, "add", "base.md", ".gitattributes")
    _git_at(vault, "commit", "-qm", "base")
    assert run(monkeypatch, vault) == 0
    (vault / "new.md").write_bytes(b"committed input\n")
    _git_at(vault, "add", "new.md")
    _git_at(vault, "commit", "-qm", "new")
    (vault / "private-bytes").write_text("private content")
    (vault / "new.md").write_text("private content")
    assert not (vault / "filter-ran").exists()
    cp = vault / importer.CHECKPOINT_FILENAME
    saved = cp.read_bytes()
    assert run(monkeypatch, vault) == 1
    assert not (vault / "filter-ran").exists()
    assert cp.read_bytes() == saved
    destination.create_memory.assert_called_once()
    (vault / "new.md").write_bytes(b"committed input\r\n")
    assert run(monkeypatch, vault) == 0
    assert not (vault / "filter-ran").exists()


def test_single_details_heading_preview_restore_and_repeat(monkeypatch, vault, destination, capsys):
    text = "## Details\n[[#Details]]"
    (vault / "single.md").write_text(text)
    opts = ("--relationships", "inline")
    assert run(monkeypatch, vault, *opts, "--dry-run") == 0
    preview = capsys.readouterr().out
    assert "Relationships:     0" in preview and "Relationship batches:    0 (0 total)" in preview
    destination.create_memory.assert_not_called()
    assert run(monkeypatch, vault, *opts) == 0
    assert run(monkeypatch, vault, *opts) == 0
    destination.create_memory.assert_called_once_with(content=text, tags=[], memory_type=None)
    destination.create_relationships_bulk.assert_not_called()


def test_legacy_unconfirmed_heading_write_stops_before_receipt_migration(monkeypatch, vault, destination, caplog):
    (vault / "index.md").write_text("## Details\n[[#Details]]")
    opts = ("--relationships", "inline")
    assert run(monkeypatch, vault, *opts) == 0
    path = vault / importer.CHECKPOINT_FILENAME
    state = importer.Checkpoint.load(path)
    key = "index.md|index.md|references"
    state.phase = "relationships"
    state.failed_relationships = [key]
    state.pending_write = {"kind": "relationships", "keys": [key]}
    state.save(path)
    before = path.read_bytes()
    assert run(monkeypatch, vault, *opts) == 1
    assert "unconfirmed write" in caplog.text and path.read_bytes() == before
    destination.create_memory.assert_called_once()
    destination.create_relationships_bulk.assert_not_called()


@pytest.mark.parametrize("missing", ["two.md", "Artifacts/two.txt", "Documents/two.csv"])
@pytest.mark.parametrize("sibling", [False, True])
def test_missing_committed_addition_blocks_before_writes(monkeypatch, vault, destination, missing, sibling):
    destination.upload_document_bytes.return_value = {"id": "doc", "processing_status": "completed"}
    _git_at(vault, "init")
    (vault / "base.md").write_text("base")
    _git_at(vault, "add", "base.md")
    _git_at(vault, "commit", "-qm", "base")
    assert run(monkeypatch, vault) == 0
    cp = vault / importer.CHECKPOINT_FILENAME
    baseline = cp.read_bytes()
    path = vault / missing
    path.parent.mkdir(exist_ok=True)
    path.write_text("two")
    _git_at(vault, "add", missing)
    if sibling:
        (vault / "one.md").write_text("one")
        _git_at(vault, "add", "one.md")
    _git_at(vault, "commit", "-qm", "addition")
    path.unlink()
    assert run(monkeypatch, vault) == 1
    assert cp.read_bytes() == baseline
    assert destination.create_memory.call_count == 1
    destination.create_artifact.assert_not_called()
    destination.upload_document_bytes.assert_not_called()
    _git_at(vault, "restore", "--", missing)
    assert run(monkeypatch, vault) == 0
    assert run(monkeypatch, vault) == 0
    assert missing in importer.Checkpoint.load(cp).source_hashes


def test_artifact_changed_after_preflight_and_legacy_binary_receipt(monkeypatch, vault, destination):
    path = vault / "Artifacts" / "a.txt"
    path.parent.mkdir()
    path.write_bytes(b"original")
    cp = vault / importer.CHECKPOINT_FILENAME
    original_phase = importer.run_vault_artifacts_phase

    def replace(*args):
        path.write_bytes(b"\0binary")
        original_phase(*args)

    monkeypatch.setattr(importer, "run_vault_artifacts_phase", replace)
    assert run(monkeypatch, vault) == 1
    assert not importer.Checkpoint.load(cp).vault_artifacts
    destination.create_artifact.assert_not_called()
    path.write_bytes(b"original")
    monkeypatch.setattr(importer, "run_vault_artifacts_phase", original_phase)
    legacy = importer.Checkpoint.load(cp)
    legacy.phase = "done"
    legacy.vault_artifacts["a.txt"] = importer.SKIP_BINARY
    legacy.save(cp)
    assert run(monkeypatch, vault) == 0
    assert run(monkeypatch, vault) == 0
    destination.create_artifact.assert_called_once_with("/a.txt", "original")
    assert importer.Checkpoint.load(cp).vault_artifacts == {"a.txt": "/a.txt"}


def test_markdown_code_examples_do_not_create_or_block_edges(monkeypatch, vault, destination):
    source = ("[[real]]\n\n```txt\n[[example]]\n```\n\n> ~~~\n> [[unknown]]\n> ~~~\n\n"
              "Use ``a ` [[unknown]]`` and `x\n[[example]]`.\n")
    for name, content in [("guide", source), ("real", "target"), ("example", "example")]:
        (vault / (name + ".md")).write_text(content)
    options = ("--relationships", "inline")
    assert run(monkeypatch, vault, *options, "--dry-run") == 0
    assert run(monkeypatch, vault, *options) == 0
    assert run(monkeypatch, vault, *options) == 0
    assert importer.Checkpoint.load(vault / importer.CHECKPOINT_FILENAME).relationships_done == {"guide.md|real.md|references"}
    assert destination.create_memory.call_args_list[1].kwargs["content"] == source


def test_upgrade_recovers_file_omitted_from_completed_git_checkpoint(monkeypatch, vault, destination):
    _git_at(vault, "init")
    (vault / "base.md").write_text("base")
    _git_at(vault, "add", "base.md")
    _git_at(vault, "commit", "-qm", "base")
    assert run(monkeypatch, vault) == 0
    cp = vault / importer.CHECKPOINT_FILENAME
    missed = vault / "missed.md"
    missed.write_text("committed")
    _git_at(vault, "add", "missed.md")
    _git_at(vault, "commit", "-qm", "missed")
    old = importer.Checkpoint.load(cp)
    old.git_inventory_checked = False
    old.commit_sha = _git_at(vault, "rev-parse", "HEAD")
    import json
    legacy = old.to_dict()
    legacy.pop("git_baseline_version")
    legacy["git_inventory_checked"] = True  # R3 set this even when the first import omitted a tracked file.
    cp.write_text(json.dumps(legacy))
    missed.unlink()
    assert run(monkeypatch, vault) == 1
    _git_at(vault, "restore", "missed.md")
    assert run(monkeypatch, vault) == 0
    assert run(monkeypatch, vault) == 0
    assert destination.create_memory.call_count == 2
    assert importer.Checkpoint.load(cp).memories.keys() == {"base.md", "missed.md"}


@pytest.mark.parametrize("missing", ["note.md", "Artifacts/log.txt", "Documents/terms.csv"])
@pytest.mark.parametrize("sibling", [True, False])
def test_first_git_snapshot_requires_every_committed_input(monkeypatch, vault, destination, missing, sibling):
    destination.upload_document_bytes.return_value = {"id": "D", "processing_status": "completed"}
    _git_at(vault, "init")
    file = vault / missing
    file.parent.mkdir(exist_ok=True)
    file.write_text("committed")
    if sibling:
        (vault / "visible.md").write_text("visible")
    _git_at(vault, "add", ".")
    _git_at(vault, "commit", "-qm", "baseline")
    file.unlink()
    for opts in [("--dry-run",), (), ()]:
        assert run(monkeypatch, vault, *opts) == 1
    assert not (vault / importer.CHECKPOINT_FILENAME).exists()
    destination.create_memory.assert_not_called()
    destination.create_artifact.assert_not_called()
    destination.upload_document_bytes.assert_not_called()
    _git_at(vault, "restore", "--", missing)
    assert run(monkeypatch, vault) == 0
    assert run(monkeypatch, vault) == 0
    calls = (destination.create_memory.call_count + destination.create_artifact.call_count
             + destination.upload_document_bytes.call_count)
    assert calls == 1 + sibling


def test_vault_duplicate_note_id_keeps_pending_write(monkeypatch, vault, destination):
    for name in ("a.md", "b.md"):
        (vault / name).write_text("same")
    destination.create_memory.side_effect = ["shared", "shared"]
    assert run(monkeypatch, vault) == 1
    assert run(monkeypatch, vault) == 1
    cp = importer.Checkpoint.load(vault / importer.CHECKPOINT_FILENAME)
    assert cp.memories == {"a.md": "shared"} and cp.pending_write["keys"] == ["b.md"]
    assert destination.create_memory.call_count == 2
    with pytest.raises(ValueError, match="repeated memory destination"):
        importer.Checkpoint.from_dict({"memories": {"a.md": "shared", "b.md": "shared"}})

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

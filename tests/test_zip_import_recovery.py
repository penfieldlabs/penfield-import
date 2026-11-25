import json
import zipfile
from unittest.mock import Mock

import pytest

import penfield_import as importer


def export(tmp_path, filename="export.zip", **sections):
    path = tmp_path / filename
    records = {name: sections.get(name, []) for name in importer.ZIP_SECTIONS}
    manifest = {
        "schema_version": "1.0.0", "source_tenant_id": "source",
        "counts": {name: len(rows) for name, rows in records.items()},
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for name, rows in records.items():
            zf.writestr(name + ".jsonl", "\n".join(json.dumps(row) for row in rows))
    return path


def client():
    result = Mock(spec=importer.PenfieldClient)
    result.destination_identity.return_value = "test-target"
    result.create_memory.side_effect = lambda **kwargs: "dest-" + kwargs["content"]
    return result


@pytest.mark.parametrize("section,rows", [
    ("documents", [{"id": "d", "filename": "absent.pdf"}]),
    ("contexts", [{"id": "m", "name": "overlap"}]),
    ("contexts", [{"id": "c", "name": "n", "description": []}]),
    ("relationships", [{"id": "r", "from_id": "m"}]),
    ("relationships", [[]]),
    ("artifacts", [{"path": "/absent.txt"}]),
])
def test_invalid_late_record_precedes_auth_and_reset(tmp_path, section, rows):
    archive = export(tmp_path, memories=[{"id": "m", "content": "ok"}], **{section: rows})
    cp = tmp_path / importer.ZIP_CHECKPOINT_FILENAME
    cp.write_bytes(b"prior-state")
    factory = Mock()
    assert importer.run_zip_import(archive, None, reset=True, client_factory=factory) == 1
    assert cp.read_bytes() == b"prior-state"
    factory.assert_not_called()


def test_preview_with_reset_leaves_checkpoint_and_directory_untouched(tmp_path):
    archive = export(tmp_path)
    cp = tmp_path / importer.ZIP_CHECKPOINT_FILENAME
    cp.write_bytes(b"prior-state")
    assert importer.run_zip_import(archive, None, dry_run=True, reset=True) == 0
    assert cp.read_bytes() == b"prior-state"
    absent = tmp_path / "other"
    assert importer.run_zip_import(archive, None, absent, dry_run=True, reset=True) == 0
    assert not absent.exists()


def test_deferred_reference_retried_without_duplicate_memory(tmp_path):
    archive = export(tmp_path, memories=[{"id": "a", "content": "one"}, {"id": "b", "content": "two"}],
                     relationships=[{"id": "r", "from_id": "a", "to_id": "b", "relationship_type": "references"}])
    api = client()
    api.create_memory.side_effect = [importer.APIError(422, "reject", "/memories"), "b1", "a1"]
    assert importer.run_zip_import(archive, api) == 2
    api.create_relationships_bulk.assert_not_called()
    cp = importer.ZipCheckpoint.load(tmp_path / importer.ZIP_CHECKPOINT_FILENAME)
    assert cp.failed_memories == ["a"]
    assert cp.skipped_relationships.keys() == {"r"}
    assert not cp.relationships_done
    assert importer.run_zip_import(archive, api) == 0
    assert api.create_memory.call_count == 3
    args = api.create_relationships_bulk.call_args.args[0]
    assert (args[0]["from_id"], args[0]["to_id"]) == ("a1", "b1")
    assert importer.run_zip_import(archive, api) == 0
    assert api.create_relationships_bulk.call_count == 1


@pytest.mark.parametrize("error", [OSError("reply lost"), importer.APIError(503, "unknown", "/memories")])
def test_uncertain_write_is_durable_and_never_replayed(tmp_path, error):
    archive = export(tmp_path, memories=[{"id": "a", "content": "a"}])
    api = client()
    api.create_memory.side_effect = error
    assert importer.run_zip_import(archive, api) == 1
    cp_path = tmp_path / importer.ZIP_CHECKPOINT_FILENAME
    saved = cp_path.read_bytes()
    assert json.loads(saved)["in_flight"]["source_keys"] == ["a"]
    assert importer.run_zip_import(archive, api) == 1
    assert cp_path.read_bytes() == saved
    assert api.create_memory.call_count == 1


def test_checkpoint_is_bound_to_archive_and_destination(tmp_path):
    first = export(tmp_path, memories=[{"id": "a", "content": "a"}])
    second = export(tmp_path, "other.zip", memories=[{"id": "b", "content": "b"}])
    api = client()
    assert importer.run_zip_import(first, api) == 0
    cp = tmp_path / importer.ZIP_CHECKPOINT_FILENAME
    initial = cp.read_bytes()
    assert importer.run_zip_import(second, api) == 1
    api.destination_identity.return_value = "another-target"
    assert importer.run_zip_import(first, api) == 1
    assert cp.read_bytes() == initial
    assert api.create_memory.call_count == 1


def test_legacy_or_busy_checkpoint_never_automatically_replays(tmp_path):
    archive = export(tmp_path, memories=[{"id": "a", "content": "a"}])
    api = client()
    cp = tmp_path / importer.ZIP_CHECKPOINT_FILENAME
    legacy = b'{"phase":"done","memory_id_map":{"a":"old"}}'
    cp.write_bytes(legacy)
    assert importer.run_zip_import(archive, api) == 1
    with importer.checkpoint_lock(cp):
        assert importer.run_zip_import(archive, api, reset=True) == 1
    assert cp.read_bytes() == legacy
    api.create_memory.assert_not_called()


def test_semantically_rejected_edge_does_not_discard_the_rest(tmp_path):
    archive = export(tmp_path, memories=[{"id": "a", "content": "a"}, {"id": "b", "content": "b"}],
                     relationships=[
                         {"id": "bad", "from_id": "a", "to_id": "b", "relationship_type": "bad"},
                         {"id": "good", "from_id": "b", "to_id": "a", "relationship_type": "supports"}])
    api = client()
    def reject_invalid(edges):
        if any(edge["relationship_type"] == "bad" for edge in edges):
            raise importer.APIError(422, "reject", "/relationships/bulk")
        return {}
    api.create_relationships_bulk.side_effect = reject_invalid
    assert importer.run_zip_import(archive, api) == 2
    cp = importer.ZipCheckpoint.load(tmp_path / importer.ZIP_CHECKPOINT_FILENAME)
    assert cp.failed_relationships == ["bad"]
    assert cp.relationships_done == {"good"}
    assert not cp.in_flight


@pytest.mark.parametrize("field,value", [
    ("memory_type", "unknown"), ("source_type", "unknown"),
    ("content", "x" * 10001), ("confidence", float("nan")), ("importance", 1.01),
    ("tags", ["x"] * 11), ("tags", ["!" * 10]), ("tags", ["a" * 51]),
])
def test_known_api_bounds_checked_before_first_write(tmp_path, field, value):
    second = {"id": "b", "content": "two", field: value}
    archive = export(tmp_path, memories=[{"id": "a", "content": "one"}, second])
    api = client()
    assert importer.run_zip_import(archive, api) == 1
    api.destination_identity.assert_not_called()
    assert not (tmp_path / importer.ZIP_CHECKPOINT_FILENAME).exists()


def test_unknown_key_in_completed_checkpoint_preserved_for_review(tmp_path):
    archive = export(tmp_path, memories=[{"id": "m", "content": "one"}])
    api = client()
    assert importer.run_zip_import(archive, api) == 0
    path = tmp_path / importer.ZIP_CHECKPOINT_FILENAME
    altered = json.loads(path.read_bytes())
    altered["memory_id_map"] = {"other": "foreign-id"}
    path.write_text(json.dumps(altered))
    original = path.read_bytes()
    assert importer.run_zip_import(archive, api) == 1
    assert path.read_bytes() == original
    assert api.create_memory.call_count == 1


@pytest.mark.parametrize("declared", [{"file_size": 4}, {"sha256": "0" * 64}])
def test_document_size_or_hash_disagreement_stops_preflight(tmp_path, declared):
    archive = export(tmp_path, documents=[{"id": "d", "filename": "sample.pdf", **declared}])
    with zipfile.ZipFile(archive, "a") as blob:
        blob.writestr("documents/d/original.pdf", b"pdf")
    api = client()
    assert importer.run_zip_import(archive, api) == 1
    api.destination_identity.assert_not_called()


def test_bounded_record_read_precedes_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "_MAX_ZIP_RECORD_BYTES", 500)
    archive = export(tmp_path, memories=[{"id": "large", "content": "x" * 550}])
    api = client()
    assert importer.run_zip_import(archive, api) == 1
    api.destination_identity.assert_not_called()


def test_crash_after_success_before_receipt_is_stored_blocks_replay(tmp_path, monkeypatch):
    archive = export(tmp_path, memories=[{"id": "m", "content": "one"}])
    api = client()
    save = importer.ZipCheckpoint.save

    def disk_failure(checkpoint, path):
        if checkpoint.memory_id_map:
            raise OSError("simulated disk write failure before replacing old checkpoint")
        save(checkpoint, path)

    with monkeypatch.context() as patch:
        patch.setattr(importer.ZipCheckpoint, "save", disk_failure)
        assert importer.run_zip_import(archive, api) == 1
    assert api.create_memory.call_count == 1
    cp = importer.ZipCheckpoint.load(tmp_path / importer.ZIP_CHECKPOINT_FILENAME)
    assert cp.in_flight["source_keys"] == ["m"]
    assert cp.memory_id_map == {}
    assert importer.run_zip_import(archive, api) == 1
    assert api.create_memory.call_count == 1


def test_expanded_context_checked_before_creating_any_memory(tmp_path):
    archive = export(tmp_path, memories=[{"id": "m", "content": "one"}],
                     contexts=[{"id": "c", "name": "context", "memory_ids": ["m"] * 130}])
    api = client()
    assert importer.run_zip_import(archive, api) == 1
    api.destination_identity.assert_not_called()

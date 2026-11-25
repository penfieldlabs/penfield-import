import io
import json
from unittest.mock import Mock
import zipfile

import pytest

import penfield_import as importer


def context(source_id, members, **fields):
    return {"id": source_id, "name": source_id, "memory_ids": members, **fields}


def make_zip(**tables):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name in {"contexts", "memories", "documents"} | tables.keys():
            rows = tables.get(name, [])
            archive.writestr(name + ".jsonl", "\n".join(map(json.dumps, rows)))
        for row in tables.get("documents", []):
            archive.writestr(f"documents/{row['id']}/original.txt", b"fixture")
    return zipfile.ZipFile(data)


def test_forward_context_dependency_and_partial_membership_survive_resume(tmp_path):
    source_id = "11111111-1111-1111-1111-111111111111"
    destination_id = "22222222-2222-2222-2222-222222222222"
    child = context("child", ["parent", source_id], description=f"see memory_id: {source_id}")
    parent = context("parent", [source_id, "absent"], checkpoint_metadata={"category": "audit"})
    cp = importer.ZipCheckpoint(memory_id_map={source_id: destination_id})
    api = Mock()
    api.create_memory.side_effect = ["new-parent", "new-child"]
    path = tmp_path / "checkpoint.json"
    with make_zip(memories=[{"id": source_id}], contexts=[child, parent]) as archive:
        importer.run_zip_contexts_phase(archive, api, cp, path)
        importer.run_zip_contexts_phase(archive, api, importer.ZipCheckpoint.load(path), path)
    assert api.create_memory.call_count == 2
    first, second = [call.kwargs for call in api.create_memory.call_args_list]
    assert json.loads(first["content"])["memory_ids"] == [destination_id]
    assert first["metadata"] == {"category": "audit"}
    child_payload = json.loads(second["content"])
    assert child_payload["memory_ids"] == ["new-parent", destination_id]
    assert child_payload["description"] == f"see memory_id: {destination_id}"
    assert cp.context_unresolved == {"parent": ["absent"]}
    assert cp.failed_contexts == ["parent"]


def test_cycle_and_dependent_row_are_reported_without_writing_empty_contexts(tmp_path):
    cp = importer.ZipCheckpoint()
    api = Mock()
    api.create_memory.return_value = "independent-id"
    records = [
        context("a", ["b"]), context("b", ["a"]),
        context("c", ["a"]), context("independent", []),
    ]
    with make_zip(contexts=records) as archive:
        importer.run_zip_contexts_phase(archive, api, cp, tmp_path / "cp")
    assert api.create_memory.call_count == 1
    assert cp.contexts == {"independent": "independent-id"}
    assert set(cp.failed_contexts) == {"a", "b", "c"}
    assert cp.context_unresolved == {"a": ["b"], "b": ["a"], "c": ["a"]}


def test_retryable_source_memory_failure_defers_context(tmp_path):
    cp = importer.ZipCheckpoint(memory_id_map={"one": "ONE"})
    api = Mock()
    api.create_memory.return_value = "ctx"
    path = tmp_path / "cp"
    with make_zip(memories=[{"id": "one"}, {"id": "two"}],
                  contexts=[context("context", ["one", "two"])]) as archive:
        importer.run_zip_contexts_phase(archive, api, cp, path)
        api.create_memory.assert_not_called()
        cp.memory_id_map["two"] = "TWO"
        importer.run_zip_contexts_phase(archive, api, cp, path)
    assert json.loads(api.create_memory.call_args.kwargs["content"])["memory_ids"] == ["ONE", "TWO"]
    assert cp.failed_contexts == []
    assert cp.context_unresolved == {}


@pytest.mark.parametrize(("name", "expected"), [
    ("Résumé: A_B", ["context", "checkpoint", "rsum-a-b"]),
    ("x" * 51, ["context", "checkpoint"]),
    ("Context", ["context", "checkpoint"]),
    ("Example - 2026", ["context", "checkpoint", "example-2026"]),
])
def test_context_name_remains_exact_when_derived_tag_is_normalized_or_omitted(tmp_path, name, expected):
    cp = importer.ZipCheckpoint()
    api = Mock()
    api.create_memory.return_value = "new-context"
    with make_zip(contexts=[context("ctx", [], name=name)]) as archive:
        importer.run_zip_contexts_phase(archive, api, cp, tmp_path / "cp")
    payload = api.create_memory.call_args.kwargs
    assert payload["tags"] == expected
    assert json.loads(payload["content"])["checkpoint_name"] == name
    assert payload["metadata"] is None


@pytest.mark.parametrize("receipt", [{}, {"id": None}, {"id": ""}])
def test_document_without_id_is_an_ambiguous_result(tmp_path, receipt):
    cp = importer.ZipCheckpoint()
    api = Mock()
    api.upload_document_bytes.return_value = receipt
    with make_zip(documents=[{"id": "d", "filename": "report.txt"}]) as archive:
        with pytest.raises(importer.APIError) as raised:
            importer.run_zip_documents_phase(archive, api, cp, tmp_path / "cp")
    assert raised.value.status_code == 0
    assert not cp.documents


def test_max_length_trait_downgrade_retains_entire_text(tmp_path):
    content = "a" * importer.MEMORY_CONTENT_LIMIT
    cp = importer.ZipCheckpoint()
    api = Mock()
    api.create_memory.return_value = "new-id"
    with make_zip(memories=[{"id": "trait", "content": content, "memory_type": "personality_trait"}]) as archive:
        importer.run_zip_memories_phase(archive, api, cp, tmp_path / "cp")
    assert api.create_memory.call_args.kwargs["content"] == content
    assert api.create_memory.call_args.kwargs["memory_type"] == "fact"


def test_relationship_evidence_reaches_write_payload(tmp_path):
    cp = importer.ZipCheckpoint(memory_id_map={"a": "new-a", "b": "new-b"})
    api = Mock()
    evidence = "Source passage supporting this edge."
    with make_zip(relationships=[{
        "id": "r", "from_id": "a", "to_id": "b", "relationship_type": "supports", "evidence": evidence,
    }]) as archive:
        importer.run_zip_relationships_phase(archive, api, cp, tmp_path / "cp")
    assert api.create_relationships_bulk.call_args.args[0][0]["evidence"] == evidence


def test_failed_document_processing_refreshes_status_without_uploading_again(tmp_path):
    cp = importer.ZipCheckpoint()
    api = Mock()
    api.upload_document_bytes.return_value = {"id": "stored", "processing_status": "failed"}
    api.document_status.return_value = {"id": "stored", "processing_status": "ready", "embedding_progress": 1.0}
    path = tmp_path / "cp"
    with make_zip(documents=[{"id": "d", "filename": "report.txt"}]) as archive:
        importer.run_zip_documents_phase(archive, api, cp, path)
        assert cp.failed_documents == ["d"]
        assert cp.documents == {"d": "stored"}
        cp = importer.ZipCheckpoint.load(path)
        importer.run_zip_documents_phase(archive, api, cp, path)
        importer.run_zip_documents_phase(archive, api, cp, path)
    assert not cp.failed_documents
    api.upload_document_bytes.assert_called_once()
    api.document_status.assert_called_once_with("stored")
    assert cp.document_processing["d"] == {"processing_status": "ready", "embedding_progress": 1.0}


@pytest.mark.parametrize("changed_field", [None, "metadata", "checkpoint_name", "memory_count"])
def test_context_readback_checks_source_metadata_and_serialized_name_and_count(tmp_path, changed_field):
    source_metadata = {"fixture": "source", "checkpoint_name": "independent source value", "memory_count": 99}
    record = context("ctx", [], name="restored name", checkpoint_metadata=source_metadata)
    received = {}
    api = importer.PenfieldClient(access_token="offline-test-token")

    def request(method, path, body=None):
        if method == "POST":
            assert path == "/memories"
            assert body["metadata"] == source_metadata
            received.update(body)
            content = json.loads(body["content"])
            assert (content["checkpoint_name"], content["memory_count"]) == ("restored name", 0)
            return {"data": {"id": "destination"}}
        assert (method, path) == ("GET", "/memories/destination")
        stored = dict(received, id="destination")
        if changed_field == "metadata":
            stored["metadata"] = {"fixture": "source"}
        elif changed_field:
            content = json.loads(stored["content"])
            content[changed_field] = "changed"
            stored["content"] = json.dumps(content)
        return {"data": stored}

    api.request = request
    cp = importer.ZipCheckpoint()
    with make_zip(contexts=[record]) as archive:
        if changed_field:
            with pytest.raises(importer.APIError) as error:
                importer.run_zip_contexts_phase(archive, api, cp, tmp_path / "cp")
            assert error.value.status_code == 0
            assert "readback differs" in error.value.body
            assert "ctx" not in cp.contexts
        else:
            importer.run_zip_contexts_phase(archive, api, cp, tmp_path / "cp")
            assert cp.contexts == {"ctx": "destination"}

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


class ContextDestination:
    def __init__(self):
        self.rows = {}

    def destination_identity(self):
        return "context-test"

    def create_memory(self, **fields):
        import uuid
        dest = str(uuid.UUID(int=100 + len(self.rows)))
        self.rows[dest] = dict(fields, id=dest, metadata=fields.get("metadata") or {})
        return dest

    def _readback(self, path):
        return self.rows[path.rsplit("/", 1)[-1]]

    def request(self, method, path):
        assert method == "GET"
        return {"data": self._readback(path)}


def write_archive(tmp_path, **rows):
    file = tmp_path / "references.zip"
    with zipfile.ZipFile(file, "w") as archive:
        counts = {section: len(rows.get(section, [])) for section in importer.ZIP_SECTIONS}
        archive.writestr("manifest.json", json.dumps({
            "schema_version": "1.0", "source_tenant_id": "test", "counts": counts,
        }))
        for section in counts:
            # Ensure diagnostics refer to physical source lines, even with a blank line.
            records = "\n" + "\n".join(map(json.dumps, rows.get(section, [])))
            archive.writestr(section + ".jsonl", records)
    return file


def description_archive(tmp_path, dependencies):
    import uuid
    ids = [str(uuid.UUID(int=n + 1)) for n in range(len(dependencies) + 1)]
    rows = [
        context(ids[n], None, description=" / ".join("memory_id: " + ids[i] for i in dep))
        for n, dep in enumerate(dependencies)
    ]
    return write_archive(tmp_path, contexts=rows), ids


def test_description_forward_reference_only_uses_destination_ids(tmp_path):
    from tools.verify_restore import verify_restore
    file, ids = description_archive(tmp_path, [[1, 1], []])
    api = ContextDestination()
    assert importer.run_zip_import(file, api) == 0
    cp_path = tmp_path / importer.ZIP_CHECKPOINT_FILENAME
    checkpoint = importer.ZipCheckpoint.load(cp_path)
    origin = api.rows[checkpoint.contexts[ids[0]]]
    target = checkpoint.contexts[ids[1]]
    assert json.loads(origin["content"])["description"] == f"memory_id: {target} / memory_id: {target}"
    assert importer.run_zip_import(file, api) == 0
    assert len(api.rows) == 2
    result = verify_restore(file, cp_path, tmp_path / "verified.jsonl", api)
    assert result["outcomes"] == {"matched": 2}


def test_description_cycles_and_absent_reference_have_exact_row_diagnostics(tmp_path, capsys, caplog):
    from tools.verify_restore import verify_restore
    file, ids = description_archive(tmp_path, [[1], [], [2], [4], [3], [3], [7]])
    api = ContextDestination()
    assert importer.run_zip_import(file, None, dry_run=True) == 0
    preview = capsys.readouterr().out
    for i in (2, 3, 4, 5):
        assert f"contexts.jsonl line {i + 2} ({ids[i]}): blocked by context cycle" in preview
    assert f"contexts.jsonl line 8 ({ids[6]}): partial context" in preview
    assert importer.run_zip_import(file, api) == 2
    assert len(api.rows) == 3
    for i in (2, 3, 4, 5):
        assert f"contexts.jsonl line {i + 2} ({ids[i]}): blocked by context cycle" in caplog.text
    path = tmp_path / importer.ZIP_CHECKPOINT_FILENAME
    assert importer.run_zip_import(file, api) == 2
    assert len(api.rows) == 3
    result = verify_restore(file, path, tmp_path / "verify.jsonl", api)
    assert result["outcomes"] == {"matched": 2, "unrestored": 4, "partial_source": 1}
    verified = {r["source_key"]: r for r in map(json.loads, (tmp_path / "verify.jsonl").read_text().splitlines())}
    assert verified[ids[6]]["missing_description_references"] == [ids[7]]
    assert verified[ids[2]]["missing_description_references"] == [ids[2]]
    assert {i for i, key in enumerate(ids) if verified.get(key, {}).get("reason") == "blocked_by_context_cycle"} == {2, 3, 4, 5}


def test_legacy_context_receipt_with_stale_forward_id_cannot_complete(tmp_path):
    from tools.verify_restore import verify_restore
    file, ids = description_archive(tmp_path, [[1], []])
    api = ContextDestination()
    assert importer.run_zip_import(file, api) == 0
    path = tmp_path / importer.ZIP_CHECKPOINT_FILENAME
    checkpoint = json.loads(path.read_text())
    del checkpoint["context_description_checks"]
    path.write_text(json.dumps(checkpoint))
    first = api.rows[checkpoint["contexts"][ids[0]]]
    old = json.loads(first["content"])
    old["description"] = f"memory_id: {ids[1]}"
    first["content"] = json.dumps(old)
    assert importer.run_zip_import(file, api) == 2
    assert len(api.rows) == 2
    result = verify_restore(file, path, tmp_path / "verify.jsonl", api)
    assert result["outcomes"] == {"mismatch": 1, "matched": 1}
    old["description"] = "memory_id: " + checkpoint["contexts"][ids[1]]
    first["content"] = json.dumps(old)
    assert importer.run_zip_import(file, api) == 0
    assert len(api.rows) == 2


def test_description_failed_memory_recovers_on_retry_and_clears_ledger(tmp_path, monkeypatch):
    from tools.verify_restore import verify_restore
    source = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    file = write_archive(
        tmp_path, memories=[{"id": source, "content": "record"}],
        contexts=[context("child", None, description="memory_id: " + source.upper())],
    )
    api = ContextDestination()
    path = tmp_path / importer.ZIP_CHECKPOINT_FILENAME
    with monkeypatch.context() as block:
        block.setattr(api, "create_memory", Mock(side_effect=importer.APIError(422, "rejected", "/memories")))
        assert importer.run_zip_import(file, api) == 2
    cp = importer.ZipCheckpoint.load(path)
    assert cp.context_unresolved_descriptions == {"child": [source]}
    assert cp.failed_contexts == ["child"] and not cp.contexts
    assert verify_restore(file, path, tmp_path / "before.jsonl", api)["outcomes"] == {"unrestored": 2}

    assert importer.run_zip_import(file, api) == 0
    assert importer.run_zip_import(file, api) == 0
    assert len(api.rows) == 2
    cp = importer.ZipCheckpoint.load(path)
    restored = json.loads(api.rows[cp.contexts["child"]]["content"])
    assert restored["description"] == "memory_id: " + cp.memory_id_map[source]
    assert not cp.context_unresolved and not cp.context_unresolved_descriptions and not cp.failed_contexts
    assert verify_restore(file, path, tmp_path / "after.jsonl", api)["outcomes"] == {"matched": 2}


def test_mixed_context_cycle_and_dependency_on_partial_protected_reference(tmp_path, capsys):
    from tools.verify_restore import verify_restore
    first = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    protected = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    rows = [
        context(first, ["second"]),
        context("second", None, description="memory_id: " + first),
        context("downstream", ["partial"]),
        context("partial", None, description="memory_id: " + protected),
    ]
    file = write_archive(tmp_path, contexts=rows,
                         memories=[{"id": protected, "content": "identity", "memory_type": "identity_core"}])
    api = ContextDestination()
    assert importer.run_zip_import(file, None, dry_run=True) == 0
    preview = capsys.readouterr().out
    assert f"line 2 ({first}): blocked by context cycle. memory_ids=second" in preview
    assert f"line 3 (second): blocked by context cycle. description memory_id={first}" in preview
    assert importer.run_zip_import(file, api) == 2
    assert importer.run_zip_import(file, api) == 2
    assert len(api.rows) == 2
    path = tmp_path / importer.ZIP_CHECKPOINT_FILENAME
    cp = importer.ZipCheckpoint.load(path)
    assert cp.context_unresolved == {first: ["second"]}
    assert cp.context_unresolved_descriptions == {"second": [first], "partial": [protected]}
    assert json.loads(api.rows[cp.contexts["partial"]]["content"])["description"] == "memory_id: " + protected
    assert json.loads(api.rows[cp.contexts["downstream"]]["content"])["memory_ids"] == [cp.contexts["partial"]]
    assert verify_restore(file, path, tmp_path / "result.jsonl", api)["outcomes"] == {
        "protected_exclusion": 1, "unrestored": 2, "partial_source": 1, "matched": 1,
    }

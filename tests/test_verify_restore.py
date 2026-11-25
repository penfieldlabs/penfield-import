import hashlib
import json
import stat
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock
import zipfile

import pytest

import penfield_import as importer
from tools import verify_restore as verify


@pytest.fixture
def restore(tmp_path):
    archive = tmp_path / "source.zip"
    sections = {
        "memories": [{"id": "m", "content": "private text", "memory_type": "fact", "metadata": {"source": "test"}},
                     {"id": "omit", "content": "protected", "memory_type": "identity_core"}],
        "relationships": [{"id": "r", "from_id": "m", "to_id": "c", "relationship_type": "references"}],
        "contexts": [{"id": "c", "name": "A - B", "memory_ids": ["m"], "description": "saved"}],
        "artifacts": [{"path": "/note.txt", "content_type": "text/plain"}],
        "documents": [{"id": "d", "filename": "fixture.pdf", "mime_type": "application/pdf"}],
    }
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("manifest.json", json.dumps({"schema_version": "1.0", "source_tenant_id": "source",
                                              "counts": {k: len(rows) for k, rows in sections.items()}}))
        for section, rows in sections.items():
            z.writestr(section + ".jsonl", "\n".join(map(json.dumps, rows)))
        z.writestr("documents/d/original.pdf", b"document bytes")
        z.writestr("artifacts/note.txt", b"text\r\n")
    destination = '["https://example.invalid","tenant","user"]'
    checkpoint = importer.ZipCheckpoint(source_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        source_tenant_id="source", destination=destination, phase="done", memory_id_map={"m": "M"},
        skipped_memories={"omit": "identity_core"}, contexts={"c": "C"}, relationships_done={"r"},
        relationship_id_map={"r": "R"}, artifacts={"/note.txt": "/note.txt"}, documents={"d": "D"})
    checkpoint.save(tmp_path / importer.ZIP_CHECKPOINT_FILENAME)
    context_json = {"checkpoint_name": "A - B", "description": "saved", "memory_count": 1,
                    "memory_ids": ["M"], "referenced_memories": ["M"]}
    live = {
        "/memories/M": {"id":"M", "content":"private text", "memory_type":"fact", "metadata":{"source":"test"}},
        "/memories/C": {"id":"C", "content": json.dumps(context_json), "memory_type":"checkpoint", "importance":0.9,
                         "tags":["context","checkpoint","a-b"], "metadata": {}},
        "/relationships/R": {"id":"R", "from_id":"M", "to_id":"C", "relationship_type":"references"},
        "/artifacts": {"path":"/note.txt","content":"text\r\n","content_type":"text/plain"},
        "/documents/D": {"id":"D","filename":"fixture.pdf","content_type":"application/pdf","processing_status":"completed",
                         "embedding_progress":{"total":1, "completed":1, "pending":0, "failed":0}},
    }
    client = Mock(spec=importer.PenfieldClient)
    client.destination_identity.return_value = destination

    def read(method, path):
        assert method == "GET"
        parsed = urlsplit(path)
        if parsed.path == "/artifacts":
            assert parse_qs(parsed.query) == {"path":["/note.txt"]}
        return {"data": live[parsed.path]}

    client.request.side_effect = read
    return archive, live, client


def run(archive, client, monkeypatch):
    monkeypatch.setattr(verify, "_client", lambda origin: client)
    output = archive.parent / "audit.jsonl"
    code = verify.main([str(archive), "--base-url", "https://example.invalid", "--output", str(output)])
    return code, output


def test_verifies_all_sections_by_id_with_private_output(restore, monkeypatch):
    archive, _, client = restore
    code, output = run(archive, client, monkeypatch)
    assert code == 0
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == 6
    assert {row["outcome"] for row in rows} == {"matched", "protected_exclusion"}
    assert "private text" not in output.read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    client._verify_document_bytes.assert_called_once_with("/documents/D", b"document bytes")
    assert client.method_calls[0][0] == "destination_identity"
    original = output.read_bytes()
    assert run(archive, client, monkeypatch)[0] == 1
    assert output.read_bytes() == original


@pytest.mark.parametrize("binding", ["origin", "account", "archive"])
def test_binding_precedes_object_reads_and_output(restore, monkeypatch, binding):
    archive, _, client = restore
    if binding == "archive":
        with archive.open("ab") as changed:
            changed.write(b"changed")
    elif binding == "origin":
        path = archive.parent / importer.ZIP_CHECKPOINT_FILENAME
        state = importer.ZipCheckpoint.load(path)
        state.destination = '["https://another.invalid","tenant","user"]'
        state.save(path)
    else:
        client.destination_identity.return_value = '["https://example.invalid","tenant","other-user"]'
    code, output = run(archive, client, monkeypatch)
    assert code == 1 and not output.exists()
    client.request.assert_not_called()
    client._verify_document_bytes.assert_not_called()


def test_flags_data_drift_and_failed_chunks_independently(restore, monkeypatch):
    archive, live, client = restore
    live["/memories/M"]["content"] = "edited"
    live["/artifacts"]["content"] = "text\n"
    live["/documents/D"]["embedding_progress"]["failed"] = 1
    live["/relationships/R"]["from_id"] = "other"
    code, output = run(archive, client, monkeypatch)
    assert code == 2
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert {row["source_key"]:row["mismatched_fields"] for row in rows if row["section"] in {"memories","relationships"}} == {
        "m":["content"], "omit":[], "r":["from_id"],
    }
    assert next(row for row in rows if row["section"] == "artifacts")["mismatched_fields"] == ["content.bytes"]
    document = next(row for row in rows if row["section"] == "documents")
    assert document["download_equal"] and document["outcome"] == "indexing_failed"


def test_partial_context_is_nonzero_with_exact_remaining_members(restore, monkeypatch):
    archive, _, client = restore
    with zipfile.ZipFile(archive) as z:
        contents = {name: z.read(name) for name in z.namelist()}
    record = json.loads(contents["contexts.jsonl"])
    record["memory_ids"].append("missing")
    contents["contexts.jsonl"] = json.dumps(record).encode()
    with zipfile.ZipFile(archive, "w") as z:
        for name, data in contents.items():
            z.writestr(name, data)
    path = archive.parent / importer.ZIP_CHECKPOINT_FILENAME
    state = importer.ZipCheckpoint.load(path)
    state.source_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    state.context_unresolved["c"] = ["missing"]
    state.save(path)
    code, output = run(archive, client, monkeypatch)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    context = next(row for row in rows if row["section"] == "contexts")
    assert code == 2 and context["outcome"] == "partial_source"
    assert context["missing_members"] == ["missing"] and context["mismatched_fields"] == []


def test_document_binary_mismatch_and_read_error_are_distinct(restore, monkeypatch):
    archive, _, client = restore
    client._verify_document_bytes.side_effect = importer.APIError(0, "Document bytes differ", "/documents/D")
    normal = client.request.side_effect

    def read(method, path):
        if path == "/relationships/R":
            raise importer.APIError(404, "deleted", path)
        return normal(method, path)

    client.request.side_effect = read
    code, output = run(archive, client, monkeypatch)
    rows = {r["source_key"]: r for r in map(json.loads, output.read_text().splitlines())}
    assert code == 2
    assert rows["d"]["download_equal"] is False and rows["d"]["mismatched_fields"] == ["content.bytes"]
    assert rows["r"]["outcome"] == "read_error" and rows["r"]["status_code"] == 404

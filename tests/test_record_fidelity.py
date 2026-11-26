"""ZIP write receipts and independent readback must agree about object identity and JSON types."""
import copy
import io
import json
from urllib.parse import urlsplit
import zipfile

import pytest

import penfield_import as imp
from tools import verify_restore


PROPERTIES = {"nested": {"items": [True, 0.0]}}
TARGET = '["https://example.invalid","fixture","user"]'


def archive_with(tmp_path, documents=None, relationships=None, memory_metadata=None):
    path = tmp_path / "source.zip"
    sections = {s: [] for s in imp.ZIP_SECTIONS}
    sections["memories"] = [{"id": "a", "content": "A", "metadata": memory_metadata}, {"id": "b", "content": "B"}]
    sections["documents"] = documents or []
    sections["relationships"] = relationships or []
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"schema_version": "1.0", "source_tenant_id": "fixture",
            "counts": {key: len(items) for key, items in sections.items()}}))
        for section, items in sections.items():
            archive.writestr(section + ".jsonl", "\n".join(json.dumps(item) for item in items))
        for doc in sections["documents"]:
            archive.writestr("documents/" + doc["id"] + "/original.txt", b"abc")
    return path


def edge(key="e", **kwargs):
    return dict(id=key, from_id="a", to_id="b", relationship_type="supports", **kwargs)


def document(key="d", **kwargs):
    return dict(id=key, filename="same.txt", mime_type="text/plain", **kwargs)


class Service:
    def __init__(self, monkeypatch):
        self.api = imp.PenfieldClient(base_url="https://example.invalid", access_token="fixture")
        self.api.destination_identity = lambda: TARGET
        self.api._rate_limiter.wait = lambda: None
        self.api.request = self.request
        self.objects = {}
        self.posted = []
        self.coerce = None
        self.conflict = False
        monkeypatch.setattr(imp, "open_request", self.open)
        monkeypatch.setattr(verify_restore, "_client", lambda _: self.api)

    def fields(self, item):
        result = copy.deepcopy(item)
        if self.coerce and self.coerce in result:
            result[self.coerce]["nested"]["items"][0] = 1
        return result

    def request(self, method, path, data=None):
        if method == "GET" and path.startswith("/relationships?"):
            return {"data": {"items": [self.fields(self.objects["/relationships/R"])], "pagination": {"has_next": False}}}
        if method == "GET":
            return {"data": self.fields(self.objects[path])}
        assert method == "POST"
        self.posted.append((path, data))
        if path == "/memories":
            record = dict(data, id=data["content"])
            self.objects["/memories/" + record["id"]] = record
            return {"data": {"id": record["id"]}}
        assert path == "/relationships/bulk" and len(data) == 1
        stored = dict(data[0], id="R")
        if "relationship_metadata" in stored:
            stored["metadata"] = stored.pop("relationship_metadata")
        self.objects.setdefault("/relationships/R", stored)
        if self.conflict:
            raise imp.APIError(409, "already exists", path)
        return {"data": {"created": [{"id": "R"}]}}

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
        if path.endswith("/download"):
            return io.BytesIO(b"abc")
        assert path.endswith("/documents/upload") and request.get_method() == "POST"
        self.posted.append((path, None))
        stored = dict(id="D", filename="same.txt", content_type="text/plain", processing_status="completed",
                      embedding_progress={"completed": 1, "total": 1, "pending": 0, "failed": 0})
        if b'name="metadata"' in request.data:
            stored["metadata"] = copy.deepcopy(PROPERTIES)
        self.objects["/documents/D"] = stored
        return io.BytesIO(b'{"data":{"id":"D"}}')


def verify(archive, output="audit.jsonl"):
    report = archive.parent / output
    code = verify_restore.main([str(archive), "--base-url", "https://example.invalid", "--output", str(report)])
    return code, [json.loads(line) for line in report.read_text().splitlines()]


@pytest.mark.parametrize("section,field", [
    ("memories", "metadata"), ("documents", "metadata"), ("relationships", "evidence"), ("relationships", "metadata"),
])
def test_write_receipt_and_verifier_reject_nested_bool_number_change(tmp_path, monkeypatch, section, field):
    service = Service(monkeypatch)
    archive = archive_with(tmp_path, documents=[document(metadata=PROPERTIES)] if section == "documents" else [],
                           relationships=[edge(**{field: PROPERTIES})] if section == "relationships" else [],
                           memory_metadata=PROPERTIES if section == "memories" else None)
    assert imp.run_zip_import(archive, service.api, tmp_path / "clean") == 0
    clean = tmp_path / "clean" / imp.ZIP_CHECKPOINT_FILENAME
    (tmp_path / imp.ZIP_CHECKPOINT_FILENAME).write_bytes(clean.read_bytes())
    service.coerce = field
    code, rows = verify(archive)
    assert code == 2 and any(row["section"] == section and field in row["mismatched_fields"] for row in rows)
    assert imp.run_zip_import(archive, service.api, tmp_path / "corrupt") == 1
    state = imp.ZipCheckpoint.load(tmp_path / "corrupt" / imp.ZIP_CHECKPOINT_FILENAME)
    assert state.in_flight.get("acknowledgement")


@pytest.mark.parametrize("field", ["evidence", "metadata"])
def test_conflict_does_not_claim_type_coercion_as_identical(tmp_path, monkeypatch, field):
    service = Service(monkeypatch)
    service.conflict = True
    service.coerce = field
    archive = archive_with(tmp_path, relationships=[edge(**{field: PROPERTIES})])
    assert imp.run_zip_import(archive, service.api) == 2
    state = imp.ZipCheckpoint.load(tmp_path / imp.ZIP_CHECKPOINT_FILENAME)
    assert state.failed_relationships == ["e"] and state.identical_relationships == []


def test_bulk_matches_reordered_receipts_by_typed_evidence():
    first = {"from_id": "A", "to_id": "B", "relationship_type": "supports", "evidence": {"flag": True}}
    second = dict(first, evidence={"flag": 1})
    responses = iter([{"created": [{"id": "number"}, {"id": "boolean"}]},
                      dict(second, id="number", evidence={"flag": 1.0}), dict(first, id="boolean")])
    api = imp.PenfieldClient(base_url="https://example.invalid", access_token="fixture")
    api.request = lambda *args: {"data": next(responses)}
    assert api.create_relationships_bulk([first, second])["verified_ids"] == ["boolean", "number"]


def test_null_relationship_optionals_use_service_defaults(tmp_path, monkeypatch):
    service = Service(monkeypatch)
    archive = archive_with(tmp_path, relationships=[edge(direction_type=None, strength=None, confidence=None)])
    assert imp.run_zip_import(archive, None, dry_run=True) == 0
    assert imp.run_zip_import(archive, service.api) == 0
    payload = next(data[0] for path, data in service.posted if path == "/relationships/bulk")
    assert {"direction_type", "strength", "confidence"}.isdisjoint(payload)
    assert verify(archive)[0] == 0


@pytest.mark.parametrize("section", ["documents", "relationships"])
def test_duplicate_create_receipt_fails_and_verifier_marks_both_sources(tmp_path, monkeypatch, section):
    monkeypatch.setattr(imp, "BULK_RELATIONSHIP_BATCH_SIZE", 1)
    service = Service(monkeypatch)
    archive = archive_with(tmp_path, documents=[document("d1"), document("d2")] if section == "documents" else [],
                           relationships=[edge("r1"), edge("r2")] if section == "relationships" else [])
    initial = imp.run_zip_import(archive, service.api)
    state_path = tmp_path / imp.ZIP_CHECKPOINT_FILENAME
    state = imp.ZipCheckpoint.load(state_path)
    # Simulate a pre-fix completed checkpoint claiming two independent creates.
    state.in_flight = {}
    state.phase = "done"
    if section == "documents":
        state.documents = {"d1": "D", "d2": "D"}
    else:
        state.relationship_id_map = {"r1": "R", "r2": "R"}
        state.relationships_done.update(state.relationship_id_map)
    state.save(state_path)
    code, rows = verify(archive)
    flagged = [row for row in rows if "checkpoint.duplicate_destination_id" in row["mismatched_fields"]]
    assert (initial, code, len(flagged)) == (1, 2, 2)
    writes = len(service.posted)
    assert imp.run_zip_import(archive, service.api) != 0
    assert len(service.posted) == writes


@pytest.mark.parametrize("different", [False, True])
def test_explicit_identical_conflict_is_only_share_for_same_edge(tmp_path, monkeypatch, different):
    monkeypatch.setattr(imp, "BULK_RELATIONSHIP_BATCH_SIZE", 1)
    service = Service(monkeypatch)
    original = service.request

    def requests(method, path, data=None):
        if method == "POST" and path == "/relationships/bulk" and "/relationships/R" in service.objects:
            service.conflict = True
        return original(method, path, data)

    service.api.request = requests
    # The service-added field would allow each source to match in isolation.
    first = edge("r1", evidence={"weight": 1})
    second = edge("r2", evidence={} if different else {"weight": 1.0})
    archive = archive_with(tmp_path, relationships=[first, second])
    code = imp.run_zip_import(archive, service.api)
    if different:
        assert code != 0
        path = tmp_path / imp.ZIP_CHECKPOINT_FILENAME
        old = imp.ZipCheckpoint.load(path)
        old.relationships_done.add("r2")
        old.relationship_id_map["r2"] = "R"
        old.identical_relationships.append("r2")
        old.save(path)
        audit_code, rows = verify(archive)
        edges = [row for row in rows if row["section"] == "relationships"]
        assert audit_code == 2 and all("checkpoint.duplicate_destination_id" in row["mismatched_fields"] for row in edges)
    else:
        assert code == 0
        assert verify(archive)[0] == 0


def test_vault_rejects_reused_document_id_and_preserves_intent(tmp_path):
    docs = tmp_path / "Documents"
    docs.mkdir()
    for name in ("one", "two"):
        (docs / (name + ".txt")).write_text("test")
    state = imp.Checkpoint()
    path = tmp_path / "checkpoint.json"

    class DuplicateService:
        def upload_document_bytes(self, *args, **kwargs):
            return {"id": "D", "processing_status": "completed"}

    with pytest.raises(ValueError, match="repeated document"):
        imp.run_documents_phase(tmp_path, DuplicateService(), state, path)
    saved = imp.Checkpoint.load(path)
    assert len(saved.documents) == 1 and saved.pending_write["kind"] == "document"
    with pytest.raises(ValueError, match="repeated document"):
        imp.Checkpoint.from_dict({"documents": {"one.txt": "D", "two.txt": "D"}})

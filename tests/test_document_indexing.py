"""Original document restoration and asynchronous vector indexing have separate receipts."""
import json
import zipfile
from unittest.mock import Mock

import pytest

import penfield_import as imp


def receipt(*, completed, pending, failed, status="completed"):
    return {
        "id": "destination-document",
        "processing_status": status,
        "embedding_progress": {"total": 100, "completed": completed, "pending": pending, "failed": failed},
    }


def document_archive(path, **fields):
    counts = {section: int(section == "documents") for section in imp.ZIP_SECTIONS}
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "schema_version": "1.0.0", "source_tenant_id": "source", "counts": counts,
        }))
        for section in counts:
            row = json.dumps(dict(id="doc", filename="original.txt", **fields)) if section == "documents" else ""
            zf.writestr(section + ".jsonl", row)
        zf.writestr("documents/doc/original.txt", b"original bytes")
    return path


def test_tagged_document_retains_verified_bytes_and_reports_omitted_tags(tmp_path, capsys):
    api = Mock(spec=imp.PenfieldClient)
    api.destination_identity.return_value = "verified-target"
    api.upload_document_bytes.return_value = receipt(completed=100, pending=0, failed=0)
    archive = document_archive(tmp_path / "tagged.zip", tags=["source-tag"])
    for _ in range(2):
        assert imp.run_zip_import(archive, api) == 2
        assert "DOCUMENTS: 0 restored, 0 identical, 1 partial" in capsys.readouterr().out
    assert imp.ZipCheckpoint.load(tmp_path / imp.ZIP_CHECKPOINT_FILENAME).document_tag_omissions == {"doc": ["source-tag"]}
    api.upload_document_bytes.assert_called_once()


def test_zip_doc_refreshes_chunk_outcomes_without_reupload(tmp_path, capsys):
    api = Mock(spec=imp.PenfieldClient)
    api.destination_identity.return_value = "verified-target"
    archive = document_archive(tmp_path / "test.zip")
    api.upload_document_bytes.return_value = receipt(completed=70, pending=30, failed=0)
    assert imp.run_zip_import(archive, api, checkpoint_dir=tmp_path) == 0
    assert "Indexing is pending or unconfirmed for 1 document" in capsys.readouterr().out
    api.document_status.side_effect = [
        receipt(completed=99, pending=0, failed=1),
        receipt(completed=100, pending=0, failed=0, status="active"),
    ]
    assert imp.run_zip_import(archive, api, checkpoint_dir=tmp_path) == 2
    assert "'failed': 1" in capsys.readouterr().out
    assert imp.run_zip_import(archive, api, checkpoint_dir=tmp_path) == 0
    assert "'ready': 1" in capsys.readouterr().out
    assert imp.run_zip_import(archive, api, checkpoint_dir=tmp_path) == 0
    assert api.document_status.call_count == 2
    api.upload_document_bytes.assert_called_once()


def test_vault_refreshes_failed_chunks_despite_completed_processing(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"
    docs.mkdir()
    (docs / "example.txt").write_text("original bytes")
    args = imp.build_parser().parse_args([str(tmp_path)])
    api = Mock(spec=imp.PenfieldClient)
    api.destination_identity.return_value = "verified-target"
    api.get_memory_count.return_value = 0
    api.upload_document_bytes.return_value = receipt(completed=99, pending=0, failed=1)
    api.document_status.return_value = receipt(completed=100, pending=0, failed=0, status="active")
    monkeypatch.setattr(imp, "_resolve_auth", lambda args: imp._AuthInfo(api_key="fixture"))
    monkeypatch.setattr(imp, "PenfieldClient", lambda **kwargs: api)
    assert imp.run_vault_import(tmp_path, args) == 1
    report = tmp_path / ".penfield_import_report.txt"
    assert "example.txt: failed" in report.read_text()
    assert imp.run_vault_import(tmp_path, args) == 0
    assert "ready=1" in report.read_text()
    assert imp.run_vault_import(tmp_path, args) == 0
    api.upload_document_bytes.assert_called_once()
    api.document_status.assert_called_once_with("destination-document")


@pytest.mark.parametrize("status,progress,expected", [
    ("active", None, "pending"),
    ("completed", {"total": 100, "completed": 70, "pending": 30, "failed": 0}, "pending"),
    ("completed", {"total": 100, "completed": 99, "pending": 0, "failed": 1}, "failed"),
    ("failed", {"total": 100, "completed": 100, "pending": 0, "failed": 0}, "failed"),
    ("active", {"total": 100, "completed": 100, "pending": 0, "failed": 0}, "ready"),
    ("completed", {"total": "?", "percent_complete": 100}, "unknown"),
])
def test_index_state(status, progress, expected):
    assert imp._index_state(status, progress) == expected

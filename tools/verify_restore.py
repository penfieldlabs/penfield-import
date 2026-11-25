"""Verify a portal ZIP restore against its current Penfield destination."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlencode
import zipfile
import zlib

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import penfield_auth
import penfield_import as importer
from penfield_io import origin


MEMBER_TOKEN = re.compile(r"(\bmemory_id:\s*)([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})(?![0-9a-fA-F])")
CLEAN_OUTCOMES = {"matched", "protected_exclusion"}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _get(client, path):
    reply = client.request("GET", path)
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, dict) or reply.get("status") == "error":
        raise ValueError("Malformed destination response")
    return data


def _real(value):
    return isinstance(value, str) and bool(value) and not value.startswith(importer.SKIP_SENTINEL_PREFIX)


def _different_fields(expected, actual):
    different = []
    for key, wanted in expected.items():
        value = actual.get(key)
        if key == "tags":
            wanted = sorted({importer.normalize_tag(tag) for tag in wanted})
            value = sorted(value) if isinstance(value, list) else ([] if value is None else value)
        elif key in {"metadata", "evidence"} and isinstance(wanted, dict):
            # Ignore fields added by the service; compare every key supplied by the source.
            if isinstance(value, dict):
                value = {k: value[k] for k in wanted if k in value}
            elif value is None and not wanted:
                value = {}
        if not importer._json_values_equal(value, wanted):
            different.append(key)
    return different


def _memory(record, stored, dest, result):
    expected = {k: record[k] for k in ("memory_type", "source_type", "importance", "confidence")
                if record.get(k) is not None}
    expected.update(id=dest, content=record["content"], tags=record.get("tags") or [],
                    metadata=record.get("metadata") or {})
    if expected.get("memory_type") == "personality_trait":
        expected["memory_type"] = "fact"
        prefixed = "[Imported personality_trait]\n\n" + record["content"]
        if len(prefixed) <= importer.MEMORY_CONTENT_LIMIT:
            expected["content"] = prefixed
        result["transformation"] = "personality_trait_to_fact"
    result["expected_content_sha256"] = _sha(expected["content"].encode())
    content = stored.get("content")
    result["actual_content_sha256"] = _sha(content.encode()) if isinstance(content, str) else None
    result["mismatched_fields"].extend(_different_fields(expected, stored))


def _context(record, stored, dest, mapped, checkpoint, source_ids, result):
    original = record.get("memory_ids") or []
    members = [mapped[mid] for mid in original if mid in mapped]
    missing = [mid for mid in original if mid not in mapped]
    description = MEMBER_TOKEN.sub(lambda m: m[1] + mapped.get(m[2], m[2]), record.get("description") or "")
    tags = ["context", "checkpoint"]
    name_tag = importer.normalize_tag(record["name"])
    if name_tag not in tags and 1 <= len(name_tag) <= 50:
        tags.append(name_tag)
    bad = result["mismatched_fields"]
    bad.extend(_different_fields({"id": dest, "memory_type": "checkpoint", "importance": 0.9,
                                  "metadata": record.get("checkpoint_metadata") or {}, "tags": tags}, stored))
    expected = {"checkpoint_name": record["name"], "description": description, "memory_count": len(members),
                "memory_ids": members, "referenced_memories": members}
    try:
        content = json.loads(stored.get("content") or "")
    except (ValueError, TypeError):
        content = None
    if isinstance(content, dict):
        bad.extend("content." + field for field in _different_fields(expected, content))
    else:
        bad.append("content.structure")
    result.update(source_member_count=len(original), resolved_member_count=len(members), missing_members=missing)
    if missing != checkpoint.context_unresolved.get(record["id"], []):
        bad.append("checkpoint.unresolved_members")
    if any(mid in source_ids and mid not in checkpoint.skipped_memories for mid in missing):
        bad.append("unresolved_importable_member")
    if missing:
        result["outcome"] = "partial_source"


def _edge(record, stored, dest, mapped, result):
    expected = {"id": dest, "from_id": mapped.get(record["from_id"]), "to_id": mapped.get(record["to_id"]),
                "relationship_type": record["relationship_type"], "metadata": record.get("metadata") or {}}
    for key in ("direction_type", "strength", "confidence", "inverse_type", "evidence"):
        if record.get(key) is not None:
            expected[key] = record[key]
    if not expected["from_id"] or not expected["to_id"]:
        result["mismatched_fields"].append("checkpoint.unmapped_endpoint")
    if "metadata" not in stored and "relationship_metadata" in stored:
        stored = dict(stored, metadata=stored["relationship_metadata"])
    if record.get("traversal_properties"):
        if "traversal_properties" in stored:
            expected["traversal_properties"] = record["traversal_properties"]
        else:
            result["unobservable_fields"] = ["traversal_properties"]
            result["outcome"] = "unobservable"
    result["mismatched_fields"].extend(_different_fields(expected, stored))


def _artifact(archive, record, stored, dest, result):
    key = record["path"]
    if dest != key or stored.get("path") != key:
        result["mismatched_fields"].append("path")
    source = archive.read("artifacts" + key)
    content = stored.get("content")
    actual = content.encode("utf-8") if isinstance(content, str) else None
    result.update(source_sha256=_sha(source), actual_sha256=_sha(actual) if actual is not None else None,
                  source_size=len(source), actual_size=len(actual) if actual is not None else None)
    if actual != source:
        result["mismatched_fields"].append("content.bytes")
    if record.get("content_type") and str(stored.get("content_type") or "").lower() != record["content_type"].lower():
        result["mismatched_fields"].append("content_type")


def _document(archive, record, stored, dest, client, path, result):
    expected = {"id": dest, "filename": record["filename"], "metadata": record.get("metadata") or {}}
    result["mismatched_fields"].extend(_different_fields(expected, stored))
    media = stored.get("content_type") or stored.get("mime_type")
    if record.get("mime_type") and record["mime_type"] != media:
        result["mismatched_fields"].append("content_type")
    source = archive.read(importer._zip_document_entry(record["id"], record["filename"]))
    result["source_sha256"] = _sha(source)
    try:
        client._verify_document_bytes(path, source)
        result["download_equal"] = True
    except importer.APIError as exc:
        if exc.body != "Document bytes differ":
            raise
        result["download_equal"] = False
        result["mismatched_fields"].append("content.bytes")
    status = importer._processing_status(stored)
    progress = stored.get("embedding_progress")
    state = importer._index_state(status, progress)
    result.update(processing_status=status, indexing_state=state)
    if isinstance(progress, dict):
        result["embedding_progress"] = {k: v for k, v in progress.items()
                                        if k in {"total", "completed", "pending", "failed", "percent_complete"}
                                        and type(v) in (int, float)}
    if record.get("tags"):
        result["omitted_document_tags"] = len(record["tags"])
        result["outcome"] = "partial_source"
    if state != "ready":
        result["outcome"] = "indexing_" + state


def _verify_rows(archive, checkpoint, keys, client, stream):
    ledgers = {"memories": checkpoint.memory_id_map, "contexts": checkpoint.contexts,
               "relationships": checkpoint.relationship_id_map, "artifacts": checkpoint.artifacts,
               "documents": checkpoint.documents}
    mapped = {k: value for section in ("memories", "contexts") for k, value in ledgers[section].items() if _real(value)}
    destinations = Counter(mapped.values())
    source_ids = keys["memories"] | keys["contexts"]
    counts = {section: Counter() for section in importer.ZIP_SECTIONS}
    for section, ledger in ledgers.items():
        for number, record in importer._zip_records(archive, section):
            key = record["path" if section == "artifacts" else "id"]
            result = {"archive_sha256": checkpoint.source_sha256, "section": section, "line": number,
                      "source_key": _sha(key.encode()) if section == "artifacts" else key,
                      "outcome": "matched", "mismatched_fields": []}
            dest = ledger.get(key)
            try:
                if section == "memories" and record.get("memory_type") == "identity_core":
                    result["outcome"] = "protected_exclusion"
                    if dest is not None or checkpoint.skipped_memories.get(key) != "identity_core":
                        result["mismatched_fields"].append("checkpoint.protected_exclusion")
                elif not _real(dest):
                    result["outcome"] = "unrestored"
                    if isinstance(dest, str) and dest.startswith(importer.SKIP_SENTINEL_PREFIX):
                        result["checkpoint_disposition"] = dest
                elif section in ("memories", "contexts"):
                    result["destination_id"] = dest
                    if destinations[dest] != 1:
                        result["mismatched_fields"].append("checkpoint.duplicate_destination_id")
                    stored = _get(client, "/memories/" + quote(dest, safe=""))
                    if section == "memories":
                        _memory(record, stored, dest, result)
                    else:
                        _context(record, stored, dest, mapped, checkpoint, source_ids, result)
                elif section == "relationships":
                    result["destination_id"] = dest
                    _edge(record, _get(client, "/relationships/" + quote(dest, safe="")), dest, mapped, result)
                elif section == "artifacts":
                    stored = _get(client, "/artifacts?" + urlencode({"path": dest}))
                    _artifact(archive, record, stored, dest, result)
                else:
                    path = "/documents/" + quote(dest, safe="")
                    result["destination_id"] = dest
                    _document(archive, record, _get(client, path), dest, client, path, result)
                if result["mismatched_fields"]:
                    result["outcome"] = "mismatch"
            except (importer.APIError, OSError, ValueError, TypeError, KeyError) as exc:
                result.update(outcome="read_error", error_class=type(exc).__name__)
                if isinstance(exc, importer.APIError):
                    result["status_code"] = exc.status_code
                    if exc.request_id:
                        result["request_id"] = exc.request_id
            counts[section][result["outcome"]] += 1
            stream.write(json.dumps(result, sort_keys=True) + "\n")
            stream.flush()
    return counts


def verify_restore(archive_path, checkpoint_path, output_path, client):
    checkpoint = importer.ZipCheckpoint.load(Path(checkpoint_path))
    if not checkpoint.source_sha256 or not checkpoint.destination:
        raise ValueError("Verification requires an existing archive- and destination-bound checkpoint")
    digest = hashlib.sha256()
    with Path(archive_path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
        if digest.hexdigest() != checkpoint.source_sha256:
            raise ValueError("Archive SHA-256 differs from the checkpoint")
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            keys = {}
            manifest = importer.validate_zip_manifest(archive, keys)
            checkpoint.validate_source_keys(keys)
            if manifest["source_tenant_id"] != checkpoint.source_tenant_id:
                raise ValueError("Source tenant differs from the checkpoint")
            if client.destination_identity() != checkpoint.destination:
                raise ValueError("Verified destination identity differs from the checkpoint")
            fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                counts = _verify_rows(archive, checkpoint, keys, client, stream)
    outcomes = sum(counts.values(), Counter())
    incomplete = sum(n for key, n in outcomes.items() if key not in CLEAN_OUTCOMES)
    return {"archive_sha256": digest.hexdigest(), "records": sum(outcomes.values()),
            "incomplete_records": incomplete, "unconfirmed_write": bool(checkpoint.in_flight),
            "sections": counts, "outcomes": outcomes}


def _client(base_url):
    api_key = os.environ.get("PENFIELD_API_KEY")
    if api_key:
        return importer.PenfieldClient(base_url=base_url, api_key=api_key)
    cached = penfield_auth._load_tokens(base_url)
    if not cached:
        raise ValueError("Set PENFIELD_API_KEY or sign in with penfield-import --login for this base URL")

    def refresh():
        renewed = penfield_auth.refresh_oauth_token(base_url)
        return (renewed.access_token, renewed.expires_in) if renewed else None

    return importer.PenfieldClient(base_url=base_url, access_token=cached["access_token"],
                                  token_expiry_seconds=penfield_auth._remaining_seconds(cached),
                                  on_token_refresh=refresh)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--checkpoint-dir", type=Path, help="Directory containing the import checkpoint (default: archive directory)")
    parser.add_argument("--base-url", default=os.environ.get("PENFIELD_BASE_URL") or importer.PENFIELD_BASE_URL)
    parser.add_argument("--output", type=Path, required=True, help="New JSONL path in an existing directory")
    args = parser.parse_args(argv)
    path = (args.checkpoint_dir or args.archive.parent) / importer.ZIP_CHECKPOINT_FILENAME
    try:
        state = importer.ZipCheckpoint.load(path)
        base_url = origin(args.base_url)
        bound = json.loads(state.destination) if state.destination else None
        if not isinstance(bound, list) or len(bound) != 3 or bound[0] != base_url:
            raise ValueError("Requested API origin differs from the checkpoint, or the checkpoint is unbound")
        result = verify_restore(args.archive, path, args.output, _client(base_url))
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile, zlib.error, importer.APIError,
            penfield_auth.AuthError) as exc:
        print(f"Verification stopped: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 2 if result["incomplete_records"] or result["unconfirmed_write"] else 0


if __name__ == "__main__":
    sys.exit(main())

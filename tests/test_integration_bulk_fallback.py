#!/usr/bin/env python3
"""Integration tests for bulk relationship 409/500 fallback.

Runs against a live Penfield dev API with a test account.
Creates real data, exercises actual code paths, cleans up after.

Usage:
    python3 tests/test_integration_bulk_fallback.py --api-key <key> [--base-url <url>]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
import penfield_import as vtp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHarness:
    """Manages test state and cleanup."""

    def __init__(self, client: vtp.PenfieldClient, base_url: str, token: str):
        self.client = client
        self.base_url = base_url
        self.token = token
        self.memory_ids: list[str] = []
        self.relationship_ids: list[str] = []
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def create_memory(self, label: str) -> str:
        mem_id = self.client.create_memory(
            content=f"Integration test memory: {label}",
            tags=["integ-test-bulk-fallback"],
            memory_type="fact",
        )
        self.memory_ids.append(mem_id)
        return mem_id

    def get_relationships_for(self, memory_id: str) -> list[dict[str, Any]]:
        resp = self.client.request("GET", f"/relationships?memory_id={memory_id}&per_page=100")
        return resp.get("data", {}).get("items", [])

    def delete_relationship(self, rel_id: str) -> None:
        try:
            self.client.request("DELETE", f"/relationships/{rel_id}")
        except (vtp.APIError, json.JSONDecodeError):
            pass

    def cleanup(self) -> None:
        print("\n--- Cleanup ---")
        # Gather all relationships for all test memories
        seen_rels = set()
        for mem_id in self.memory_ids:
            try:
                rels = self.get_relationships_for(mem_id)
                for r in rels:
                    seen_rels.add(r["id"])
            except vtp.APIError:
                pass
        for rel_id in self.relationship_ids:
            seen_rels.add(rel_id)

        for rel_id in seen_rels:
            self.delete_relationship(rel_id)
        print(f"  Deleted {len(seen_rels)} relationships")

        for mem_id in self.memory_ids:
            try:
                self.client.request("DELETE", f"/memories/{mem_id}")
            except (vtp.APIError, json.JSONDecodeError):
                pass
        print(f"  Deleted {len(self.memory_ids)} memories")

    def assert_eq(self, actual: Any, expected: Any, msg: str) -> None:
        if actual == expected:
            self.passed += 1
            print(f"    PASS: {msg}")
        else:
            self.failed += 1
            err = f"    FAIL: {msg} — expected {expected!r}, got {actual!r}"
            print(err)
            self.errors.append(err)

    def assert_true(self, condition: bool, msg: str) -> None:
        if condition:
            self.passed += 1
            print(f"    PASS: {msg}")
        else:
            self.failed += 1
            err = f"    FAIL: {msg}"
            print(err)
            self.errors.append(err)

    def assert_in(self, item: Any, collection: Any, msg: str) -> None:
        if item in collection:
            self.passed += 1
            print(f"    PASS: {msg}")
        else:
            self.failed += 1
            err = f"    FAIL: {msg} — {item!r} not in collection"
            print(err)
            self.errors.append(err)

    def report(self) -> int:
        print(f"\n{'='*60}")
        print(f"Results: {self.passed} passed, {self.failed} failed")
        if self.errors:
            print("\nFailures:")
            for e in self.errors:
                print(f"  {e}")
        print(f"{'='*60}")
        return 1 if self.failed else 0


def make_checkpoint(tmp_dir: Path, memories: dict[str, str]) -> tuple[vtp.Checkpoint, Path]:
    cp = vtp.Checkpoint(phase="relationships", memories=memories)
    cp_path = tmp_dir / "checkpoint.json"
    cp.save(cp_path)
    return cp, cp_path


def make_notes(memory_map: dict[str, str], relationships: list[tuple[str, str, str]]) -> list[vtp.ParsedNote]:
    """Build ParsedNote list from memory map and relationship tuples.

    relationships: [(from_relpath, target_filename, rel_type), ...]
    """
    # Collect relationships per note
    note_rels: dict[str, list[tuple[str, str]]] = {}
    for from_rp, target_fn, rel_type in relationships:
        note_rels.setdefault(from_rp, []).append((target_fn, rel_type))

    notes = []
    for rel_path in memory_map:
        fn = Path(rel_path).stem
        rels = note_rels.get(rel_path, [])
        notes.append(vtp.ParsedNote(
            filename=fn, rel_path=rel_path, vault_dir="",
            content="test", body="test", frontmatter={},
            tags=[], relationships=rels,
        ))
    return notes


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------

def test_happy_path_bulk_succeeds(h: TestHarness) -> None:
    """Scenario 1: Fresh import, no duplicates — bulk succeeds on first try."""
    print("\n[Scenario 1] Happy path — bulk succeeds")

    m1 = h.create_memory("S1-A")
    m2 = h.create_memory("S1-B")
    m3 = h.create_memory("S1-C")

    memory_map = {"s1a.md": m1, "s1b.md": m2, "s1c.md": m3}
    relationships = [
        ("s1a.md", "s1b", "supports"),
        ("s1b.md", "s1c", "follows"),
    ]
    notes = make_notes(memory_map, relationships)

    with tempfile.TemporaryDirectory() as tmp:
        cp, cp_path = make_checkpoint(Path(tmp), memory_map)
        vtp.run_relationships_phase(notes, h.client, cp, cp_path)

        h.assert_eq(len(cp.relationships_done), 2, "2 relationships marked done")
        h.assert_eq(len(cp.failed_relationships), 0, "0 failures")

    # Verify on server
    rels = h.get_relationships_for(m1)
    h.assert_true(len(rels) >= 1, "Server has relationship from m1")


def test_full_batch_duplicate_resume(h: TestHarness) -> None:
    """Scenario 2: All items in batch already exist — pure resume case."""
    print("\n[Scenario 2] Full-batch duplicate — resume scenario")

    m1 = h.create_memory("S2-A")
    m2 = h.create_memory("S2-B")
    m3 = h.create_memory("S2-C")

    # Pre-create all relationships on server
    h.client.create_relationships_bulk([
        {"from_id": m1, "to_id": m2, "relationship_type": "supports"},
        {"from_id": m2, "to_id": m3, "relationship_type": "follows"},
    ])

    memory_map = {"s2a.md": m1, "s2b.md": m2, "s2c.md": m3}
    relationships = [
        ("s2a.md", "s2b", "supports"),
        ("s2b.md", "s2c", "follows"),
    ]
    notes = make_notes(memory_map, relationships)

    with tempfile.TemporaryDirectory() as tmp:
        cp, cp_path = make_checkpoint(Path(tmp), memory_map)
        # Checkpoint has NO relationships_done — simulating resume from scratch
        vtp.run_relationships_phase(notes, h.client, cp, cp_path)

        h.assert_eq(len(cp.relationships_done), 2, "Both marked as done via 409 fallback")
        h.assert_eq(len(cp.failed_relationships), 0, "0 failures")


def test_partial_batch_duplicate(h: TestHarness) -> None:
    """Scenario 3: Some items in batch exist, some don't — interrupted import."""
    print("\n[Scenario 3] Partial-batch duplicate — interrupted import")

    m1 = h.create_memory("S3-A")
    m2 = h.create_memory("S3-B")
    m3 = h.create_memory("S3-C")
    m4 = h.create_memory("S3-D")

    # Pre-create ONLY the first relationship (simulating partial commit before crash)
    h.client.create_relationships_bulk([
        {"from_id": m1, "to_id": m2, "relationship_type": "supports"},
    ])

    memory_map = {"s3a.md": m1, "s3b.md": m2, "s3c.md": m3, "s3d.md": m4}
    relationships = [
        ("s3a.md", "s3b", "supports"),   # already exists
        ("s3b.md", "s3c", "follows"),     # new
        ("s3c.md", "s3d", "references"),  # new
    ]
    notes = make_notes(memory_map, relationships)

    with tempfile.TemporaryDirectory() as tmp:
        cp, cp_path = make_checkpoint(Path(tmp), memory_map)
        vtp.run_relationships_phase(notes, h.client, cp, cp_path)

        h.assert_eq(len(cp.relationships_done), 3, "All 3 marked done (1 dup + 2 new)")
        h.assert_eq(len(cp.failed_relationships), 0, "0 failures")

    # Verify on server
    all_rels = []
    for mid in [m1, m2, m3]:
        all_rels.extend(h.get_relationships_for(mid))
    # Deduplicate by id
    unique = {r["id"]: r for r in all_rels}
    h.assert_true(len(unique) >= 3, f"Server has >= 3 relationships (got {len(unique)})")


def test_double_resume(h: TestHarness) -> None:
    """Scenario 4: Import → resume → resume again. Second resume should be a no-op."""
    print("\n[Scenario 4] Double resume — idempotency")

    m1 = h.create_memory("S4-A")
    m2 = h.create_memory("S4-B")

    memory_map = {"s4a.md": m1, "s4b.md": m2}
    relationships = [("s4a.md", "s4b", "supports")]
    notes = make_notes(memory_map, relationships)

    with tempfile.TemporaryDirectory() as tmp:
        cp, cp_path = make_checkpoint(Path(tmp), memory_map)

        # First run: creates the relationship
        vtp.run_relationships_phase(notes, h.client, cp, cp_path)
        h.assert_eq(len(cp.relationships_done), 1, "First run: 1 done")

        # Second run: should be a no-op (already in relationships_done)
        vtp.run_relationships_phase(notes, h.client, cp, cp_path)
        h.assert_eq(len(cp.relationships_done), 1, "Second run: still 1 done (no-op)")
        h.assert_eq(len(cp.failed_relationships), 0, "No failures on second run")


def test_checkpoint_survives_fallback(h: TestHarness) -> None:
    """Scenario 5: Checkpoint file is correctly persisted during individual fallback."""
    print("\n[Scenario 5] Checkpoint persistence through fallback")

    m1 = h.create_memory("S5-A")
    m2 = h.create_memory("S5-B")
    m3 = h.create_memory("S5-C")

    # Pre-create one relationship to force 409 on bulk
    h.client.create_relationships_bulk([
        {"from_id": m1, "to_id": m2, "relationship_type": "supports"},
    ])

    memory_map = {"s5a.md": m1, "s5b.md": m2, "s5c.md": m3}
    relationships = [
        ("s5a.md", "s5b", "supports"),   # duplicate
        ("s5b.md", "s5c", "follows"),     # new
    ]
    notes = make_notes(memory_map, relationships)

    with tempfile.TemporaryDirectory() as tmp:
        cp, cp_path = make_checkpoint(Path(tmp), memory_map)
        vtp.run_relationships_phase(notes, h.client, cp, cp_path)

        # Load checkpoint from disk (as resume would)
        loaded = vtp.Checkpoint.load(cp_path)
        h.assert_eq(len(loaded.relationships_done), 2, "Loaded checkpoint has 2 done")
        h.assert_eq(len(loaded.failed_relationships), 0, "Loaded checkpoint has 0 failures")

        # The keys should match
        h.assert_eq(loaded.relationships_done, cp.relationships_done, "In-memory matches disk")


def test_multi_batch_one_fails(h: TestHarness) -> None:
    """Scenario 6: Multiple batches, only one has a duplicate. Others unaffected."""
    print("\n[Scenario 6] Multi-batch — only affected batch falls back")

    # We can't easily create 100+ memories for a real multi-batch test,
    # so we patch the batch size to 2 to get multiple batches from fewer items.
    original_batch_size = vtp.BULK_RELATIONSHIP_BATCH_SIZE
    vtp.BULK_RELATIONSHIP_BATCH_SIZE = 2

    try:
        m1 = h.create_memory("S6-A")
        m2 = h.create_memory("S6-B")
        m3 = h.create_memory("S6-C")
        m4 = h.create_memory("S6-D")
        m5 = h.create_memory("S6-E")

        # Pre-create relationship that will be in batch 2
        h.client.create_relationships_bulk([
            {"from_id": m3, "to_id": m4, "relationship_type": "references"},
        ])

        memory_map = {
            "s6a.md": m1, "s6b.md": m2, "s6c.md": m3,
            "s6d.md": m4, "s6e.md": m5,
        }
        relationships = [
            ("s6a.md", "s6b", "supports"),    # batch 1 — new
            ("s6b.md", "s6c", "follows"),      # batch 1 — new
            ("s6c.md", "s6d", "references"),   # batch 2 — DUPLICATE
            ("s6d.md", "s6e", "supports"),     # batch 2 — new
        ]
        notes = make_notes(memory_map, relationships)

        with tempfile.TemporaryDirectory() as tmp:
            cp, cp_path = make_checkpoint(Path(tmp), memory_map)
            vtp.run_relationships_phase(notes, h.client, cp, cp_path)

            h.assert_eq(len(cp.relationships_done), 4, "All 4 relationships done")
            h.assert_eq(len(cp.failed_relationships), 0, "0 failures")

    finally:
        vtp.BULK_RELATIONSHIP_BATCH_SIZE = original_batch_size


def test_all_relationship_types_survive_fallback(h: TestHarness) -> None:
    """Scenario 7: Different relationship types in the same fallback batch."""
    print("\n[Scenario 7] Multiple relationship types in fallback")

    m1 = h.create_memory("S7-A")
    m2 = h.create_memory("S7-B")

    # Pre-create ONE type between these memories
    h.client.create_relationships_bulk([
        {"from_id": m1, "to_id": m2, "relationship_type": "supports"},
    ])

    memory_map = {"s7a.md": m1, "s7b.md": m2}
    # Same pair, multiple types — only "supports" is a duplicate
    relationships = [
        ("s7a.md", "s7b", "supports"),     # duplicate
        ("s7a.md", "s7b", "references"),   # new (different type)
        ("s7a.md", "s7b", "follows"),      # new (different type)
    ]
    notes = make_notes(memory_map, relationships)

    with tempfile.TemporaryDirectory() as tmp:
        cp, cp_path = make_checkpoint(Path(tmp), memory_map)
        vtp.run_relationships_phase(notes, h.client, cp, cp_path)

        h.assert_eq(len(cp.relationships_done), 3, "All 3 types done")
        h.assert_eq(len(cp.failed_relationships), 0, "0 failures")

    rels = h.get_relationships_for(m1)
    types = sorted(r["relationship_type"] for r in rels if r["from_id"] == m1 and r["to_id"] == m2)
    h.assert_eq(types, ["follows", "references", "supports"], "All 3 types on server")


def test_failed_relationships_cleared_on_successful_resume(h: TestHarness) -> None:
    """Scenario 8: Items in failed_relationships from a prior run succeed on resume."""
    print("\n[Scenario 8] Prior failures cleared on successful resume")

    m1 = h.create_memory("S8-A")
    m2 = h.create_memory("S8-B")

    memory_map = {"s8a.md": m1, "s8b.md": m2}
    relationships = [("s8a.md", "s8b", "supports")]
    notes = make_notes(memory_map, relationships)

    with tempfile.TemporaryDirectory() as tmp:
        cp, cp_path = make_checkpoint(Path(tmp), memory_map)
        # Simulate a prior run that failed this relationship
        cp.failed_relationships = ["s8a.md|s8b.md|supports"]
        cp.save(cp_path)

        vtp.run_relationships_phase(notes, h.client, cp, cp_path)

        h.assert_eq(len(cp.relationships_done), 1, "Relationship now done")
        h.assert_eq(len(cp.failed_relationships), 0, "Prior failure cleared")


def test_single_item_batch(h: TestHarness) -> None:
    """Scenario 9: Batch with exactly 1 item — no off-by-one."""
    print("\n[Scenario 9] Single-item batch")

    m1 = h.create_memory("S9-A")
    m2 = h.create_memory("S9-B")

    memory_map = {"s9a.md": m1, "s9b.md": m2}
    relationships = [("s9a.md", "s9b", "supports")]
    notes = make_notes(memory_map, relationships)

    with tempfile.TemporaryDirectory() as tmp:
        cp, cp_path = make_checkpoint(Path(tmp), memory_map)
        vtp.run_relationships_phase(notes, h.client, cp, cp_path)
        h.assert_eq(len(cp.relationships_done), 1, "Single item done")

    # Now resume — the bulk-of-1 will 409
    with tempfile.TemporaryDirectory() as tmp:
        cp2, cp_path2 = make_checkpoint(Path(tmp), memory_map)
        vtp.run_relationships_phase(notes, h.client, cp2, cp_path2)
        h.assert_eq(len(cp2.relationships_done), 1, "Single item done via 409 fallback")
        h.assert_eq(len(cp2.failed_relationships), 0, "No failures")


def test_empty_relationship_list(h: TestHarness) -> None:
    """Scenario 10: No relationships to create — should be a no-op."""
    print("\n[Scenario 10] Empty relationship list")

    m1 = h.create_memory("S10-A")

    memory_map = {"s10a.md": m1}
    notes = make_notes(memory_map, [])

    with tempfile.TemporaryDirectory() as tmp:
        cp, cp_path = make_checkpoint(Path(tmp), memory_map)
        vtp.run_relationships_phase(notes, h.client, cp, cp_path)
        h.assert_eq(len(cp.relationships_done), 0, "No relationships done (none to create)")
        h.assert_eq(len(cp.failed_relationships), 0, "No failures")


def test_vault_import_end_to_end(h: TestHarness) -> None:
    """Scenario 11: Full vault parse → import → resume cycle with real files."""
    print("\n[Scenario 11] End-to-end vault import with resume")

    with tempfile.TemporaryDirectory() as vault_dir:
        vault = Path(vault_dir)

        # Create a mini vault with frontmatter relationships
        (vault / "concept-a.md").write_text(
            "---\ntags: [test]\nsupports:\n- '[[concept-b]]'\n---\nConcept A content"
        )
        (vault / "concept-b.md").write_text(
            "---\ntags: [test]\nfollows:\n- '[[concept-c]]'\n---\nConcept B content"
        )
        (vault / "concept-c.md").write_text(
            "---\ntags: [test]\nreferences:\n- '[[concept-a]]'\n---\nConcept C content — circular ref"
        )

        # Parse vault
        notes = vtp.parse_vault(vault, vtp.DEFAULT_SKIP_DIRS, relationship_mode="frontmatter")
        h.assert_eq(len(notes), 3, "Parsed 3 notes")

        total_rels = sum(len(n.relationships) for n in notes)
        h.assert_eq(total_rels, 3, "3 relationships extracted from frontmatter")

        with tempfile.TemporaryDirectory() as cp_dir:
            cp_path = Path(cp_dir) / "checkpoint.json"

            # Phase 1: Create memories
            cp = vtp.Checkpoint()
            vtp.run_memories_phase(notes, h.client, cp, cp_path)
            h.assert_eq(len(cp.memories), 3, "3 memories created")

            # Track for cleanup
            for mem_id in cp.memories.values():
                if mem_id and not mem_id.startswith("__skip"):
                    h.memory_ids.append(mem_id)

            # Phase 2: Create relationships (first run)
            vtp.run_relationships_phase(notes, h.client, cp, cp_path)
            h.assert_eq(len(cp.relationships_done), 3, "3 relationships done on first run")
            h.assert_eq(len(cp.failed_relationships), 0, "0 failures on first run")

            # Phase 3: Simulate resume — clear relationships_done, re-run
            cp.relationships_done = set()
            cp.save(cp_path)

            vtp.run_relationships_phase(notes, h.client, cp, cp_path)
            h.assert_eq(len(cp.relationships_done), 3, "3 relationships done on resume (via 409 fallback)")
            h.assert_eq(len(cp.failed_relationships), 0, "0 failures on resume")

            # Verify on server
            for note in notes:
                mem_id = cp.memories.get(note.rel_path)
                if mem_id:
                    rels = h.get_relationships_for(mem_id)
                    h.assert_true(len(rels) >= 1, f"Server has relationships for {note.filename}")


def test_bidirectional_relationships(h: TestHarness) -> None:
    """Scenario 12: A→B and B→A as separate relationships — not treated as duplicates."""
    print("\n[Scenario 12] Bidirectional relationships are distinct")

    m1 = h.create_memory("S12-A")
    m2 = h.create_memory("S12-B")

    # Pre-create A→B
    h.client.create_relationships_bulk([
        {"from_id": m1, "to_id": m2, "relationship_type": "supports"},
    ])

    memory_map = {"s12a.md": m1, "s12b.md": m2}
    relationships = [
        ("s12a.md", "s12b", "supports"),  # duplicate (A→B)
        ("s12b.md", "s12a", "supports"),  # NEW (B→A) — different direction
    ]
    notes = make_notes(memory_map, relationships)

    with tempfile.TemporaryDirectory() as tmp:
        cp, cp_path = make_checkpoint(Path(tmp), memory_map)
        vtp.run_relationships_phase(notes, h.client, cp, cp_path)

        h.assert_eq(len(cp.relationships_done), 2, "Both directions done")
        h.assert_eq(len(cp.failed_relationships), 0, "0 failures")

    # Verify both directions exist on server
    rels = h.get_relationships_for(m1)
    outbound = [r for r in rels if r["from_id"] == m1 and r["to_id"] == m2]
    inbound = [r for r in rels if r["from_id"] == m2 and r["to_id"] == m1]
    h.assert_eq(len(outbound), 1, "A→B exists")
    h.assert_eq(len(inbound), 1, "B→A exists")


def test_large_batch_with_mixed_duplicates(h: TestHarness) -> None:
    """Scenario 13: Larger batch with scattered duplicates — patch batch size to 10."""
    print("\n[Scenario 13] Larger batch with scattered duplicates")

    original_batch_size = vtp.BULK_RELATIONSHIP_BATCH_SIZE
    vtp.BULK_RELATIONSHIP_BATCH_SIZE = 10

    try:
        # Create 8 memories → 7 sequential relationships
        mems = []
        for i in range(8):
            mems.append(h.create_memory(f"S13-{i}"))

        # Pre-create relationships 0→1, 3→4, 6→7 (scattered duplicates)
        h.client.create_relationships_bulk([
            {"from_id": mems[0], "to_id": mems[1], "relationship_type": "supports"},
            {"from_id": mems[3], "to_id": mems[4], "relationship_type": "supports"},
            {"from_id": mems[6], "to_id": mems[7], "relationship_type": "supports"},
        ])

        memory_map = {f"s13-{i}.md": mems[i] for i in range(8)}
        relationships = [
            (f"s13-{i}.md", f"s13-{i+1}", "supports") for i in range(7)
        ]
        notes = make_notes(memory_map, relationships)

        with tempfile.TemporaryDirectory() as tmp:
            cp, cp_path = make_checkpoint(Path(tmp), memory_map)
            vtp.run_relationships_phase(notes, h.client, cp, cp_path)

            h.assert_eq(len(cp.relationships_done), 7, "All 7 relationships done")
            h.assert_eq(len(cp.failed_relationships), 0, "0 failures")

        # Verify on server
        total_server_rels = set()
        for m in mems:
            for r in h.get_relationships_for(m):
                total_server_rels.add(r["id"])
        h.assert_true(len(total_server_rels) >= 7, f"Server has >= 7 unique relationships (got {len(total_server_rels)})")

    finally:
        vtp.BULK_RELATIONSHIP_BATCH_SIZE = original_batch_size


def test_checkpoint_round_trip_after_fallback(h: TestHarness) -> None:
    """Scenario 14: Load checkpoint from disk after fallback, re-run — no API calls needed."""
    print("\n[Scenario 14] Checkpoint round-trip — re-run after fallback is a no-op")

    m1 = h.create_memory("S14-A")
    m2 = h.create_memory("S14-B")

    # Pre-create to force fallback
    h.client.create_relationships_bulk([
        {"from_id": m1, "to_id": m2, "relationship_type": "supports"},
    ])

    memory_map = {"s14a.md": m1, "s14b.md": m2}
    relationships = [("s14a.md", "s14b", "supports")]
    notes = make_notes(memory_map, relationships)

    with tempfile.TemporaryDirectory() as tmp:
        cp, cp_path = make_checkpoint(Path(tmp), memory_map)
        vtp.run_relationships_phase(notes, h.client, cp, cp_path)

        h.assert_eq(len(cp.relationships_done), 1, "Done after fallback")

        # Load from disk and re-run
        cp_loaded = vtp.Checkpoint.load(cp_path)
        cp_loaded.memories = memory_map  # restore (not serialized in relationships phase)

        # Count API calls
        original_bulk = h.client.create_relationships_bulk
        call_count = [0]
        def counting_bulk(payloads):
            call_count[0] += 1
            return original_bulk(payloads)

        h.client.create_relationships_bulk = counting_bulk
        try:
            vtp.run_relationships_phase(notes, h.client, cp_loaded, cp_path)
            h.assert_eq(call_count[0], 0, "Zero API calls on re-run (all already done)")
        finally:
            h.client.create_relationships_bulk = original_bulk


def test_crash_mid_batch_and_resume(h: TestHarness) -> None:
    """Scenario 15: Simulate process kill mid-batch, then resume from checkpoint.

    Patches the client to raise SystemExit after the bulk create succeeds
    but BEFORE the checkpoint is saved — simulating a kill -9 at the worst
    possible moment. The server has the relationships, the checkpoint doesn't.
    Resume must recover via the 409 fallback.
    """
    print("\n[Scenario 15] Crash mid-batch and resume from checkpoint")

    m1 = h.create_memory("S15-A")
    m2 = h.create_memory("S15-B")
    m3 = h.create_memory("S15-C")
    m4 = h.create_memory("S15-D")

    original_batch_size = vtp.BULK_RELATIONSHIP_BATCH_SIZE
    vtp.BULK_RELATIONSHIP_BATCH_SIZE = 2  # 2 batches of 2

    try:
        memory_map = {"s15a.md": m1, "s15b.md": m2, "s15c.md": m3, "s15d.md": m4}
        relationships = [
            ("s15a.md", "s15b", "supports"),   # batch 1
            ("s15b.md", "s15c", "follows"),     # batch 1
            ("s15c.md", "s15d", "references"),  # batch 2
            ("s15d.md", "s15a", "supports"),    # batch 2
        ]
        notes = make_notes(memory_map, relationships)

        with tempfile.TemporaryDirectory() as tmp:
            cp_path = Path(tmp) / "checkpoint.json"
            cp, _ = make_checkpoint(Path(tmp), memory_map)

            # Patch: let batch 1 succeed on the server, then "crash" before
            # the checkpoint can record it.
            original_bulk = h.client.create_relationships_bulk
            call_count = [0]

            def crashing_bulk(payloads):
                call_count[0] += 1
                result = original_bulk(payloads)  # Actually commits to server
                if call_count[0] == 1:
                    # Save checkpoint as-is (relationships NOT marked done) then crash
                    cp.save(cp_path)
                    raise SystemExit("simulated crash")
                return result

            h.client.create_relationships_bulk = crashing_bulk

            crashed = False
            try:
                vtp.run_relationships_phase(notes, h.client, cp, cp_path)
            except SystemExit:
                crashed = True

            h.client.create_relationships_bulk = original_bulk

            h.assert_true(crashed, "Process crashed as expected")
            h.assert_eq(len(cp.relationships_done), 0, "Checkpoint has 0 done (crash before save)")

            # Batch 1's relationships ARE on the server
            rels_m1 = h.get_relationships_for(m1)
            h.assert_true(len(rels_m1) >= 1, "Server has batch 1 relationships despite crash")

            # --- RESUME ---
            cp_resumed = vtp.Checkpoint.load(cp_path)
            cp_resumed.memories = memory_map  # Not serialized by relationships phase
            h.assert_eq(len(cp_resumed.relationships_done), 0, "Resumed checkpoint starts at 0 done")

            vtp.run_relationships_phase(notes, h.client, cp_resumed, cp_path)

            h.assert_eq(len(cp_resumed.relationships_done), 4, "All 4 done after resume")
            h.assert_eq(len(cp_resumed.failed_relationships), 0, "0 failures after resume")

            # Verify all 4 relationships exist on server
            all_rels = set()
            for mid in [m1, m2, m3, m4]:
                for r in h.get_relationships_for(mid):
                    all_rels.add(r["id"])
            h.assert_true(len(all_rels) >= 4, f"Server has >= 4 relationships (got {len(all_rels)})")

    finally:
        vtp.BULK_RELATIONSHIP_BATCH_SIZE = original_batch_size


def test_full_scale_batch_100_items(h: TestHarness) -> None:
    """Scenario 16: Full-scale batch with 101 memories and 100 relationships.

    Uses the real BULK_RELATIONSHIP_BATCH_SIZE (100) so the entire set fits
    in one batch. Pre-creates 10 scattered duplicates. Verifies the fallback
    handles all 100 items correctly at production scale.
    """
    print("\n[Scenario 16] Full-scale batch — 100 relationships, 10 scattered duplicates")

    N = 101  # 101 memories → 100 sequential relationships
    DUPES = 10  # every 10th relationship is pre-created

    # Create 101 memories
    mems = []
    for i in range(N):
        mems.append(h.create_memory(f"S16-{i:03d}"))
    print(f"    Created {N} memories")

    # Pre-create every 10th relationship as a duplicate
    dupe_payloads = []
    for i in range(0, 100, 100 // DUPES):
        dupe_payloads.append({
            "from_id": mems[i], "to_id": mems[i + 1],
            "relationship_type": "supports",
        })
    h.client.create_relationships_bulk(dupe_payloads)
    print(f"    Pre-created {len(dupe_payloads)} duplicates")

    memory_map = {f"s16-{i:03d}.md": mems[i] for i in range(N)}
    relationships = [
        (f"s16-{i:03d}.md", f"s16-{i+1:03d}", "supports") for i in range(100)
    ]
    notes = make_notes(memory_map, relationships)

    with tempfile.TemporaryDirectory() as tmp:
        cp, cp_path = make_checkpoint(Path(tmp), memory_map)
        vtp.run_relationships_phase(notes, h.client, cp, cp_path)

        h.assert_eq(len(cp.relationships_done), 100, "All 100 relationships done")
        h.assert_eq(len(cp.failed_relationships), 0, "0 failures")

    # Verify on server: query a sample of memories and count unique
    # relationships. Each interior node has 1 inbound + 1 outbound;
    # endpoints have only 1. With 7 sample points we expect ~12-13
    # unique relationship IDs.
    total_server_rels = set()
    for idx in [0, 1, 25, 50, 75, 98, 99]:
        for r in h.get_relationships_for(mems[idx]):
            total_server_rels.add(r["id"])
    h.assert_true(len(total_server_rels) >= 7, f"Spot-check found >= 7 relationships (got {len(total_server_rels)})")
    print(f"    Verified {len(total_server_rels)} relationships via spot-check")


def test_network_failure_mid_batch_and_resume(h: TestHarness) -> None:
    """Scenario 17: Simulate network failure mid-fallback using iptables.

    Adds a firewall rule to block traffic to the API host during the
    individual fallback, then removes it and resumes. Verifies the circuit
    breaker fires and resume completes the remaining items.

    Requires root/sudo for iptables. Skipped if not available.
    """
    print("\n[Scenario 17] Network failure mid-batch via iptables")

    import subprocess
    import socket

    # Resolve API host IP
    api_host = h.base_url.split("//")[1].split("/")[0]
    try:
        api_ip = socket.gethostbyname(api_host)
    except socket.gaierror:
        print("    SKIP: Cannot resolve API host")
        return

    # Check if we have iptables access
    try:
        subprocess.run(
            ["sudo", "-n", "iptables", "-L", "OUTPUT", "-n"],
            capture_output=True, timeout=5, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        print("    SKIP: No passwordless sudo/iptables access")
        return

    m1 = h.create_memory("S17-A")
    m2 = h.create_memory("S17-B")
    m3 = h.create_memory("S17-C")
    m4 = h.create_memory("S17-D")
    m5 = h.create_memory("S17-E")

    # Pre-create first relationship to force 409 on bulk → individual fallback
    h.client.create_relationships_bulk([
        {"from_id": m1, "to_id": m2, "relationship_type": "supports"},
    ])

    memory_map = {
        "s17a.md": m1, "s17b.md": m2, "s17c.md": m3,
        "s17d.md": m4, "s17e.md": m5,
    }
    relationships = [
        ("s17a.md", "s17b", "supports"),    # duplicate
        ("s17b.md", "s17c", "follows"),      # new
        ("s17c.md", "s17d", "references"),   # new — will fail (network blocked)
        ("s17d.md", "s17e", "supports"),     # new — will fail or be deferred
    ]
    notes = make_notes(memory_map, relationships)

    block_rule = ["sudo", "-n", "iptables", "-I", "OUTPUT", "-d", api_ip, "-j", "REJECT"]
    unblock_rule = ["sudo", "-n", "iptables", "-D", "OUTPUT", "-d", api_ip, "-j", "REJECT"]

    with tempfile.TemporaryDirectory() as tmp:
        cp, cp_path = make_checkpoint(Path(tmp), memory_map)

        # Patch client: after 2 individual items succeed, block the network
        original_bulk = h.client.create_relationships_bulk
        call_count = [0]
        blocked = [False]

        def intercepting_bulk(payloads):
            call_count[0] += 1
            # call 1 = original bulk (will 409)
            # call 2 = individual item 1 (duplicate, 409)
            # call 3 = individual item 2 (new, succeeds)
            # call 4+ = block network before these
            if call_count[0] == 4 and not blocked[0]:
                subprocess.run(block_rule, capture_output=True, timeout=5)
                blocked[0] = True
            return original_bulk(payloads)

        h.client.create_relationships_bulk = intercepting_bulk

        try:
            # Run with network cut — should hit circuit breaker or timeout errors
            vtp.run_relationships_phase(notes, h.client, cp, cp_path)
        except Exception:
            pass  # Network errors may propagate
        finally:
            # ALWAYS unblock, even if the test crashes
            if blocked[0]:
                subprocess.run(unblock_rule, capture_output=True, timeout=5)
            h.client.create_relationships_bulk = original_bulk

        # Checkpoint should have at least the first 2 items done
        cp_after_crash = vtp.Checkpoint.load(cp_path)
        done_count = len(cp_after_crash.relationships_done)
        h.assert_true(done_count >= 2, f"At least 2 items done before network failure (got {done_count})")
        h.assert_true(done_count < 4, f"Not all items done (network was blocked) (got {done_count})")

        # --- RESUME with network restored ---
        cp_after_crash.memories = memory_map
        vtp.run_relationships_phase(notes, h.client, cp_after_crash, cp_path)

        h.assert_eq(len(cp_after_crash.relationships_done), 4, "All 4 done after resume")
        h.assert_eq(len(cp_after_crash.failed_relationships), 0, "0 failures after resume")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Integration tests for bulk relationship fallback")
    parser.add_argument("--api-key", required=True, help="Penfield API key")
    parser.add_argument("--base-url", default="https://api-dev.penfield.app/api/v2",
                        help="Penfield API base URL")
    args = parser.parse_args()

    # Authenticate
    print(f"Authenticating against {args.base_url}...")
    client = vtp.PenfieldClient(
        base_url=args.base_url.rstrip("/").replace("/api/v2", ""),
        api_key=args.api_key,
    )
    # Force token exchange
    client._ensure_token()
    print(f"Authenticated. Tenant: {client._access_token[:40] if client._access_token else 'NONE'}...\n")

    h = TestHarness(client, args.base_url, client._access_token or "")

    try:
        test_happy_path_bulk_succeeds(h)
        test_full_batch_duplicate_resume(h)
        test_partial_batch_duplicate(h)
        test_double_resume(h)
        test_checkpoint_survives_fallback(h)
        test_multi_batch_one_fails(h)
        test_all_relationship_types_survive_fallback(h)
        test_failed_relationships_cleared_on_successful_resume(h)
        test_single_item_batch(h)
        test_empty_relationship_list(h)
        test_vault_import_end_to_end(h)
        test_bidirectional_relationships(h)
        test_large_batch_with_mixed_duplicates(h)
        test_checkpoint_round_trip_after_fallback(h)
        test_crash_mid_batch_and_resume(h)
        test_full_scale_batch_100_items(h)
        test_network_failure_mid_batch_and_resume(h)
    except Exception as e:
        print(f"\n!!! UNEXPECTED EXCEPTION: {e}")
        traceback.print_exc()
        h.failed += 1
        h.errors.append(f"Unexpected exception: {e}")
    finally:
        h.cleanup()

    return h.report()


if __name__ == "__main__":
    sys.exit(main())

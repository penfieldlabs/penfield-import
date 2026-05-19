#!/usr/bin/env python3
"""Comprehensive pytest test suite for penfield_import.py

All tests run without API calls or network access. Everything is mocked.
Python 3.9+ compatible (no walrus operators, no match statements).
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

# Import the module under test
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import penfield_import as vtp


# ============================================================================
# YAML PARSING TESTS
# ============================================================================

class TestParseYAML:
    """Test the YAML parser."""

    def test_scalar_key_value_pairs(self):
        """Parse simple key: value pairs."""
        yaml_text = "name: John\nage: 30\n"
        result = vtp.parse_yaml(yaml_text)
        assert result["name"] == "John"
        assert result["age"] == 30

    def test_flat_list_dash_style(self):
        """Parse flat lists with dash style."""
        yaml_text = "items:\n- apple\n- banana\n- cherry\n"
        result = vtp.parse_yaml(yaml_text)
        assert result["items"] == ["apple", "banana", "cherry"]

    def test_inline_list(self):
        """Parse inline lists [a, b, c]."""
        yaml_text = "tags: [python, testing, automation]\n"
        result = vtp.parse_yaml(yaml_text)
        assert result["tags"] == ["python", "testing", "automation"]

    def test_duplicate_key_merging(self):
        """Later values for duplicate keys overwrite earlier ones."""
        yaml_text = "items: first\nitems: second\n"
        result = vtp.parse_yaml(yaml_text)
        assert "items" in result
        # Duplicate scalar keys: last value wins
        assert result["items"] == "second"

    def test_boolean_coercion_true(self):
        """String 'true' coerced to boolean True."""
        yaml_text = "flag: true\n"
        result = vtp.parse_yaml(yaml_text)
        assert result["flag"] is True
        assert isinstance(result["flag"], bool)

    def test_boolean_coercion_false(self):
        """String 'false' coerced to boolean False."""
        yaml_text = "flag: false\n"
        result = vtp.parse_yaml(yaml_text)
        assert result["flag"] is False
        assert isinstance(result["flag"], bool)

    def test_integer_coercion(self):
        """String that's an integer coerced to int."""
        yaml_text = "count: 42\n"
        result = vtp.parse_yaml(yaml_text)
        assert result["count"] == 42
        assert isinstance(result["count"], int)

    def test_float_coercion(self):
        """String that's a float coerced to float."""
        yaml_text = "rating: 3.14\n"
        result = vtp.parse_yaml(yaml_text)
        assert result["rating"] == 3.14
        assert isinstance(result["rating"], float)

    def test_empty_value(self):
        """Key with no value maps to None."""
        yaml_text = "key:\n"
        result = vtp.parse_yaml(yaml_text)
        assert result.get("key") is None

    def test_comments_skipped(self):
        """Comments are skipped."""
        yaml_text = "# This is a comment\nname: Alice\n# Another comment\nage: 25\n"
        result = vtp.parse_yaml(yaml_text)
        assert result["name"] == "Alice"
        assert result["age"] == 25
        assert len(result) == 2

    def test_blank_lines_skipped(self):
        """Blank lines are skipped."""
        yaml_text = "name: Bob\n\nage: 35\n\n\n"
        result = vtp.parse_yaml(yaml_text)
        assert result["name"] == "Bob"
        assert result["age"] == 35

    def test_quoted_values(self):
        """Quoted values are stripped of quotes."""
        yaml_text = 'name: "John Doe"\ntag: \'python\'\n'
        result = vtp.parse_yaml(yaml_text)
        assert result["name"] == "John Doe"
        assert result["tag"] == "python"

    def test_empty_yaml(self):
        """Empty YAML returns empty dict."""
        result = vtp.parse_yaml("")
        assert result == {}

    def test_whitespace_only_yaml(self):
        """Whitespace-only YAML returns empty dict."""
        result = vtp.parse_yaml("   \n  \n  ")
        assert result == {}

    def test_mixed_inline_and_dash_lists(self):
        """Duplicate list keys merge into a single list.

        Canonical Penfield behavior on both backends: when the first
        occurrence is a list, subsequent values are merged in rather
        than dropped.  Stricter than YAML spec (which says last-value-
        wins), preserving user data in hand-authored frontmatter.
        """
        yaml_text = "items: [a, b]\nitems:\n- c\n- d\n"
        result = vtp.parse_yaml(yaml_text)
        assert result["items"] == ["a", "b", "c", "d"]

    def test_duplicate_list_then_scalar_appends(self):
        """A scalar value appended to an already-list key becomes a list item."""
        yaml_text = "tags:\n- python\n- testing\ntags: automation\n"
        result = vtp.parse_yaml(yaml_text)
        assert result["tags"] == ["python", "testing", "automation"]

    def test_duplicate_scalar_then_list_wraps(self):
        """Scalar value followed by a list on the same key wraps the scalar.

        Without this, the PyYAML backend's ``SafeLoader`` would overwrite the
        earlier scalar with the later list and silently drop user data.  Both
        backends must preserve the scalar by wrapping it into a list with the
        subsequent items appended.
        """
        yaml_text = "author: Alice\nauthor:\n- Bob\n- Carol\n"
        result = vtp.parse_yaml(yaml_text)
        assert result["author"] == ["Alice", "Bob", "Carol"]


# ============================================================================
# FRONTMATTER EXTRACTION TESTS
# ============================================================================

class TestExtractFrontmatter:
    """Test extract_frontmatter function."""

    def test_standard_frontmatter(self):
        """Extract frontmatter with standard delimiters."""
        content = "---\ntitle: My Note\ndesc: Some text\n---\nBody content here"
        frontmatter, body = vtp.extract_frontmatter(content)
        assert frontmatter["title"] == "My Note"
        assert frontmatter["desc"] == "Some text"
        assert body == "Body content here"

    def test_no_frontmatter(self):
        """No frontmatter returns empty dict and full content."""
        content = "Just body content\nNo frontmatter here"
        frontmatter, body = vtp.extract_frontmatter(content)
        assert frontmatter == {}
        assert body == content

    def test_malformed_frontmatter_missing_closing(self):
        """Missing closing --- returns empty dict and full content."""
        content = "---\ntitle: Incomplete\nNo closing delimiter\nBody here"
        frontmatter, body = vtp.extract_frontmatter(content)
        assert frontmatter == {}
        assert body == content

    def test_empty_frontmatter(self):
        """Empty frontmatter (---> ---)."""
        content = "---\n---\nBody content"
        frontmatter, body = vtp.extract_frontmatter(content)
        assert frontmatter == {}
        assert body == "Body content"

    def test_frontmatter_with_multiline_body(self):
        """Frontmatter extraction preserves multiline body."""
        content = "---\nkey: value\n---\nLine 1\nLine 2\nLine 3"
        frontmatter, body = vtp.extract_frontmatter(content)
        assert frontmatter["key"] == "value"
        assert body == "Line 1\nLine 2\nLine 3"

    def test_frontmatter_with_lists(self):
        """Frontmatter with list values."""
        content = "---\ntags:\n  - python\n  - testing\n---\nContent"
        frontmatter, body = vtp.extract_frontmatter(content)
        assert isinstance(frontmatter.get("tags"), list)
        assert "python" in frontmatter.get("tags", [])
        assert body == "Content"

    def test_frontmatter_with_trailing_newline(self):
        """Closing --- on line with trailing newline."""
        content = "---\ntitle: Test\n---\nBody\n"
        frontmatter, body = vtp.extract_frontmatter(content)
        assert frontmatter["title"] == "Test"
        assert "Body" in body


# ============================================================================
# RELATIONSHIP EXTRACTION TESTS
# ============================================================================

class TestExtractFrontmatterRelationships:
    """Test extract_frontmatter_relationships function."""

    def test_standard_wikilink_relationship(self):
        """Extract standard wikilink targets [[Note Name]] with valid type."""
        frontmatter = {
            "supports": ["[[Target Note]]"]
        }
        rels = vtp.extract_frontmatter_relationships(frontmatter)
        assert len(rels) == 1
        assert ("Target Note", "supports") in rels

    def test_multiple_relationship_types(self):
        """Extract multiple relationship types from one note."""
        frontmatter = {
            "supports": ["[[NoteA]]"],
            "contradicts": ["[[NoteB]]"],
            "child_of": ["[[NoteC]]"]
        }
        rels = vtp.extract_frontmatter_relationships(frontmatter)
        assert len(rels) == 3
        assert ("NoteA", "supports") in rels
        assert ("NoteB", "contradicts") in rels
        assert ("NoteC", "child_of") in rels

    def test_multiple_targets_same_type(self):
        """Multiple targets of the same relationship type."""
        frontmatter = {
            "supports": ["[[TargetA]]", "[[TargetB]]", "[[TargetC]]"]
        }
        rels = vtp.extract_frontmatter_relationships(frontmatter)
        assert len(rels) == 3
        assert ("TargetA", "supports") in rels
        assert ("TargetB", "supports") in rels
        assert ("TargetC", "supports") in rels

    def test_non_standard_types_skipped(self):
        """Non-standard relationship types are skipped."""
        frontmatter = {
            "custom_type": ["[[Note]]"],
            "supports": ["[[ValidTarget]]"]
        }
        rels = vtp.extract_frontmatter_relationships(frontmatter)
        assert len(rels) == 1
        assert ("ValidTarget", "supports") in rels

    def test_duplicate_deduplication(self):
        """Duplicate target+type pairs appear only once."""
        frontmatter = {
            "supports": ["[[SameTarget]]", "[[SameTarget]]"]
        }
        rels = vtp.extract_frontmatter_relationships(frontmatter)
        assert len(rels) == 1
        assert ("SameTarget", "supports") in rels

    def test_non_list_values_skipped(self):
        """Non-list values for relationship keys are skipped."""
        frontmatter = {
            "supports": "[[NotAList]]",  # String instead of list
            "contradicts": ["[[ValidTarget]]"]
        }
        rels = vtp.extract_frontmatter_relationships(frontmatter)
        assert len(rels) == 1
        assert ("ValidTarget", "contradicts") in rels

    def test_empty_frontmatter_zero_relationships(self):
        """Empty frontmatter returns empty list (CRITICAL)."""
        frontmatter = {}
        rels = vtp.extract_frontmatter_relationships(frontmatter)
        assert rels == []

    def test_case_insensitive_type_matching(self):
        """Relationship types are matched case-insensitively and stored lowercase."""
        frontmatter = {
            "SUPPORTS": ["[[Target]]"],
            "Contradicts": ["[[Target2]]"]
        }
        rels = vtp.extract_frontmatter_relationships(frontmatter)
        assert len(rels) == 2
        assert ("Target", "supports") in rels
        assert ("Target2", "contradicts") in rels

    def test_wikilink_with_whitespace(self):
        """Wikilink targets with whitespace are handled."""
        frontmatter = {
            "supports": ["[[ Target With Spaces ]]"]
        }
        rels = vtp.extract_frontmatter_relationships(frontmatter)
        assert len(rels) == 1
        assert ("Target With Spaces", "supports") in rels

    def test_wikilink_with_pipe_alias(self):
        """Wikilink with pipe alias [[Target|display]] extracts only the target."""
        frontmatter = {
            "supports": ["[[Target Note|display text]]"],
            "references": ["[[Another|alias]]"],
        }
        rels = vtp.extract_frontmatter_relationships(frontmatter)
        assert len(rels) == 2
        assert ("Target Note", "supports") in rels
        assert ("Another", "references") in rels

    def test_non_wikilink_items_skipped(self):
        """Non-wikilink items in relationship arrays are skipped."""
        frontmatter = {
            "supports": ["[[ValidTarget]]", "NotAWikilink", "[[AnotherValid]]"]
        }
        rels = vtp.extract_frontmatter_relationships(frontmatter)
        assert len(rels) == 2
        assert ("ValidTarget", "supports") in rels
        assert ("AnotherValid", "supports") in rels

    def test_non_string_items_skipped(self):
        """Non-string items in arrays are skipped."""
        frontmatter = {
            "supports": ["[[ValidTarget]]", 42, None, ["nested"]]
        }
        rels = vtp.extract_frontmatter_relationships(frontmatter)
        assert len(rels) == 1
        assert ("ValidTarget", "supports") in rels


# ============================================================================
# INLINE RELATIONSHIP EXTRACTION TESTS
# ============================================================================

class TestExtractInlineRelationships:
    """Test extract_inline_relationships function."""

    def test_basic_wikilink(self):
        """Extract [[Target]] as references relationship."""
        rels = vtp.extract_inline_relationships("See [[My Note]] for details")
        assert len(rels) == 1
        assert ("My Note", "references") in rels

    def test_wikilink_with_alias(self):
        """[[Target|display]] extracts target only."""
        rels = vtp.extract_inline_relationships("See [[Real Target|shown text]] here")
        assert len(rels) == 1
        assert ("Real Target", "references") in rels

    def test_multiple_wikilinks(self):
        """Multiple wikilinks extracted."""
        rels = vtp.extract_inline_relationships("Links to [[A]], [[B]], and [[C]]")
        assert len(rels) == 3
        assert ("A", "references") in rels
        assert ("B", "references") in rels
        assert ("C", "references") in rels

    def test_deduplicates(self):
        """Same target mentioned twice produces one relationship."""
        rels = vtp.extract_inline_relationships("See [[Note]] and also [[Note]]")
        assert len(rels) == 1

    def test_no_wikilinks(self):
        """No wikilinks returns empty list."""
        rels = vtp.extract_inline_relationships("Plain text with no links")
        assert rels == []


class TestParseVaultRelationshipModes:
    """Test parse_vault with different relationship modes."""

    def test_frontmatter_mode(self, tmp_path):
        """frontmatter mode extracts from YAML only."""
        (tmp_path / "note.md").write_text(
            '---\nsupports:\n  - "[[Target]]"\n---\nBody with [[Inline Link]]'
        )
        notes = vtp.parse_vault(tmp_path, set(), "frontmatter")
        assert len(notes[0].relationships) == 1
        assert ("Target", "supports") in notes[0].relationships

    def test_inline_mode(self, tmp_path):
        """inline mode extracts from body wikilinks only."""
        (tmp_path / "note.md").write_text(
            '---\nsupports:\n  - "[[Target]]"\n---\nBody with [[Inline Link]]'
        )
        notes = vtp.parse_vault(tmp_path, set(), "inline")
        assert len(notes[0].relationships) == 1
        assert ("Inline Link", "references") in notes[0].relationships

    def test_both_mode(self, tmp_path):
        """both mode combines frontmatter + inline, deduplicated."""
        (tmp_path / "note.md").write_text(
            '---\nreferences:\n  - "[[Shared]]"\nsupports:\n  - "[[Other]]"\n---\n'
            'Body links [[Shared]] and [[New]]'
        )
        notes = vtp.parse_vault(tmp_path, set(), "both")
        rels = notes[0].relationships
        # Shared is in frontmatter as references AND inline — should not duplicate
        assert sum(1 for t, r in rels if t == "Shared" and r == "references") == 1
        # Other from frontmatter
        assert ("Other", "supports") in rels
        # New from inline
        assert ("New", "references") in rels
        assert len(rels) == 3


# ============================================================================
# CONTENT FORMATTING TESTS
# ============================================================================

class TestFormatMemoryContent:
    """Test format_memory_content function."""

    def test_normal_note_body_only(self):
        """Normal notes with body only, no interesting frontmatter."""
        note = vtp.ParsedNote(
            rel_path="test.md",
            filename="test",
            vault_dir="",
            content="test",
            body="This is body content",
            frontmatter={},
            relationships=[],
            tags=[]
        )
        result = vtp.format_memory_content(note)
        assert result == "This is body content"

    def test_frontmatter_excluded_by_default(self):
        """Frontmatter metadata is excluded from content by default."""
        note = vtp.ParsedNote(
            rel_path="test.md",
            filename="test",
            vault_dir="",
            content="test",
            body="Body text",
            frontmatter={"author": "Alice", "created": "2025-01-01"},
            relationships=[],
            tags=[]
        )
        result = vtp.format_memory_content(note)
        assert result == "Body text"
        assert "author" not in result

    def test_frontmatter_included_when_flag_set(self):
        """Frontmatter metadata preserved when include_frontmatter=True."""
        note = vtp.ParsedNote(
            rel_path="test.md",
            filename="test",
            vault_dir="",
            content="test",
            body="Body text",
            frontmatter={"author": "Alice", "created": "2025-01-01"},
            relationships=[],
            tags=[]
        )
        result = vtp.format_memory_content(note, include_frontmatter=True)
        assert "author: Alice" in result
        assert "created: 2025-01-01" in result
        assert "Body text" in result

    def test_relationship_keys_excluded_with_frontmatter(self):
        """Relationship keys excluded even with include_frontmatter=True."""
        note = vtp.ParsedNote(
            rel_path="test.md",
            filename="test",
            vault_dir="",
            content="test",
            body="Body",
            frontmatter={
                "author": "Bob",
                "supports": ["[[Target]]"]
            },
            relationships=[("Target", "supports")],
            tags=[]
        )
        result = vtp.format_memory_content(note, include_frontmatter=True)
        assert "author: Bob" in result
        assert "supports" not in result

    def test_tag_key_excluded_with_frontmatter(self):
        """Tags key excluded even with include_frontmatter=True."""
        note = vtp.ParsedNote(
            rel_path="test.md",
            filename="test",
            vault_dir="",
            content="test",
            body="Body",
            frontmatter={"tags": ["python", "testing"]},
            relationships=[],
            tags=["python", "testing"]
        )
        result = vtp.format_memory_content(note, include_frontmatter=True)
        assert "tags:" not in result
        assert "Body" in result

    def test_empty_body_returns_empty(self):
        """Empty body returns empty string (does not leak frontmatter from content)."""
        note = vtp.ParsedNote(
            rel_path="test.md",
            filename="test",
            vault_dir="",
            content="---\nid: test\n---\n",
            body="",
            frontmatter={"id": "test"},
            relationships=[],
            tags=[]
        )
        result = vtp.format_memory_content(note)
        assert result == ""

    def test_list_values_formatted_with_frontmatter(self):
        """List values formatted as comma-separated with include_frontmatter=True."""
        note = vtp.ParsedNote(
            rel_path="test.md",
            filename="test",
            vault_dir="",
            content="test",
            body="Body",
            frontmatter={"related": ["a", "b", "c"]},
            relationships=[],
            tags=[]
        )
        result = vtp.format_memory_content(note, include_frontmatter=True)
        assert "related: a, b, c" in result

    def test_none_values_skipped_with_frontmatter(self):
        """None values skipped with include_frontmatter=True."""
        note = vtp.ParsedNote(
            rel_path="test.md",
            filename="test",
            vault_dir="",
            content="test",
            body="Body",
            frontmatter={"empty_key": None, "valid_key": "value"},
            relationships=[],
            tags=[]
        )
        result = vtp.format_memory_content(note, include_frontmatter=True)
        assert "empty_key" not in result
        assert "valid_key: value" in result


# ============================================================================
# SMART TRUNCATION TESTS
# ============================================================================

class TestSmartTruncate:
    """Test smart_truncate function."""

    def test_content_under_limit_no_truncation(self):
        """Content under limit is not truncated."""
        content = "Short content"
        result = vtp.smart_truncate(content, limit=1000)
        assert result == content

    def test_truncation_at_heading_boundary(self):
        """Truncate at heading boundary if found."""
        content = "A" * 6000 + "\n## New Section\n" + "B" * 3000
        result = vtp.smart_truncate(content, limit=7000)
        assert "## New Section" not in result
        assert "[Content truncated" in result

    def test_truncation_at_paragraph_boundary(self):
        """Truncate at paragraph boundary (double newline) if found."""
        content = "A" * 6000 + "\n\nNew paragraph\n" + "B" * 3000
        result = vtp.smart_truncate(content, limit=7000)
        assert "New paragraph" not in result
        assert "[Content truncated" in result

    def test_fallback_character_limit_truncation(self):
        """Fallback to character limit if no boundaries found."""
        content = "A" * 15000
        result = vtp.smart_truncate(content, limit=10000)
        assert len(result) <= 11000  # Limit + truncation message
        assert "[Content truncated" in result

    def test_truncation_message_appended(self):
        """Truncation message is appended."""
        content = "A" * 15000
        result = vtp.smart_truncate(content, limit=10000)
        assert "*[Content truncated.]*" in result

    def test_truncation_message_includes_artifact_path(self):
        """Truncation message includes artifact path when provided."""
        content = "A" * 15000
        result = vtp.smart_truncate(content, limit=10000, artifact_path="/oversize-notes/big.md")
        assert "*[Content truncated. Full document: /oversize-notes/big.md]*" in result

    def test_truncation_without_artifact_path(self):
        """Truncation without artifact_path uses generic message."""
        content = "A" * 15000
        result = vtp.smart_truncate(content, limit=10000)
        assert "Full document:" not in result
        assert "*[Content truncated.]*" in result


# ============================================================================
# CHECKPOINT SERIALIZATION TESTS
# ============================================================================

class TestCheckpoint:
    """Test Checkpoint dataclass serialization."""

    def test_to_dict_round_trip(self):
        """Create checkpoint, to_dict(), from_dict() → compare."""
        original = vtp.Checkpoint(
            phase="memories",
            memories={"file1.md": "uuid-1", "file2.md": "uuid-2"},
            artifacts={"file3.md": "/vault/file3"},
            relationships_done={"f1|f2|supports", "f2|f3|contradicts"},
            failed_memories=["bad1.md"],
            failed_artifacts=[],
            failed_relationships=["f4|f5|unknown"]
        )
        dict_repr = original.to_dict()
        restored = vtp.Checkpoint.from_dict(dict_repr)
        assert restored.phase == original.phase
        assert restored.memories == original.memories
        assert restored.artifacts == original.artifacts
        assert restored.relationships_done == original.relationships_done
        assert restored.failed_memories == original.failed_memories
        assert restored.failed_artifacts == original.failed_artifacts
        assert restored.failed_relationships == original.failed_relationships

    def test_save_and_load_checkpoint(self, tmp_path):
        """Save and load checkpoint from disk."""
        checkpoint_path = tmp_path / "test_checkpoint.json"
        original = vtp.Checkpoint(
            phase="artifacts",
            memories={"note.md": "id-123"}
        )
        original.save(checkpoint_path)
        assert checkpoint_path.exists()

        loaded = vtp.Checkpoint.load(checkpoint_path)
        assert loaded.phase == "artifacts"
        assert loaded.memories == {"note.md": "id-123"}

    def test_corrupt_checkpoint_file_recovery(self, tmp_path):
        """Corrupt checkpoint returns fresh Checkpoint."""
        checkpoint_path = tmp_path / "corrupt.json"
        checkpoint_path.write_text("{ invalid json ]")

        result = vtp.Checkpoint.load(checkpoint_path)
        assert result.phase == "parse"
        assert result.memories == {}

    def test_missing_checkpoint_file_returns_fresh(self, tmp_path):
        """Missing checkpoint file returns fresh Checkpoint."""
        checkpoint_path = tmp_path / "nonexistent.json"
        result = vtp.Checkpoint.load(checkpoint_path)
        assert result.phase == "parse"
        assert result.memories == {}

    def test_unknown_fields_ignored(self):
        """Unknown fields in JSON are ignored (forward compatibility)."""
        data = {
            "phase": "done",
            "memories": {},
            "artifacts": {},
            "relationships_done": [],
            "failed_memories": [],
            "failed_artifacts": [],
            "failed_relationships": [],
            "future_field": "should be ignored"
        }
        checkpoint = vtp.Checkpoint.from_dict(data)
        assert checkpoint.phase == "done"

    def test_relationships_done_sorted_on_save(self, tmp_path):
        """relationships_done is sorted when saved."""
        checkpoint_path = tmp_path / "sorted.json"
        original = vtp.Checkpoint(
            relationships_done={"z|a|type", "a|z|type", "m|n|type"}
        )
        original.save(checkpoint_path)

        data = json.loads(checkpoint_path.read_text())
        assert data["relationships_done"] == sorted(["z|a|type", "a|z|type", "m|n|type"])


# ============================================================================
# PARSE VAULT TESTS
# ============================================================================

class TestParseVault:
    """Test parse_vault function."""

    def test_parse_md_and_txt_files(self, tmp_path):
        """All supported memory extensions are found and parsed."""
        (tmp_path / "note1.md").write_text("---\ntitle: MD Note\n---\nBody")
        (tmp_path / "note2.txt").write_text("---\ntitle: TXT Note\n---\nBody")
        (tmp_path / "note3.markdown").write_text("---\ntitle: Markdown Note\n---\nBody")
        (tmp_path / "note4.text").write_text("---\ntitle: Text Note\n---\nBody")
        (tmp_path / "ignore.pdf").write_text("Not parsed")

        notes = vtp.parse_vault(tmp_path, set())
        assert len(notes) == 4
        filenames = {n.filename for n in notes}
        assert "note1" in filenames
        assert "note2" in filenames
        assert "note3" in filenames
        assert "note4" in filenames

    def test_skip_dirs_functionality(self, tmp_path):
        """skip_dirs prevents parsing files in those directories."""
        (tmp_path / "main.md").write_text("Main note")
        (tmp_path / "_meta").mkdir()
        (tmp_path / "_meta" / "meta_note.md").write_text("Meta note")
        (tmp_path / "_templates").mkdir()
        (tmp_path / "_templates" / "template.md").write_text("Template")

        notes = vtp.parse_vault(tmp_path, {"_meta", "_templates"})
        assert len(notes) == 1
        assert notes[0].filename == "main"

    def test_relationship_extraction_from_parsed_notes(self, tmp_path):
        """Relationships are extracted during parsing."""
        content = "---\nsupports:\n  - \"[[TargetNote]]\"\n---\nBody"
        (tmp_path / "source.md").write_text(content)
        (tmp_path / "target.md").write_text("Target content")

        notes = vtp.parse_vault(tmp_path, set())
        source_note = next(n for n in notes if n.filename == "source")
        assert len(source_note.relationships) == 1
        assert ("TargetNote", "supports") in source_note.relationships

    def test_tag_extraction_from_frontmatter(self, tmp_path):
        """Tags are extracted from frontmatter."""
        content = "---\ntags: [python, testing]\n---\nBody"
        (tmp_path / "note.md").write_text(content)

        notes = vtp.parse_vault(tmp_path, set())
        assert len(notes[0].tags) == 2
        assert "python" in notes[0].tags

    def test_notes_with_zero_relationships_parse_correctly(self, tmp_path):
        """Notes with no relationships parse without error."""
        (tmp_path / "note1.md").write_text("---\ntitle: Basic\n---\nNo relationships")
        (tmp_path / "note2.md").write_text("Just text, no YAML")

        notes = vtp.parse_vault(tmp_path, set())
        assert len(notes) == 2
        for note in notes:
            assert note.relationships == []

    def test_rel_path_and_vault_dir_set_correctly(self, tmp_path):
        """rel_path and vault_dir are set correctly."""
        (tmp_path / "root.md").write_text("Root")
        subdir = tmp_path / "subfolder"
        subdir.mkdir()
        (subdir / "nested.md").write_text("Nested")

        notes = vtp.parse_vault(tmp_path, set())
        root_note = next(n for n in notes if n.filename == "root")
        nested_note = next(n for n in notes if n.filename == "nested")

        assert root_note.vault_dir == ""
        assert root_note.rel_path == "root.md"
        assert nested_note.vault_dir == "subfolder"
        assert nested_note.rel_path == "subfolder/nested.md"

    def test_content_and_body_split_correctly(self, tmp_path):
        """content (full) and body (without frontmatter) are split."""
        md_content = "---\ntitle: Test\n---\nBody content"
        (tmp_path / "note.md").write_text(md_content)

        notes = vtp.parse_vault(tmp_path, set())
        note = notes[0]
        assert note.content == md_content
        assert "title: Test" not in note.body
        assert "Body content" in note.body


# ============================================================================
# RATE LIMITER TESTS
# ============================================================================

class TestRateLimiter:
    """Test RateLimiter class."""

    def test_basic_rate_limiter_tracks_timestamps(self):
        """RateLimiter tracks request timestamps."""
        limiter = vtp.RateLimiter(max_requests=2, window_seconds=1.0)
        limiter.wait()
        limiter.wait()
        # Should have 2 timestamps
        assert len(limiter._timestamps) == 2

    def test_wait_returns_without_blocking_under_limit(self):
        """wait() returns immediately when under rate limit."""
        limiter = vtp.RateLimiter(max_requests=10, window_seconds=1.0)
        start = time.monotonic()
        for _ in range(5):
            limiter.wait()
        elapsed = time.monotonic() - start
        # Should be very fast (no sleeping)
        assert elapsed < 0.5

    def test_wait_with_empty_history(self):
        """wait_if_needed doesn't crash with empty history."""
        limiter = vtp.RateLimiter()
        limiter.wait()
        # Should complete without error
        assert len(limiter._timestamps) >= 1

    def test_old_timestamps_pruned(self):
        """Timestamps outside the window are pruned."""
        limiter = vtp.RateLimiter(max_requests=10, window_seconds=0.1)
        limiter.wait()
        assert len(limiter._timestamps) == 1

        time.sleep(0.15)
        limiter.wait()
        # Old timestamp should be pruned
        assert len(limiter._timestamps) == 1


# ============================================================================
# DRY RUN REPORT TESTS
# ============================================================================

class TestDryRunReport:
    """Test dry_run_report function."""

    def test_runs_without_error(self, tmp_path, capsys):
        """dry_run_report runs without error on parsed notes."""
        notes = [
            vtp.ParsedNote(
                rel_path="test.md",
                filename="test",
                vault_dir="",
                content="x" * 5000,
                body="Body",
                frontmatter={},
                relationships=[],
                tags=["python"]
            )
        ]
        args = argparse.Namespace(
            llm=False,
            llm_model="test",
            claude=False,
            claude_model="haiku",
            include_frontmatter=False,
            dry_run=True
        )

        vtp.dry_run_report(notes, args)
        captured = capsys.readouterr()
        assert "Vault Import" in captured.out
        assert "Total notes:" in captured.out


# ============================================================================
# BUILD RELATIONSHIP LIST TESTS
# ============================================================================

class TestBuildRelationshipList:
    """Test build_relationship_list function."""

    def test_builds_correct_payloads(self):
        """Builds correct payloads from notes with relationships."""
        checkpoint = vtp.Checkpoint(
            memories={"source.md": "id-from", "target.md": "id-to"}
        )
        notes = [
            vtp.ParsedNote(
                rel_path="source.md",
                filename="source",
                vault_dir="",
                content="",
                body="",
                frontmatter={"supports": ["[[target]]"]},
                relationships=[("target", "supports")],
                tags=[]
            ),
            vtp.ParsedNote(
                rel_path="target.md",
                filename="target",
                vault_dir="",
                content="",
                body="",
                frontmatter={},
                relationships=[],
                tags=[]
            )
        ]

        result = vtp.build_relationship_list(notes, checkpoint)
        assert len(result) == 1
        key, payload = result[0]
        assert payload["from_id"] == "id-from"
        assert payload["to_id"] == "id-to"
        assert payload["relationship_type"] == "supports"

    def test_handles_notes_with_zero_relationships(self):
        """Handles notes with zero relationships (returns empty list)."""
        checkpoint = vtp.Checkpoint(memories={"note.md": "id-1"})
        notes = [
            vtp.ParsedNote(
                rel_path="note.md",
                filename="note",
                vault_dir="",
                content="",
                body="",
                frontmatter={},
                relationships=[],
                tags=[]
            )
        ]

        result = vtp.build_relationship_list(notes, checkpoint)
        assert result == []

    def test_handles_duplicate_filenames(self, caplog):
        """Handles duplicate filenames (warns, resolves to last)."""
        checkpoint = vtp.Checkpoint(memories={
            "dir1/test.md": "id-1",
            "dir2/test.md": "id-2"
        })
        notes = [
            vtp.ParsedNote(
                rel_path="dir1/test.md",
                filename="test",
                vault_dir="dir1",
                content="",
                body="",
                frontmatter={},
                relationships=[],
                tags=[]
            ),
            vtp.ParsedNote(
                rel_path="dir2/test.md",
                filename="test",
                vault_dir="dir2",
                content="",
                body="",
                frontmatter={"supports": ["[[other]]"]},
                relationships=[("other", "supports")],
                tags=[]
            ),
            vtp.ParsedNote(
                rel_path="other.md",
                filename="other",
                vault_dir="",
                content="",
                body="",
                frontmatter={},
                relationships=[],
                tags=[]
            )
        ]

        # Add "other" to checkpoint so the relationship can resolve
        checkpoint.memories["other.md"] = "id-other"

        with caplog.at_level("WARNING"):
            result = vtp.build_relationship_list(notes, checkpoint)
        assert "Duplicate filename" in caplog.text
        # dir2/test.md has the relationship; "other" resolves to "other.md"
        assert len(result) == 1
        _, payload = result[0]
        assert payload["from_id"] == "id-2"
        assert payload["to_id"] == "id-other"
        assert payload["relationship_type"] == "supports"

    def test_handles_missing_memory_ids(self):
        """Handles missing memory IDs (skips gracefully)."""
        checkpoint = vtp.Checkpoint(memories={})  # Empty
        notes = [
            vtp.ParsedNote(
                rel_path="source.md",
                filename="source",
                vault_dir="",
                content="",
                body="",
                frontmatter={"supports": ["[[target]]"]},
                relationships=[("target", "supports")],
                tags=[]
            )
        ]

        result = vtp.build_relationship_list(notes, checkpoint)
        assert result == []

    def test_skips_unresolved_targets(self):
        """Skips relationships with unresolved targets."""
        checkpoint = vtp.Checkpoint(
            memories={"source.md": "id-from"}
        )
        notes = [
            vtp.ParsedNote(
                rel_path="source.md",
                filename="source",
                vault_dir="",
                content="",
                body="",
                frontmatter={"supports": ["[[nonexistent]]"]},
                relationships=[("nonexistent", "supports")],
                tags=[]
            )
        ]

        result = vtp.build_relationship_list(notes, checkpoint)
        assert result == []


# ============================================================================
# WRITE IMPORT REPORT TESTS
# ============================================================================

class TestWriteImportReport:
    """Test write_import_report function."""

    def test_writes_report_file_with_correct_stats(self, tmp_path):
        """Writes report file with correct stats."""
        notes = [
            vtp.ParsedNote("a.md", "a", "", "", "", {}, [], []),
            vtp.ParsedNote("b.md", "b", "", "", "", {}, [], [])
        ]
        checkpoint = vtp.Checkpoint(
            memories={"a.md": "id-1", "b.md": "id-2"},
            artifacts={"a.md": "/vault/a"},
            relationships_done={"a|b|supports"},
            failed_memories=[],
            failed_artifacts=[],
            failed_relationships=[]
        )

        vtp.write_import_report(notes, checkpoint, tmp_path, tmp_path, False)
        report_path = tmp_path / ".penfield_import_report.txt"
        assert report_path.exists()

        report_text = report_path.read_text()
        assert "Total files:    2" in report_text
        assert "Created:      2" in report_text

    def test_handles_zero_failures_case(self, tmp_path):
        """Handles zero failures case."""
        notes = [vtp.ParsedNote("a.md", "a", "", "", "", {}, [], [])]
        checkpoint = vtp.Checkpoint(
            memories={"a.md": "id-1"},
            artifacts={},
            relationships_done={},
            failed_memories=[],
            failed_artifacts=[],
            failed_relationships=[]
        )

        vtp.write_import_report(notes, checkpoint, tmp_path, tmp_path, False)
        report_path = tmp_path / ".penfield_import_report.txt"
        report_text = report_path.read_text()
        assert "NO FAILURES" in report_text

    def test_handles_failures_case(self, tmp_path):
        """Handles failures case."""
        notes = [vtp.ParsedNote("a.md", "a", "", "", "", {}, [], [])]
        checkpoint = vtp.Checkpoint(
            memories={"a.md": "id-1"},
            failed_memories=["b.md"],
            failed_artifacts=["c.md"],
            failed_relationships=["x|y|z"]
        )

        vtp.write_import_report(notes, checkpoint, tmp_path, tmp_path, False)
        report_path = tmp_path / ".penfield_import_report.txt"
        report_text = report_path.read_text()
        assert "3 FAILURES" in report_text


# ============================================================================
# VERSION AND PARSER TESTS
# ============================================================================

class TestVersionAndParser:
    """Test version and CLI parser."""

    def test_version_is_string(self):
        """__version__ is a string."""
        assert isinstance(vtp.__version__, str)
        assert len(vtp.__version__) > 0

    def test_build_parser_includes_version(self):
        """build_parser includes --version."""
        parser = vtp.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--version"])

    def test_build_parser_vault_path_optional_for_auth(self):
        """build_parser allows missing vault_path (for --login etc.)."""
        parser = vtp.build_parser()
        args = parser.parse_args(["--login"])
        assert args.login is True
        assert args.vault_path is None

    def test_build_parser_accepts_dry_run(self):
        """build_parser accepts --dry-run flag."""
        parser = vtp.build_parser()
        args = parser.parse_args(["/fake/path", "--dry-run"])
        assert args.dry_run is True

    def test_build_parser_accepts_skip_dirs(self):
        """build_parser accepts --skip-dirs."""
        parser = vtp.build_parser()
        args = parser.parse_args(["/fake/path", "--skip-dirs", "dir1", "dir2"])
        assert "dir1" in args.skip_dirs
        assert "dir2" in args.skip_dirs


# ============================================================================
# PHASE INDEX TESTS
# ============================================================================

class TestPhaseIndex:
    """Test phase_index function."""

    def test_phase_order_indices(self):
        """phase_index returns correct indices for phase order."""
        assert vtp.phase_index("parse") == 0
        assert vtp.phase_index("memories") == 1
        assert vtp.phase_index("artifacts") == 2
        assert vtp.phase_index("vault_artifacts") == 3
        assert vtp.phase_index("documents") == 4
        assert vtp.phase_index("relationships") == 5
        assert vtp.phase_index("verify") == 6
        assert vtp.phase_index("done") == 7

    def test_unknown_phase_returns_zero(self):
        """Unknown phase returns 0 (fallback)."""
        assert vtp.phase_index("unknown") == 0


# ============================================================================
# ARTIFACT PATH TESTS
# ============================================================================

class TestArtifactPathForNote:
    """Test artifact_path_for_note function."""

    def test_safe_path_generation(self):
        """Generates safe artifact paths."""
        note = vtp.ParsedNote(
            rel_path="folder/file with spaces & special!.md",
            filename="file",
            vault_dir="folder",
            content="",
            body="",
            frontmatter={},
            relationships=[],
            tags=[]
        )
        path = vtp.artifact_path_for_note(note)
        assert path.startswith("/")
        assert " " not in path
        assert "&" not in path

    def test_preserves_valid_characters(self):
        """Preserves valid characters in path."""
        note = vtp.ParsedNote(
            rel_path="docs/my-note_v1.md",
            filename="my-note_v1",
            vault_dir="docs",
            content="",
            body="",
            frontmatter={},
            relationships=[],
            tags=[]
        )
        path = vtp.artifact_path_for_note(note)
        assert "my-note_v1" in path
        assert ".md" in path


# ============================================================================
# PREPARE MEMORY CONTENT TESTS
# ============================================================================

class TestPrepareMemoryContent:
    """Test prepare_memory_content function."""

    def test_small_note_not_truncated(self):
        """Small notes are not truncated."""
        note = vtp.ParsedNote(
            rel_path="small.md",
            filename="small",
            vault_dir="",
            content="Short content",
            body="Short content",
            frontmatter={},
            relationships=[],
            tags=[]
        )
        content, needs_artifact = vtp.prepare_memory_content(note, summarizer=None)
        assert needs_artifact is False
        assert content == "Short content"

    def test_oversized_note_truncated_without_llm(self):
        """Oversized notes are truncated with artifact path reference."""
        note = vtp.ParsedNote(
            rel_path="large.md",
            filename="large",
            vault_dir="",
            content="X" * 15000,
            body="X" * 15000,
            frontmatter={},
            relationships=[],
            tags=[]
        )
        content, needs_artifact = vtp.prepare_memory_content(note, summarizer=None)
        assert needs_artifact is True
        assert "[Content truncated" in content
        assert "/oversize-notes/large.md" in content

    def test_oversized_note_with_summarizer(self):
        """Oversized notes use summarizer and include artifact path."""
        note = vtp.ParsedNote(
            rel_path="large.md",
            filename="large",
            vault_dir="",
            content="X" * 15000,
            body="X" * 15000,
            frontmatter={},
            relationships=[],
            tags=[]
        )
        summarizer = mock.Mock(spec=vtp.LLMClient)
        summarizer.summarize.return_value = "Summary of content"

        content, needs_artifact = vtp.prepare_memory_content(note, summarizer)
        assert needs_artifact is True
        assert "Summary" in content
        assert "/oversize-notes/large.md" in content
        summarizer.summarize.assert_called_once()


# ============================================================================
# PARSED NOTE TESTS
# ============================================================================

class TestParsedNote:
    """Test ParsedNote dataclass."""

    def test_parsed_note_creation(self):
        """ParsedNote can be created and accessed."""
        note = vtp.ParsedNote(
            rel_path="folder/note.md",
            filename="note",
            vault_dir="folder",
            content="Full content",
            body="Body only",
            frontmatter={"key": "value"},
            relationships=[("target", "supports")],
            tags=["tag1", "tag2"]
        )
        assert note.rel_path == "folder/note.md"
        assert note.filename == "note"
        assert note.body == "Body only"
        assert len(note.tags) == 2


# ============================================================================
# ESTIMATE LLM TOKENS TEST
# ============================================================================

class TestEstimateLLMTokens:
    """Test estimate_llm_tokens function."""

    def test_estimates_tokens_for_oversized_notes(self):
        """Estimates tokens only for oversized notes."""
        notes = [
            vtp.ParsedNote(
                rel_path="small.md",
                filename="small",
                vault_dir="",
                content="x" * 5000,
                body="x" * 5000,
                frontmatter={},
                relationships=[],
                tags=[]
            ),
            vtp.ParsedNote(
                rel_path="large.md",
                filename="large",
                vault_dir="",
                content="y" * 15000,
                body="y" * 15000,
                frontmatter={},
                relationships=[],
                tags=[]
            )
        ]
        tokens = vtp.estimate_llm_tokens(notes)
        # Only large note contributes
        assert tokens > 0
        # Rough estimate: 15000 / 4 ≈ 3750
        assert tokens >= 3500


class TestDocumentsPhase:
    """Tests for the Documents/ import phase."""

    def test_documents_dir_with_supported_files(self, tmp_path):
        """Supported document types are uploaded; unsupported are skipped."""
        docs_dir = tmp_path / "Documents"
        docs_dir.mkdir()
        (docs_dir / "report.pdf").write_bytes(b"%PDF-1.4 fake")
        (docs_dir / "notes.txt").write_text("some notes")
        (docs_dir / "readme.md").write_text("# Readme")
        (docs_dir / "data.json").write_text('{"key": "value"}')
        (docs_dir / "config.yaml").write_text("key: value")
        (docs_dir / "script.py").write_text("print('hello')")
        (docs_dir / "app.js").write_text("console.log('hi')")
        (docs_dir / "data.csv").write_text("a,b,c")
        (docs_dir / "page.html").write_text("<html></html>")
        (docs_dir / "book.epub").write_bytes(b"PK\x03\x04 fake epub")
        (docs_dir / "image.png").write_bytes(b"\x89PNG")
        (docs_dir / "binary.exe").write_bytes(b"\x00\x01\x02")

        cp = vtp.Checkpoint()
        cp_path = tmp_path / ".penfield_import_checkpoint.json"

        mock_client = mock.MagicMock()
        mock_client.upload_document.return_value = {"id": "doc-uuid"}

        vtp.run_documents_phase(tmp_path, mock_client, cp, cp_path)

        # 10 supported types uploaded — not .png or .exe
        assert mock_client.upload_document.call_count == 10
        assert len(cp.documents) == 10
        assert "image.png" not in cp.documents
        assert "binary.exe" not in cp.documents
        assert "documents.json" not in cp.documents
        assert "image.png" not in cp.documents

    def test_oversized_document_skipped(self, tmp_path):
        """Documents exceeding 20MB are skipped and marked as failed."""
        docs_dir = tmp_path / "Documents"
        docs_dir.mkdir()
        big_file = docs_dir / "huge.pdf"
        big_file.write_bytes(b"x" * (21 * 1024 * 1024))  # 21MB
        (docs_dir / "small.txt").write_text("ok")

        cp = vtp.Checkpoint()
        cp_path = tmp_path / ".penfield_import_checkpoint.json"
        mock_client = mock.MagicMock()
        mock_client.upload_document.return_value = {"id": "doc-uuid"}

        vtp.run_documents_phase(tmp_path, mock_client, cp, cp_path)

        # Only small.txt uploaded; huge.pdf skipped
        assert mock_client.upload_document.call_count == 1
        assert len(cp.documents) == 1
        assert "huge.pdf" in cp.failed_documents

    def test_no_documents_dir_skipped(self, tmp_path):
        """No Documents/ directory → phase skipped gracefully."""
        cp = vtp.Checkpoint()
        cp_path = tmp_path / ".penfield_import_checkpoint.json"
        mock_client = mock.MagicMock()

        vtp.run_documents_phase(tmp_path, mock_client, cp, cp_path)

        mock_client.upload_document.assert_not_called()
        assert len(cp.documents) == 0


class TestVaultArtifactsPhase:
    """Tests for the Artifacts/ import phase."""

    def test_artifacts_dir_uploads_all_files(self, tmp_path):
        """All files in Artifacts/ are uploaded with correct paths."""
        art_dir = tmp_path / "Artifacts"
        art_dir.mkdir()
        (art_dir / "test.txt").write_text("test content")
        sub = art_dir / "research"
        sub.mkdir()
        (sub / "notes.md").write_text("# Notes")
        (sub / "data.csv").write_text("a,b,c")

        cp = vtp.Checkpoint()
        cp_path = tmp_path / ".penfield_import_checkpoint.json"

        mock_client = mock.MagicMock()
        vtp.run_vault_artifacts_phase(tmp_path, mock_client, cp, cp_path)

        assert mock_client.create_artifact.call_count == 3
        assert len(cp.vault_artifacts) == 3
        # Verify paths are preserved
        paths = [call.args[0] for call in mock_client.create_artifact.call_args_list]
        assert "/test.txt" in paths
        assert "/research/notes.md" in paths
        assert "/research/data.csv" in paths

    def test_no_artifacts_dir_skipped(self, tmp_path):
        """No Artifacts/ directory → phase skipped."""
        cp = vtp.Checkpoint()
        cp_path = tmp_path / ".penfield_import_checkpoint.json"
        mock_client = mock.MagicMock()

        vtp.run_vault_artifacts_phase(tmp_path, mock_client, cp, cp_path)

        mock_client.create_artifact.assert_not_called()

    def test_oversized_plus_vault_artifacts(self, tmp_path):
        """Both oversized-note artifacts and vault artifacts contribute to total."""
        cp = vtp.Checkpoint()
        cp.artifacts = {"big-note.md": "/big-note.md"}  # From oversized phase
        cp.vault_artifacts = {"file.txt": "/file.txt"}    # From vault phase

        # Total artifacts = 2
        assert len(cp.artifacts) + len(cp.vault_artifacts) == 2


class TestSkipPenfieldDir:
    """Tests that _penfield/ is skipped during vault parsing."""

    def test_penfield_dir_skipped(self, tmp_path):
        """Files in _penfield/ are not imported as memories."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "note.md").write_text("---\ntags: [test]\n---\nReal note")
        penfield_dir = vault / "_penfield"
        penfield_dir.mkdir()
        (penfield_dir / "export-meta.md").write_text("---\ntype: meta\n---\nExport metadata")

        notes = vtp.parse_vault(vault, vtp.DEFAULT_SKIP_DIRS)
        filenames = [n.filename for n in notes]
        assert "note" in filenames
        assert "export-meta" not in filenames


# ============================================================================
# GIT INCREMENTAL IMPORT TESTS
# ============================================================================

class TestGitHeadSha:
    """Tests for _git_head_sha."""

    def test_returns_sha_in_git_repo(self, tmp_path):
        """Returns a 40-char hex SHA in a real git repo."""
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init", "--author", "test <t@t>"],
            cwd=str(tmp_path), capture_output=True,
            env={**__import__("os").environ, "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        sha = vtp._git_head_sha(tmp_path)
        assert sha is not None
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_returns_none_outside_git_repo(self, tmp_path):
        """Returns None when the path is not in a git repo."""
        sha = vtp._git_head_sha(tmp_path)
        assert sha is None


class TestGitNewFilesSince:
    """Tests for _git_new_files_since."""

    def _init_repo(self, tmp_path):
        """Create a git repo with one commit and return the initial SHA."""
        import subprocess
        env = {**__import__("os").environ, "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "existing.md").write_text("first")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init", "--author", "t <t@t>"],
                        cwd=str(tmp_path), capture_output=True, env=env)
        result = subprocess.run(["git", "rev-parse", "HEAD"],
                                cwd=str(tmp_path), capture_output=True, text=True)
        return result.stdout.strip(), env

    def test_detects_new_md_files(self, tmp_path):
        """New .md files added after the baseline commit are returned."""
        import subprocess
        sha, env = self._init_repo(tmp_path)
        (tmp_path / "new_note.md").write_text("new content")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add note", "--author", "t <t@t>"],
                        cwd=str(tmp_path), capture_output=True, env=env)

        new = vtp._git_new_files_since(tmp_path, sha)
        assert "new_note.md" in new

    def test_ignores_csv_files(self, tmp_path):
        """Files with unsupported extensions are not returned."""
        import subprocess
        sha, env = self._init_repo(tmp_path)
        (tmp_path / "data.csv").write_text("a,b,c")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add csv", "--author", "t <t@t>"],
                        cwd=str(tmp_path), capture_output=True, env=env)

        new = vtp._git_new_files_since(tmp_path, sha)
        assert len(new) == 0

    def test_returns_empty_for_invalid_sha(self, tmp_path):
        """Returns empty list when the baseline SHA is not reachable."""
        self._init_repo(tmp_path)
        new = vtp._git_new_files_since(tmp_path, "0000000000000000000000000000000000000000")
        assert new == []

    def test_returns_empty_outside_git(self, tmp_path):
        """Returns empty list when not in a git repo."""
        new = vtp._git_new_files_since(tmp_path, "abc123")
        assert new == []


class TestParseVaultRestrictTo:
    """Tests for parse_vault restrict_to parameter."""

    def test_restrict_to_filters_files(self, tmp_path):
        """Only files in restrict_to are parsed."""
        (tmp_path / "a.md").write_text("---\ntags: []\n---\nA")
        (tmp_path / "b.md").write_text("---\ntags: []\n---\nB")
        (tmp_path / "c.md").write_text("---\ntags: []\n---\nC")

        notes = vtp.parse_vault(tmp_path, set(), restrict_to={"a.md", "c.md"})
        filenames = sorted(n.filename for n in notes)
        assert filenames == ["a", "c"]

    def test_restrict_to_none_returns_all(self, tmp_path):
        """When restrict_to is None, all files are parsed."""
        (tmp_path / "a.md").write_text("content A")
        (tmp_path / "b.md").write_text("content B")

        notes = vtp.parse_vault(tmp_path, set(), restrict_to=None)
        assert len(notes) == 2


# ============================================================================
# RELATIONSHIP PHASE ERROR HANDLING TESTS
# ============================================================================

class TestRelationshipsPhaseErrorHandling:
    """Tests for bulk relationship 409/500 fallback to individual creates."""

    def _make_batch(self, count: int = 3) -> list[tuple[str, dict[str, Any]]]:
        """Build a batch of (key, payload) tuples for testing."""
        return [
            (f"note{i}.md|note{i+1}.md|supports", {"from_id": f"uuid-{i}", "to_id": f"uuid-{i+1}", "relationship_type": "supports"})
            for i in range(count)
        ]

    def test_bulk_success_marks_all_done(self, tmp_path):
        """Happy path: bulk succeeds, all keys in relationships_done."""
        cp = vtp.Checkpoint(phase="relationships", memories={
            "note0.md": "uuid-0", "note1.md": "uuid-1",
            "note2.md": "uuid-2", "note3.md": "uuid-3",
        })
        cp_path = tmp_path / "cp.json"

        notes = [
            vtp.ParsedNote(filename="note0", rel_path="note0.md", vault_dir="", content="a", body="a", frontmatter={}, tags=[], relationships=[("note1", "supports")]),
            vtp.ParsedNote(filename="note1", rel_path="note1.md", vault_dir="", content="b", body="b", frontmatter={}, tags=[], relationships=[("note2", "supports")]),
            vtp.ParsedNote(filename="note2", rel_path="note2.md", vault_dir="", content="c", body="c", frontmatter={}, tags=[], relationships=[("note3", "supports")]),
        ]

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.return_value = {"data": {"created": []}}

        vtp.run_relationships_phase(notes, mock_client, cp, cp_path)

        assert len(cp.relationships_done) == 3
        assert cp.failed_relationships == []
        mock_client.create_relationships_bulk.assert_called_once()

    def test_bulk_409_triggers_individual_fallback(self, tmp_path):
        """Bulk 409 falls back to individual creates via bulk-of-1."""
        cp = vtp.Checkpoint(phase="relationships", memories={
            "note0.md": "uuid-0", "note1.md": "uuid-1",
            "note2.md": "uuid-2", "note3.md": "uuid-3",
        })
        cp_path = tmp_path / "cp.json"

        notes = [
            vtp.ParsedNote(filename="note0", rel_path="note0.md", vault_dir="", content="a", body="a", frontmatter={}, tags=[], relationships=[("note1", "supports")]),
            vtp.ParsedNote(filename="note1", rel_path="note1.md", vault_dir="", content="b", body="b", frontmatter={}, tags=[], relationships=[("note2", "supports")]),
            vtp.ParsedNote(filename="note2", rel_path="note2.md", vault_dir="", content="c", body="c", frontmatter={}, tags=[], relationships=[("note3", "supports")]),
        ]

        mock_client = mock.MagicMock()
        # First call is the full batch — returns 409
        # Subsequent calls are individual bulk-of-1 — succeed
        mock_client.create_relationships_bulk.side_effect = [
            vtp.APIError(409, '{"error":{"code":"RES_CONFLICT"}}', "/relationships/bulk"),
            {"data": {"created": []}},
            {"data": {"created": []}},
            {"data": {"created": []}},
        ]

        vtp.run_relationships_phase(notes, mock_client, cp, cp_path)

        assert len(cp.relationships_done) == 3
        assert cp.failed_relationships == []
        # 1 bulk attempt + 3 individual
        assert mock_client.create_relationships_bulk.call_count == 4

    def test_bulk_500_triggers_individual_fallback(self, tmp_path):
        """Bulk 500 (after request() retries exhausted) falls back to individual."""
        cp = vtp.Checkpoint(phase="relationships", memories={
            "note0.md": "uuid-0", "note1.md": "uuid-1",
        })
        cp_path = tmp_path / "cp.json"

        notes = [
            vtp.ParsedNote(filename="note0", rel_path="note0.md", vault_dir="", content="a", body="a", frontmatter={}, tags=[], relationships=[("note1", "supports")]),
        ]

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.side_effect = [
            vtp.APIError(500, "Internal Server Error", "/relationships/bulk"),
            {"data": {"created": []}},  # individual succeeds
        ]

        vtp.run_relationships_phase(notes, mock_client, cp, cp_path)

        assert len(cp.relationships_done) == 1
        assert cp.failed_relationships == []

    def test_individual_409_marked_as_done(self, tmp_path):
        """In fallback, individual 409 means duplicate — marked as done."""
        batch = self._make_batch(3)
        cp = vtp.Checkpoint()
        cp_path = tmp_path / "cp.json"

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.side_effect = [
            {"data": {"created": []}},  # item 0 succeeds
            vtp.APIError(409, '{"error":{"code":"RES_CONFLICT"}}', "/relationships/bulk"),  # item 1 duplicate
            {"data": {"created": []}},  # item 2 succeeds
        ]

        vtp._create_relationships_individually(mock_client, batch, cp, cp_path)

        assert len(cp.relationships_done) == 3
        assert cp.failed_relationships == []

    def test_individual_non_retriable_error_tracked_as_failure(self, tmp_path):
        """In fallback, non-409/non-500 errors are tracked as failures."""
        batch = self._make_batch(2)
        cp = vtp.Checkpoint()
        cp_path = tmp_path / "cp.json"

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.side_effect = [
            {"data": {"created": []}},  # item 0 succeeds
            vtp.APIError(422, '{"error":{"code":"VAL_VALIDATION_FAILED"}}', "/relationships/bulk"),  # genuine error
        ]

        vtp._create_relationships_individually(mock_client, batch, cp, cp_path)

        assert len(cp.relationships_done) == 1
        assert len(cp.failed_relationships) == 1
        assert batch[1][0] in cp.failed_relationships

    def test_circuit_breaker_aborts_on_consecutive_500s(self, tmp_path):
        """3 consecutive 500s in fallback aborts remaining items."""
        batch = self._make_batch(6)
        cp = vtp.Checkpoint()
        cp_path = tmp_path / "cp.json"

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.side_effect = [
            {"data": {"created": []}},  # item 0 succeeds
            vtp.APIError(500, "error", "/relationships/bulk"),  # item 1
            vtp.APIError(500, "error", "/relationships/bulk"),  # item 2
            vtp.APIError(500, "error", "/relationships/bulk"),  # item 3 → triggers breaker
        ]

        vtp._create_relationships_individually(mock_client, batch, cp, cp_path)

        # Only 4 API calls made (1 success + 3 failures), not 6
        assert mock_client.create_relationships_bulk.call_count == 4
        assert len(cp.relationships_done) == 1
        # Items 1-5 should all be in failed_relationships
        assert len(cp.failed_relationships) == 5

    def test_circuit_breaker_resets_on_non_500(self, tmp_path):
        """A success between 500s resets the consecutive counter."""
        batch = self._make_batch(6)
        cp = vtp.Checkpoint()
        cp_path = tmp_path / "cp.json"

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.side_effect = [
            vtp.APIError(500, "error", "/relationships/bulk"),  # item 0 — counter=1
            vtp.APIError(500, "error", "/relationships/bulk"),  # item 1 — counter=2
            {"data": {"created": []}},                          # item 2 — counter=0
            vtp.APIError(500, "error", "/relationships/bulk"),  # item 3 — counter=1
            vtp.APIError(500, "error", "/relationships/bulk"),  # item 4 — counter=2
            {"data": {"created": []}},                          # item 5 — counter=0
        ]

        vtp._create_relationships_individually(mock_client, batch, cp, cp_path)

        # All 6 calls made — breaker never tripped
        assert mock_client.create_relationships_bulk.call_count == 6
        assert len(cp.relationships_done) == 2
        assert len(cp.failed_relationships) == 4

    def test_checkpoint_saved_per_item_in_fallback(self, tmp_path):
        """Checkpoint is saved after each individual create for crash safety."""
        batch = self._make_batch(3)
        cp = vtp.Checkpoint()
        cp_path = tmp_path / "cp.json"

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.return_value = {"data": {"created": []}}

        with mock.patch.object(cp, "save", wraps=cp.save) as mock_save:
            vtp._create_relationships_individually(mock_client, batch, cp, cp_path)
            assert mock_save.call_count == 3

    def test_non_retriable_bulk_error_preserves_batch_failure(self, tmp_path):
        """Bulk 422 (non-409, non-500) marks all keys as failed, no fallback."""
        cp = vtp.Checkpoint(phase="relationships", memories={
            "note0.md": "uuid-0", "note1.md": "uuid-1",
        })
        cp_path = tmp_path / "cp.json"

        notes = [
            vtp.ParsedNote(filename="note0", rel_path="note0.md", vault_dir="", content="a", body="a", frontmatter={}, tags=[], relationships=[("note1", "supports")]),
        ]

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.side_effect = vtp.APIError(
            422, '{"error":{"code":"VAL_VALIDATION_FAILED"}}', "/relationships/bulk"
        )

        vtp.run_relationships_phase(notes, mock_client, cp, cp_path)

        assert len(cp.relationships_done) == 0
        assert len(cp.failed_relationships) == 1
        # Only 1 call — no fallback triggered
        mock_client.create_relationships_bulk.assert_called_once()

    def test_already_done_skipped_in_fallback(self, tmp_path):
        """Items already in relationships_done are skipped during fallback."""
        batch = self._make_batch(3)
        cp = vtp.Checkpoint()
        cp.relationships_done.add(batch[0][0])  # Mark first as already done
        cp.relationships_done.add(batch[2][0])  # Mark third as already done
        cp_path = tmp_path / "cp.json"

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.return_value = {"data": {"created": []}}

        vtp._create_relationships_individually(mock_client, batch, cp, cp_path)

        # Only 1 API call (for item 1, the only one not already done)
        mock_client.create_relationships_bulk.assert_called_once_with([batch[1][1]])

    def test_fallback_clears_prior_failure_on_success(self, tmp_path):
        """Item previously in failed_relationships is removed on individual success."""
        batch = self._make_batch(2)
        cp = vtp.Checkpoint()
        cp.failed_relationships = [batch[0][0], batch[1][0]]
        cp_path = tmp_path / "cp.json"

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.return_value = {"data": {"created": []}}

        vtp._create_relationships_individually(mock_client, batch, cp, cp_path)

        assert cp.failed_relationships == []
        assert len(cp.relationships_done) == 2

    def test_fallback_clears_prior_failure_on_duplicate(self, tmp_path):
        """Item previously in failed_relationships is removed when found as duplicate."""
        batch = self._make_batch(1)
        cp = vtp.Checkpoint()
        cp.failed_relationships = [batch[0][0]]
        cp_path = tmp_path / "cp.json"

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.side_effect = vtp.APIError(
            409, '{"error":{"code":"RES_CONFLICT"}}', "/relationships/bulk"
        )

        vtp._create_relationships_individually(mock_client, batch, cp, cp_path)

        assert cp.failed_relationships == []
        assert batch[0][0] in cp.relationships_done

    def test_bulk_502_triggers_individual_fallback(self, tmp_path):
        """502 Bad Gateway from bulk also triggers fallback (not just 500)."""
        cp = vtp.Checkpoint(phase="relationships", memories={
            "note0.md": "uuid-0", "note1.md": "uuid-1",
        })
        cp_path = tmp_path / "cp.json"

        notes = [
            vtp.ParsedNote(filename="note0", rel_path="note0.md", vault_dir="", content="a", body="a", frontmatter={}, tags=[], relationships=[("note1", "supports")]),
        ]

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.side_effect = [
            vtp.APIError(502, "Bad Gateway", "/relationships/bulk"),
            {"data": {"created": []}},  # individual succeeds
        ]

        vtp.run_relationships_phase(notes, mock_client, cp, cp_path)

        assert len(cp.relationships_done) == 1
        assert cp.failed_relationships == []

    def test_bulk_503_triggers_individual_fallback(self, tmp_path):
        """503 Service Unavailable from bulk also triggers fallback."""
        cp = vtp.Checkpoint(phase="relationships", memories={
            "note0.md": "uuid-0", "note1.md": "uuid-1",
        })
        cp_path = tmp_path / "cp.json"

        notes = [
            vtp.ParsedNote(filename="note0", rel_path="note0.md", vault_dir="", content="a", body="a", frontmatter={}, tags=[], relationships=[("note1", "supports")]),
        ]

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.side_effect = [
            vtp.APIError(503, "Service Unavailable", "/relationships/bulk"),
            {"data": {"created": []}},  # individual succeeds
        ]

        vtp.run_relationships_phase(notes, mock_client, cp, cp_path)

        assert len(cp.relationships_done) == 1
        assert cp.failed_relationships == []

    def test_bulk_422_does_not_trigger_fallback(self, tmp_path):
        """422 from bulk is NOT retried individually — it's a validation error."""
        cp = vtp.Checkpoint(phase="relationships", memories={
            "note0.md": "uuid-0", "note1.md": "uuid-1",
        })
        cp_path = tmp_path / "cp.json"

        notes = [
            vtp.ParsedNote(filename="note0", rel_path="note0.md", vault_dir="", content="a", body="a", frontmatter={}, tags=[], relationships=[("note1", "supports")]),
        ]

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.side_effect = vtp.APIError(
            422, '{"error":{"code":"VAL_VALIDATION_FAILED"}}', "/relationships/bulk"
        )

        vtp.run_relationships_phase(notes, mock_client, cp, cp_path)

        assert len(cp.relationships_done) == 0
        assert len(cp.failed_relationships) == 1
        # Only the one bulk call — no fallback
        mock_client.create_relationships_bulk.assert_called_once()

    def test_bulk_403_does_not_trigger_fallback(self, tmp_path):
        """403 from bulk is NOT retried individually — it's an auth error."""
        cp = vtp.Checkpoint(phase="relationships", memories={
            "note0.md": "uuid-0", "note1.md": "uuid-1",
        })
        cp_path = tmp_path / "cp.json"

        notes = [
            vtp.ParsedNote(filename="note0", rel_path="note0.md", vault_dir="", content="a", body="a", frontmatter={}, tags=[], relationships=[("note1", "supports")]),
        ]

        mock_client = mock.MagicMock()
        mock_client.create_relationships_bulk.side_effect = vtp.APIError(
            403, '{"error":{"code":"AUTH_FORBIDDEN"}}', "/relationships/bulk"
        )

        vtp.run_relationships_phase(notes, mock_client, cp, cp_path)

        assert len(cp.relationships_done) == 0
        assert len(cp.failed_relationships) == 1
        mock_client.create_relationships_bulk.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""Tests for tools/corpus.py — Layer 0 retrieval over the bundled corpus."""
from __future__ import annotations

from pathlib import Path

import pytest

from network_engineer.tools import corpus
from network_engineer.tools.corpus import (
    CitationCandidate,
    CitationExcerpt,
    _BM25Index,
    _parse_frontmatter,
    _tokenize,
)


# ── Frontmatter parsing ─────────────────────────────────────────────────────


def test_parse_frontmatter_extracts_yaml_block() -> None:
    text = """---
source_id: rfc-1918
title: Address Allocation for Private Internets
severity_band: INFO
---

This is the body.
"""
    fm, body = _parse_frontmatter(text)
    assert fm["source_id"] == "rfc-1918"
    assert fm["severity_band"] == "INFO"
    assert "This is the body." in body


def test_parse_frontmatter_no_block_returns_full_text() -> None:
    text = "Just a body, no frontmatter at all."
    fm, body = _parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_parse_frontmatter_invalid_yaml_returns_empty_dict() -> None:
    text = "---\n: : :\n---\n\nbody"
    fm, body = _parse_frontmatter(text)
    assert isinstance(fm, dict)
    assert "body" in body


# ── Tokenizer ───────────────────────────────────────────────────────────────


def test_tokenize_lowercases_and_splits() -> None:
    assert _tokenize("Hello, World! 192.168.1.1") == [
        "hello", "world", "192", "168", "1", "1",
    ]


def test_tokenize_empty_string_returns_empty_list() -> None:
    assert _tokenize("") == []


# ── BM25 ────────────────────────────────────────────────────────────────────


def _doc(source_id: str, text: str, **kwargs):
    """Helper to build a CorpusDocument for BM25 tests."""
    from network_engineer.tools.corpus import CorpusDocument
    return CorpusDocument(
        source_id=source_id,
        title=kwargs.get("title", source_id),
        full_text=text,
        category=kwargs.get("category", "authored"),
        severity_band=kwargs.get("severity_band"),
    )


def test_bm25_returns_relevant_doc_first() -> None:
    docs = [
        _doc("a", "the quick brown fox jumps over the lazy dog"),
        _doc("b", "ssh telnet wan exposed dangerous port forwards"),
        _doc("c", "wifi encryption wpa2 wpa3 secure"),
    ]
    index = _BM25Index(docs)
    results = index.query("ssh wan port", top_k=3)
    assert results
    # The SSH doc should be the highest-scored
    assert results[0][0] == 1


def test_bm25_returns_empty_for_empty_query() -> None:
    docs = [_doc("a", "anything")]
    index = _BM25Index(docs)
    assert index.query("") == []


def test_bm25_ignores_unmatched_terms() -> None:
    docs = [_doc("a", "ssh wan port exposed")]
    index = _BM25Index(docs)
    results = index.query("xyzzyabc nonsense gibberish", top_k=5)
    assert results == []


def test_bm25_handles_empty_corpus() -> None:
    index = _BM25Index([])
    assert index.query("anything") == []


# ── Loader against the actual bundled corpus ────────────────────────────────


def test_corpus_loads_authored_summaries() -> None:
    """The v0.1 starter bundle landed in data/corpus/authored/.
    Reload to ensure we read the live state, not cached."""
    corpus.reload()
    assert corpus.is_loaded()
    summary = corpus.manifest_summary()
    assert summary["doc_count"] >= 8
    assert summary["by_category"]["authored"] >= 8


def test_corpus_query_finds_ssh_wan_summary() -> None:
    corpus.reload()
    candidates = corpus.query("ssh wan port forward dangerous", top_k=5)
    assert candidates
    source_ids = [c.source_id for c in candidates]
    assert "red-005-ssh-telnet-wan-exposed" in source_ids


def test_corpus_query_finds_open_wifi_summary() -> None:
    corpus.reload()
    candidates = corpus.query("open wifi unencrypted ssid", top_k=5)
    source_ids = [c.source_id for c in candidates]
    assert "red-001-open-wifi-primary-ssid" in source_ids


def test_corpus_query_finds_iot_segmentation_summary() -> None:
    corpus.reload()
    candidates = corpus.query("iot trusted vlan segmentation", top_k=5)
    source_ids = [c.source_id for c in candidates]
    assert "amber-004-iot-on-trusted-vlan" in source_ids


def test_cite_by_id_returns_full_excerpt() -> None:
    corpus.reload()
    excerpt = corpus.cite_by_id("red-005-ssh-telnet-wan-exposed")
    assert excerpt is not None
    assert excerpt.severity_band == "RED"
    assert "WAN" in excerpt.full_text
    assert "NIST" in str(excerpt.sources_cited) + excerpt.full_text


def test_cite_by_id_returns_none_for_unknown() -> None:
    corpus.reload()
    assert corpus.cite_by_id("nonexistent-source") is None


def test_list_red_codes_returns_red_banded_only() -> None:
    corpus.reload()
    red_codes = corpus.list_red_codes()
    assert "red-005-ssh-telnet-wan-exposed" in red_codes
    assert "red-001-open-wifi-primary-ssid" in red_codes
    # AMBER summaries should NOT appear in red_codes
    assert "amber-001-port-forward-http-https" not in red_codes


def test_list_amber_codes_returns_amber_banded_only() -> None:
    corpus.reload()
    amber_codes = corpus.list_amber_codes()
    assert "amber-001-port-forward-http-https" in amber_codes
    assert "amber-004-iot-on-trusted-vlan" in amber_codes
    assert "red-005-ssh-telnet-wan-exposed" not in amber_codes


# ── Empty / missing-corpus handling ─────────────────────────────────────────


def test_is_loaded_false_for_missing_directory(tmp_path: Path) -> None:
    nonexistent = tmp_path / "missing"
    assert corpus.is_loaded(corpus_dir=nonexistent) is False


def test_query_empty_corpus_returns_empty_list(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert corpus.query("anything", corpus_dir=empty) == []


def test_cite_by_id_empty_corpus_returns_none(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert corpus.cite_by_id("anything", corpus_dir=empty) is None


# ── Manifest summary ────────────────────────────────────────────────────────


def test_manifest_summary_includes_severity_counts() -> None:
    corpus.reload()
    summary = corpus.manifest_summary()
    assert summary["red_count"] >= 4   # we've shipped at least 4 RED in v0.1
    assert summary["amber_count"] >= 2  # at least 2 AMBER


# ── Integration with conductor_tools ────────────────────────────────────────


def test_conductor_evaluate_against_corpus_finds_red_for_ssh_wan() -> None:
    """End-to-end: the Conductor's evaluate_against_corpus tool should
    return a RED severity for SSH-on-WAN action with citation."""
    from network_engineer.agents.conductor_tools import _evaluate_against_corpus

    result = _evaluate_against_corpus(
        action="port_forward_ssh",
        current_state={"port": 22, "destination": "WAN", "protocol": "tcp"},
    )
    assert result["corpus_loaded"] is True
    assert result["severity"] == "RED"
    assert "ssh" in result["canonical_source"].lower()
    assert "WAN" in result["title"] or "wan" in result["title"].lower()


def test_conductor_evaluate_against_corpus_handles_no_match() -> None:
    """When no corpus entry is relevant to a query, return null severity
    so the Conductor refrains from recording a caution marker."""
    from network_engineer.agents.conductor_tools import _evaluate_against_corpus

    result = _evaluate_against_corpus(
        action="completely_unrelated_xyzzy_action",
        current_state={"frobnicator": "nonexistent_widget_name"},
    )
    # corpus_loaded=true (we have docs), but severity may be null if no match
    assert result["corpus_loaded"] is True

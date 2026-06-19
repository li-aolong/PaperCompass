"""Unit tests for metadata scoring."""

from papercompass.auto.metadata import build_anchor_stats, metadata_score


def test_metadata_score_multi_source_bonus():
    paper = {
        "title": "p", "year": 2024,
        "sources": ["arxiv", "openalex", "semanticscholar"],
        "citation_count": 50,
        "venue": "ACL",
    }
    stats = build_anchor_stats([{"venue": "ACL", "citation_count": 30}])
    s = metadata_score(paper, stats)
    # multi_source(30) + cit_above_median(25) + venue_match(25) + freshness_2024(10) = 90
    assert s >= 80


def test_metadata_score_low_when_unanchored():
    paper = {"title": "p", "year": 2020, "sources": ["arxiv"], "citation_count": 0, "venue": ""}
    stats = build_anchor_stats([{"venue": "ACL", "citation_count": 50}])
    s = metadata_score(paper, stats)
    assert s == 0.0


def test_metadata_score_caps_at_100():
    paper = {
        "title": "p", "year": 2025,
        "sources": ["a", "b", "c", "d"],
        "citation_count": 10000,
        "venue": "X",
    }
    stats = build_anchor_stats([{"venue": "X", "citation_count": 100}])
    s = metadata_score(paper, stats)
    assert s <= 100.0


def test_build_anchor_stats_handles_empty_inputs():
    stats = build_anchor_stats([])
    assert stats["venues"] == set()
    assert stats["citation_median"] == 0
    assert stats["anchor_count"] == 0


def test_build_anchor_stats_computes_median():
    stats = build_anchor_stats([
        {"venue": "A", "citation_count": 10},
        {"venue": "B", "citation_count": 50},
        {"venue": "C", "citation_count": 200},
    ])
    assert stats["citation_median"] == 50
    assert stats["venues"] == {"a", "b", "c"}

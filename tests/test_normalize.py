"""v3 normalize tests.

normalize.relevance_decision is now a hard pre-filter only:
- year < min_year → out_of_scope
- manual / imported_paper → trusted
- arXiv main category not in {cs, stat, eess} → out_of_scope
- everything else → pending_fusion_score (let the fusion stage decide)

All keyword/pattern-based precision logic moved to the fusion stage.
"""

from papercompass.normalize import (
    deduplicate_papers,
    identity_keys,
    normalize_raw_candidate,
    relevance_decision,
)


def test_imported_paper_is_trusted():
    decision = relevance_decision(
        {"title": "Imported", "year": 2024},
        {"min_year": 2022},
        source_type="imported_paper",
    )
    assert decision["included"] is True
    assert decision["reason"] == "trusted_import"


def test_manual_paper_is_trusted():
    decision = relevance_decision(
        {"title": "Manual", "year": 2024},
        {"min_year": 2022},
        source_type="manual",
    )
    assert decision["confidence"] == "trusted"


def test_year_below_min_is_out_of_scope():
    decision = relevance_decision(
        {"title": "Old paper", "year": 2019},
        {"min_year": 2022},
    )
    assert decision["included"] is False
    assert decision["reason"] == "before_min_year"


def test_arxiv_physics_main_cat_filtered_out():
    decision = relevance_decision(
        {"title": "Octopus glass", "year": 2024, "categories": ["physics.bio-ph", "cond-mat"]},
        {"min_year": 2022},
        source_type="arxiv",
    )
    assert decision["included"] is False
    assert decision["reason"].startswith("arxiv_main_cat:physics")


def test_arxiv_cs_main_cat_passes_through_to_fusion():
    decision = relevance_decision(
        {"title": "TinyAgent", "year": 2024, "categories": ["cs.CL"]},
        {"min_year": 2022},
        source_type="arxiv",
    )
    assert decision["included"] is False
    assert decision["reason"] == "pending_fusion_score"
    assert decision["needs_review"] is True


def test_paper_without_categories_passes_through():
    """Non-arxiv sources don't carry categories; should not be rejected."""
    decision = relevance_decision(
        {"title": "OpenAlex paper", "year": 2024},
        {"min_year": 2022},
        source_type="openalex",
    )
    assert decision["reason"] == "pending_fusion_score"


def test_normalize_clamps_negative_citation_counts():
    paper = normalize_raw_candidate(
        {
            "source_type": "imported_paper",
            "raw": {
                "title": "Citation Placeholder Paper",
                "year": 2024,
                "citation_count": -1,
                "reference_count": -1,
                "gs_citation": -1,
            },
        },
        {"min_year": 2022},
    )

    assert paper["citation_count"] == 0
    assert paper["reference_count"] == 0
    assert paper["gs_citation"] == 0
    assert paper["max_citation"] == 0


def test_identity_keys_reads_top_level_seed_ids():
    keys = identity_keys({
        "title": "Training Large Language Models to Reason in a Continuous Latent Space",
        "year": 2024,
        "arxiv_id": "2412.06769v1",
        "doi": "https://doi.org/10.48550/arXiv.2412.06769",
    })

    assert "arxiv:2412.06769" in keys
    assert "doi:10.48550/arxiv.2412.06769" in keys


# test_term_hits_uses_word_boundaries — _term_hits was the v2 keyword
# matcher. v3 uses substring matching against discriminator_terms +
# search_hints in discovery._topic_discriminators; no replacement test
# needed here (covered by tests/test_discovery.py).


def test_source_library_id_prevents_overmerge():
    papers = [
        {
            "title": "Paper A",
            "year": 2024,
            "ids": {"arxiv": "2509.13672"},
            "source_library_id": "old:a",
            "sources": ["existing"],
        },
        {
            "title": "Paper B",
            "year": 2024,
            "ids": {"arxiv": "2509.13672"},
            "source_library_id": "old:b",
            "sources": ["existing"],
        },
    ]
    assert len(deduplicate_papers(papers)) == 2

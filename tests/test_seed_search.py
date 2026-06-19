"""Unit tests for source-backed anchor bootstrap.

Covers:
- search_seed_candidates uses strong_terms for title.search probes
- min_year is translated to from_publication_date filter
- HTTP failure returns empty list (silent fallback)
- seed anchors are accepted only when backed by source evidence
- brain-recalled seed titles are ignored by the compatibility wrapper
"""

from __future__ import annotations

from typing import Any

from papercompass.auto.seed_search import (
    _seed_query_variants,
    merge_brain_and_search_seeds,
    search_seed_candidates,
)


def _stub_fetch(captured: dict[str, Any], response: Any):
    def fake(url: str, **kwargs: Any):
        captured["url"] = url
        captured.setdefault("urls", []).append(url)
        captured["kwargs"] = kwargs
        if isinstance(response, Exception):
            raise response
        return response
    return fake


def _ok_response(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"results": items, "meta": {"count": len(items)}}


def _openalex_item(title: str, year: int, *, citations: int = 100, arxiv: str = "") -> dict[str, Any]:
    item = {
        "id": f"https://openalex.org/W{abs(hash(title)) % 10**8}",
        "doi": f"https://doi.org/10.5555/{abs(hash(title)) % 10**6}",
        "display_name": title,
        "publication_year": year,
        "publication_date": f"{year}-06-15",
        "type": "article",
        "cited_by_count": citations,
        "primary_location": {"source": {"display_name": "arXiv"}, "landing_page_url": ""},
        "best_oa_location": {},
        "open_access": {},
        "authorships": [{"author": {"display_name": "A. One"}}],
        "ids": {},
        "abstract_inverted_index": {},
        "keywords": [],
        "topics": [],
        "primary_topic": {"display_name": ""},
    }
    if arxiv:
        item["ids"] = {"arxiv": arxiv}
    return item


# ----------------------------------------------------------- search


def test_search_uses_strong_terms_as_separate_title_probes():
    captured: dict[str, Any] = {}
    rows = search_seed_candidates(
        "Small language model agents",
        strong_terms=["small language model", "on-device agent"],
        min_year=2022,
        http_get_json=_stub_fetch(captured, _ok_response([
            _openalex_item("On-device LLM agents", 2024, citations=80),
            _openalex_item("Tiny agent benchmark", 2025, citations=20),
        ])),
    )
    assert len(rows) == 2
    assert captured["url"] is not None
    # title.search filter (not default search) is what restricts to on-topic
    # papers — default search returns generic "language model" / "agent"
    # high-citation hits (medical guidelines, ChatGPT-in-education, etc.).
    assert any("title.search%3Asmall+language+model" in url for url in captured["urls"])
    assert any("title.search%3Aon-device+agent" in url for url in captured["urls"])
    assert all("from_publication_date%3A2022-01-01" in url for url in captured["urls"])
    assert all("sort=cited_by_count%3Adesc" in url for url in captured["urls"])
    # the default `search` parameter must NOT be set (we route through filter)
    assert all("search=small" not in url for url in captured["urls"])


def test_search_falls_back_to_direction_when_no_strong_terms():
    captured: dict[str, Any] = {}
    search_seed_candidates(
        "speculative decoding",
        strong_terms=[],
        http_get_json=_stub_fetch(captured, _ok_response([])),
    )
    assert "title.search%3Aspeculative+decoding" in captured["url"]


def test_seed_query_variants_skip_generic_cores():
    variants = _seed_query_variants(
        "Implicit chain-of-thought and latent reasoning",
        ["implicit chain-of-thought reasoning", "latent reasoning language model", "agent"],
        cap=10,
    )
    assert "implicit chain thought reasoning" in variants
    assert "latent reasoning language model" in variants
    assert "continuous chain thought" in variants
    assert "chain thought" not in variants
    assert "latent reasoning" not in variants
    assert "agent" not in variants


def test_search_returns_empty_on_http_failure():
    rows = search_seed_candidates(
        "anything",
        strong_terms=["x"],
        http_get_json=_stub_fetch({}, RuntimeError("openalex 503")),
    )
    assert rows == []


def test_search_returns_empty_on_malformed_payload():
    rows = search_seed_candidates(
        "anything",
        strong_terms=["x"],
        http_get_json=_stub_fetch({}, {"meta": {"count": 0}}),  # missing 'results'
    )
    assert rows == []


def test_search_continues_when_one_probe_fails():
    calls = {"n": 0}

    def fake(url: str, **kwargs: Any):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("openalex 503")
        return _ok_response([_openalex_item("Recovered Anchor", 2024, citations=10)])

    rows = search_seed_candidates(
        "latent reasoning",
        strong_terms=["implicit chain-of-thought reasoning", "latent reasoning language model"],
        http_get_json=fake,
    )
    assert [r["title"] for r in rows] == ["Recovered Anchor"]


def test_search_skips_items_without_title():
    captured: dict[str, Any] = {}
    rows = search_seed_candidates(
        "x",
        strong_terms=["real topic"],
        http_get_json=_stub_fetch(captured, _ok_response([
            _openalex_item("", 2024, citations=10),
            _openalex_item("Real Paper", 2024, citations=10),
        ])),
    )
    assert len(rows) == 1
    assert rows[0]["title"] == "Real Paper"


def test_search_uses_crossref_fallback_with_source_evidence():
    calls: list[str] = []

    def fake(url: str, **kwargs: Any):
        calls.append(url)
        if "openalex" in url:
            return _ok_response([])
        if "crossref" in url:
            return {
                "message": {
                    "items": [
                        {
                            "DOI": "10.1145/example",
                            "title": ["Fallback Anchor"],
                            "issued": {"date-parts": [[2024]]},
                            "container-title": ["ACL"],
                            "URL": "https://doi.org/10.1145/example",
                            "is-referenced-by-count": 7,
                        }
                    ]
                }
            }
        return {}

    rows = search_seed_candidates(
        "fallback anchor",
        strong_terms=["fallback anchor"],
        min_year=2022,
        http_get_json=fake,
        enable_fallbacks=True,
    )

    assert [row["title"] for row in rows] == ["Fallback Anchor"]
    assert rows[0]["verified"] is True
    assert rows[0]["evidence"]["source"] == "crossref"
    assert rows[0]["evidence"]["source_item_id"] == "10.1145/example"
    assert any("openalex" in url for url in calls)
    assert any("crossref" in url for url in calls)


# ----------------------------------------------------------- merge


def _evidence(title: str) -> dict:
    return {
        "source": "openalex",
        "query": title,
        "source_item_id": f"https://openalex.org/{title}",
        "source_url": f"https://example.org/{title}",
        "match_type": "openalex_title_search",
    }


def test_merge_ignores_brain_suggestions_and_keeps_source_backed_anchors():
    search = [
        {"title": "Top Cited Paper A", "year": 2023, "arxiv_id": "2301.0001", "citation_count": 500, "verified": True, "evidence": _evidence("Top Cited Paper A")},
        {"title": "Top Cited Paper B", "year": 2024, "arxiv_id": "2402.0002", "citation_count": 300, "verified": True, "evidence": _evidence("Top Cited Paper B")},
    ]
    brain = [
        {
            "title": "Brain Pick C",
            "year": 2023,
            "arxiv_id": "2303.0003",
            "why_seed": "core",
            "paper_role": "mechanism_eval",
            "required": True,
        },
    ]
    merged = merge_brain_and_search_seeds(brain, search, cap=15)
    titles = [m["title"] for m in merged]
    assert titles == ["Top Cited Paper A", "Top Cited Paper B"]
    assert "openalex source-backed" in merged[0]["why_seed"]
    assert "500" in merged[0]["why_seed"]
    assert merged[0]["paper_role"] == "background_anchor"
    assert merged[0]["required"] is True
    assert merged[0]["evidence"]["source"] == "openalex"


def test_merge_dedupes_by_title():
    search = [
        {"title": "Shared Paper", "year": 2024, "arxiv_id": "2401.0001", "citation_count": 99, "verified": True, "evidence": _evidence("Shared Paper")},
    ]
    brain = [
        {"title": "shared paper", "year": 2024, "arxiv_id": "alt", "why_seed": "brain pick"},
    ]
    merged = merge_brain_and_search_seeds(brain, search, cap=15)
    assert len(merged) == 1
    assert merged[0]["arxiv_id"] == "2401.0001"
    assert merged[0]["required"] is True


def test_merge_caps_total_with_source_backed_anchors_only():
    search = [
        {"title": f"S{i}", "year": 2024, "citation_count": 100, "verified": True, "evidence": _evidence(f"S{i}")}
        for i in range(20)
    ]
    brain = [{"title": f"B{i}", "year": 2024} for i in range(20)]
    merged = merge_brain_and_search_seeds(brain, search, cap=15, max_search=10)
    assert len(merged) == 15
    assert [m["title"] for m in merged] == [f"S{i}" for i in range(15)]


def test_merge_low_cit_search_still_requires_source_evidence():
    low_cit_search = [
        {"title": "Low cit A", "year": 2025, "citation_count": 1, "verified": True, "evidence": _evidence("Low cit A")},
        {"title": "Low cit B", "year": 2025, "citation_count": 2},
    ]
    brain = [
        {"title": "TinyAgent paper", "year": 2024, "why_seed": "named anchor"},
        {"title": "Octopus paper", "year": 2024, "why_seed": "named anchor"},
    ]
    merged = merge_brain_and_search_seeds(
        brain, low_cit_search, cap=15, min_anchor_citations=5
    )
    titles = [m["title"] for m in merged]
    assert titles == ["Low cit A"]


def test_merge_high_cit_search_becomes_required_anchor():
    search = [{"title": "Real anchor", "year": 2023, "citation_count": 500, "verified": True, "evidence": _evidence("Real anchor")}]
    brain = [{"title": "Brain pick", "year": 2024, "why_seed": "core"}]
    merged = merge_brain_and_search_seeds(brain, search, min_anchor_citations=5)
    assert [row["title"] for row in merged] == ["Real anchor"]
    assert merged[0]["paper_role"] == "background_anchor"
    assert merged[0]["required"] is True


def test_merge_handles_empty_search():
    """OpenAlex unreachable → no generated anchors; no brain fallback."""
    brain = [
        {"title": "Brain Only", "year": 2024, "arxiv_id": "2401.9999", "why_seed": "core"},
    ]
    merged = merge_brain_and_search_seeds(brain, [], cap=15)
    assert merged == []


def test_merge_skips_rows_without_title():
    search = [
        {"title": "", "year": 2024, "verified": True, "evidence": _evidence("blank")},
        {"title": "Good", "year": 2024, "citation_count": 1, "verified": True, "evidence": _evidence("Good")},
    ]
    merged = merge_brain_and_search_seeds([], search, cap=15)
    assert len(merged) == 1
    assert merged[0]["title"] == "Good"
    assert merged[0]["paper_role"] == "background_anchor"
    assert merged[0]["required"] is True

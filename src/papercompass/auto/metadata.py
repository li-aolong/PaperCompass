"""Metadata-signal scoring channel for v3 fusion.

Lightweight 0-100 score per paper from non-text signals:
- multi-source agreement (≥3 sources independently pulled it = high topic-relevance prior)
- citation count vs anchor median
- venue match against accepted-anchor venues
- year freshness

No brain calls. Used by the fusion stage as a third independent channel
alongside LLM judge and embedding similarity, and as the tiebreaker for
boundary cases that two brain passes can't agree on.
"""

from __future__ import annotations

from typing import Any


# Venues that don't actually distinguish papers — almost every CS paper
# carries one of these. Treating them as a "venue match" lets every arXiv
# paper get a +25 metadata bonus and overwhelms the boundary fallback.
_GENERIC_VENUES = {
    "arxiv",
    "arxiv.org",
    "arxiv preprint",
    "openreview",
    "biorxiv",
    "ssrn",
    "researchgate",
    "preprint",
}


def _venue_key(paper: dict[str, Any]) -> str:
    v = (paper.get("venue") or "").strip().lower()
    if v in _GENERIC_VENUES:
        return ""
    return v


def _citation_count(paper: dict[str, Any]) -> int:
    for k in ("max_citation", "citation_count", "gs_citation", "cited_by_count"):
        v = paper.get(k)
        if v is not None:
            try:
                return int(float(v))
            except (TypeError, ValueError):
                continue
    return 0


def _source_count(paper: dict[str, Any]) -> int:
    sources = paper.get("sources") or []
    if isinstance(sources, list):
        return len({s for s in sources if s})
    return 0


def _year(paper: dict[str, Any]) -> int:
    try:
        return int(paper.get("year") or 0)
    except (TypeError, ValueError):
        return 0


def _median(values: list[int]) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[len(s) // 2]


def build_anchor_stats(anchor_papers: list[dict[str, Any]]) -> dict[str, Any]:
    """Pre-compute aggregate stats over a set of anchor (already-accepted /
    seed) papers. Pass the result to `metadata_score` for each candidate."""
    venues: set[str] = set()
    cit_values: list[int] = []
    for p in anchor_papers or []:
        if not isinstance(p, dict):
            continue
        v = _venue_key(p)
        if v:
            venues.add(v)
        cit = _citation_count(p)
        if cit > 0:
            cit_values.append(cit)
    return {
        "venues": venues,
        "citation_median": _median(cit_values),
        "anchor_count": len([p for p in anchor_papers or [] if isinstance(p, dict)]),
    }


def metadata_score(paper: dict[str, Any], anchor_stats: dict[str, Any]) -> float:
    """Return a 0-100 metadata score for a candidate paper given anchor stats.

    Score breakdown (max 100):
      multi-source: ≥3 sources +30, 2 sources +15
      citation: ≥anchor median +25 (skip if no anchor data), positive +10
      venue: in anchor venues +25
      freshness: year ≥ recent_threshold +20, ≥ recent-2y +10
    """
    score = 0.0

    sc = _source_count(paper)
    if sc >= 3:
        score += 30
    elif sc >= 2:
        score += 15

    cit = _citation_count(paper)
    median = anchor_stats.get("citation_median", 0)
    if median > 0 and cit >= median:
        score += 25
    elif cit > 0:
        score += 10

    venue = _venue_key(paper)
    venues = anchor_stats.get("venues") or set()
    if venue and venue in venues:
        score += 25

    year = _year(paper)
    if year >= 2025:
        score += 20
    elif year >= 2023:
        score += 10

    return round(min(100.0, score), 1)

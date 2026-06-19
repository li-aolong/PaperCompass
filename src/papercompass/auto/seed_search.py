"""Bootstrap recall anchors from OpenAlex after the brain writes search hints.

Instead of trusting the brain to recall paper titles from memory (which can be
stale or hallucinated), use a cheap deterministic search — top-N by citation
count in the direction's concept space — to surface real, source-backed
anchors.

Why this matters:
- brain knowledge has a hard cutoff; recent papers get missed
- brain can fabricate papers (unverified arxiv_id / made-up titles)
- top-cited OpenAlex hits are by construction real and well-anchored

Several bounded OpenAlex calls per plan invocation. No RemoteBudget
consumption — this runs once before the discover stage's budget kicks in.
Failure is silent (empty list); the orchestrator treats "no anchors" as a
valid build state.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any

from ..roles import BACKGROUND_ANCHOR, CORE_METHOD, NEGATIVE_ROLES, normalize_role, seed_required
from ..text import clean_text


_SEED_QUERY_BLOCKLIST: set[str] = {
    "agent",
    "agents",
    "chain thought",
    "implicit chain",
    "latent reasoning",
    "hidden state",
    "test time",
    "compressed reasoning",
    "internalized reasoning",
    "recurrent reasoning",
}


def _usable_seed_probe(term: str) -> bool:
    term = clean_text(term)
    if not term:
        return False
    lowered = term.lower()
    if lowered in _SEED_QUERY_BLOCKLIST:
        return False
    tokens = [tok for tok in re.split(r"[\s\-]+", lowered) if tok]
    if len(tokens) == 1:
        return bool(re.search(r"[0-9\-]", tokens[0]) or len(tokens[0]) >= 8)
    return len(lowered) >= 8


def _seed_query_variants(direction: str, strong_terms: list[str] | None, *, cap: int = 5) -> list[str]:
    """Deterministically derive several OpenAlex title probes.

    Earlier code joined the first strong terms into one title.search query.
    For composite topics this is too strict: `"implicit chain-of-thought
    reasoning" + "latent reasoning language model"` becomes an AND query that
    few real titles can satisfy. Seed bootstrap should find real anchors
    without polluting seed repair, so probes stay title-specific: full hints
    first, then deterministic aliases, while generic cores are skipped.
    """
    from .plan import _clean_phrase, _recall_aliases

    terms = [_clean_phrase(t) for t in (strong_terms or []) if _clean_phrase(t)]
    variants: list[str] = []
    if terms:
        variants.extend(terms)
        for term in terms:
            variants.extend(_recall_aliases(term))
    else:
        variants.append(clean_text(direction))

    out: list[str] = []
    seen: set[str] = set()
    for term in variants:
        term = clean_text(term)
        if not term:
            continue
        key = term.lower()
        if key in seen or not _usable_seed_probe(term):
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= cap:
            break
    if not out and _usable_seed_probe(direction):
        out.append(clean_text(direction))
    return out


def search_seed_candidates(
    direction: str,
    *,
    strong_terms: list[str] | None = None,
    min_year: int | None = None,
    max_results: int = 30,
    base_url: str | None = None,
    api_key: str = "",
    timeout: int = 35,
    http_get_json: Any = None,  # injectable for tests
    arxiv_search_fn: Any = None,  # injectable for tests
    enable_fallbacks: bool | None = None,
) -> list[dict[str, Any]]:
    """Fetch source-backed candidate anchors matching the direction.

    OpenAlex is tried first. If it returns no anchors in real runs, deterministic
    fallback lookups query Crossref, DBLP, then arXiv. Empty list on failure is
    valid: caller treats this as "no source-backed anchors".
    """
    if os.environ.get("PAPERCOMPASS_SKIP_SEED_SEARCH", "").strip().lower() in {"1", "true", "yes"}:
        return []
    # Local imports to defer the discovery module load until actually called
    # (avoids importing the heavy discovery module just to import this file).
    from ..discovery import (
        CROSSREF_API,
        DBLP_API,
        OPENALEX_API,
        crossref_item_to_raw,
        dblp_item_to_raw,
        http_get_json as _default_http_get_json,
        openalex_item_to_raw,
        openalex_select_fields,
        openalex_url,
    )

    fetch = http_get_json or _default_http_get_json
    base_url = base_url or OPENALEX_API
    direction = clean_text(direction)
    queries = _seed_query_variants(direction, strong_terms)
    if not queries:
        return []

    # Use title.search filter, not the default `search` param. OpenAlex's
    # default search is lexical OR over all fields (title/abstract/concepts/
    # keywords) and returns top-cited "language model" or "agent" papers
    # that are clearly not on-topic (medical guidelines, ChatGPT-in-education
    # surveys, etc.). title.search restricts the match to titles AND-joining
    # the tokens, which is the correct semantics for axis-anchored seeds.
    api_key = api_key or os.environ.get("OPENALEX_API_KEY", "")

    if enable_fallbacks is None:
        enable_fallbacks = http_get_json is None

    out: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    def push_raw(raw: dict[str, Any], evidence: dict[str, Any]) -> None:
        title = clean_text(raw.get("title"))
        if not title:
            return
        if min_year and int(raw.get("year") or 0) and int(raw.get("year") or 0) < int(min_year):
            return
        key = title.lower()
        if key in seen_titles:
            return
        seen_titles.add(key)
        raw["evidence"] = evidence
        raw["verified"] = True
        out.append(raw)

    per_query = max(1, min(max_results, 50))
    for query in queries:
        filters = ["type:article", f"title.search:{query}"]
        if min_year:
            filters.append(f"from_publication_date:{int(min_year)}-01-01")
        params: dict[str, Any] = {
            "filter": ",".join(filters),
            "per-page": per_query,
            "sort": "cited_by_count:desc",
            "select": openalex_select_fields(),
        }
        if api_key:
            params["api_key"] = api_key
        url = openalex_url(base_url, params)
        try:
            data = fetch(
                url,
                timeout=timeout,
                attempts=2,
                retry_status={429, 500, 502, 503, 504},
                backoff=3.0,
                source="seed_search",
            )
        except Exception:
            continue
        items = data.get("results") if isinstance(data, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            raw = openalex_item_to_raw(item)
            push_raw(
                raw,
                {
                    "source": "openalex",
                    "query": query,
                    "api_url": url,
                    "source_item_id": clean_text(raw.get("openalex_id")),
                    "source_url": clean_text(raw.get("url") or raw.get("openalex_id")),
                    "match_type": "openalex_title_search",
                },
            )
    if not out and enable_fallbacks:
        for query in queries:
            url = (
                f"{CROSSREF_API.rstrip('/')}/works?"
                + urllib.parse.urlencode({
                    "query.title": query,
                    "rows": min(per_query, 20),
                    "select": "DOI,title,author,published-print,published-online,issued,container-title,URL,type,abstract,reference-count,is-referenced-by-count",
                    **({"filter": f"from-pub-date:{int(min_year)}-01-01"} if min_year else {}),
                })
            )
            try:
                data = fetch(
                    url,
                    timeout=timeout,
                    attempts=2,
                    retry_status={429, 500, 502, 503, 504},
                    backoff=3.0,
                    source="seed_search_crossref",
                )
            except Exception:
                continue
            items = ((data.get("message") or {}).get("items") or []) if isinstance(data, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw = crossref_item_to_raw(item)
                source_id = clean_text(raw.get("doi"))
                push_raw(
                    raw,
                    {
                        "source": "crossref",
                        "query": query,
                        "api_url": url,
                        "source_item_id": source_id,
                        "source_url": clean_text(raw.get("url") or (f"https://doi.org/{source_id}" if source_id else "")),
                        "match_type": "crossref_title_search",
                    },
                )
        if not out:
            for query in queries:
                url = f"{DBLP_API}?{urllib.parse.urlencode({'q': query, 'format': 'json', 'h': min(per_query, 20), 'f': 0})}"
                try:
                    data = fetch(
                        url,
                        timeout=timeout,
                        attempts=2,
                        retry_status={429, 500, 502, 503, 504},
                        backoff=3.0,
                        source="seed_search_dblp",
                    )
                except Exception:
                    continue
                items = (((data.get("result") or {}).get("hits") or {}).get("hit") or []) if isinstance(data, dict) else []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    raw = dblp_item_to_raw(item)
                    source_id = clean_text(raw.get("dblp_key") or raw.get("doi"))
                    push_raw(
                        raw,
                        {
                            "source": "dblp",
                            "query": query,
                            "api_url": url,
                            "source_item_id": source_id,
                            "source_url": clean_text(raw.get("url") or source_id),
                            "match_type": "dblp_title_search",
                        },
                    )
        if not out:
            if arxiv_search_fn is None:
                from ..sources.arxiv import arxiv_search as arxiv_search_fn
            for query in queries:
                arxiv_query = f'ti:"{query}"'
                try:
                    items = arxiv_search_fn(arxiv_query, max_results=min(per_query, 20), timeout=timeout)
                except Exception:
                    continue
                for raw in items or []:
                    if not isinstance(raw, dict):
                        continue
                    source_id = clean_text(raw.get("arxiv_id"))
                    push_raw(
                        raw,
                        {
                            "source": "arxiv",
                            "query": arxiv_query,
                            "source_item_id": source_id,
                            "source_url": clean_text(raw.get("url") or (f"https://arxiv.org/abs/{source_id}" if source_id else "")),
                            "match_type": "arxiv_title_search",
                        },
                    )
    out.sort(
        key=lambda row: int(row.get("citation_count") or row.get("cited_by_count") or 0),
        reverse=True,
    )
    return out[:max_results]


def source_backed_seed_anchors(
    search_seeds: list[dict[str, Any]],
    *,
    cap: int = 15,
    max_search: int = 10,
    min_anchor_citations: int = 5,
) -> list[dict[str, Any]]:
    """Convert search-derived records into source-backed recall anchors.

    No model-recalled paper title is accepted here. Every output row must come
    from a programmatic source record carrying evidence. This keeps anchor
    recall as an audit over real source output rather than model memory.

    Dedupe by lowercased normalized title. Each output row uses the
    plan_schema seed shape: title / year / arxiv_id / doi / url / why_seed /
    paper_role / required.
    """
    out: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    def normalize(row: dict[str, Any]) -> dict[str, Any] | None:
        title = clean_text(row.get("title"))
        if not title:
            return None
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        verified = row.get("verified") is True and bool(evidence)
        if not verified:
            return None
        why = clean_text(row.get("why_seed"))
        if not why:
            source_label = clean_text(evidence.get("source") or "source")
            cit = row.get("citation_count") or row.get("cited_by_count")
            if cit:
                why = f"{source_label} source-backed ({int(cit)} citations)"
            else:
                why = f"{source_label} source-backed search result"
        role = normalize_role(
            row.get("paper_role") or row.get("role"),
            default=BACKGROUND_ANCHOR,
        )
        requested_required = seed_required({"paper_role": role, "required": row.get("required")})
        required = bool(role not in NEGATIVE_ROLES)
        normalized = {
            "title": title,
            "year": row.get("year"),
            "arxiv_id": clean_text(row.get("arxiv_id")),
            "doi": clean_text(row.get("doi")),
            "url": clean_text(row.get("url")),
            "why_seed": why,
            "paper_role": role,
            "required": required,
            "requested_required": requested_required,
            "verified": True,
        }
        normalized["evidence"] = evidence
        return normalized

    def push(rows: list[dict[str, Any]]) -> None:
        for row in rows or []:
            if len(out) >= cap:
                return
            if not isinstance(row, dict):
                continue
            normalized = normalize(row)
            if normalized is None:
                continue
            key = clean_text(normalized["title"]).lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            out.append(normalized)

    # Split search seeds by citation count
    high_cit: list[dict[str, Any]] = []
    low_cit: list[dict[str, Any]] = []
    for row in search_seeds or []:
        if not isinstance(row, dict):
            continue
        cit = int(row.get("citation_count") or row.get("cited_by_count") or 0)
        if cit >= min_anchor_citations:
            high_cit.append(row)
        else:
            low_cit.append(row)

    # Phase 1: high-citation search anchors are source-backed.
    remaining_search_slots = max(0, min(max_search, cap - len(out)))
    push(high_cit[:remaining_search_slots])
    # Phase 2: low-citation anchors fill slack only if still source-backed.
    push(low_cit)
    # Phase 3: any remaining high-cit beyond max_search.
    push(high_cit[max_search:])
    return out


def merge_brain_and_search_seeds(
    brain_seeds: list[dict[str, Any]],
    search_seeds: list[dict[str, Any]],
    *,
    cap: int = 15,
    max_search: int = 10,
    min_anchor_citations: int = 5,
) -> list[dict[str, Any]]:
    """Backward-compatible wrapper that ignores brain-recalled seed titles."""
    _ = brain_seeds
    return source_backed_seed_anchors(
        search_seeds,
        cap=cap,
        max_search=max_search,
        min_anchor_citations=min_anchor_citations,
    )

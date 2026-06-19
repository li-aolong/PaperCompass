from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .text import as_list, clean_text, compact_authors, normalize_title, parse_year, short_hash
from .text import strip_derived_tags
from .roles import CORE_METHOD, normalize_role


ID_KEYS = {
    "doi": "doi",
    "arxiv_id": "arxiv",
    "semantic_scholar_id": "semantic_scholar",
    "paper_id": "semantic_scholar",
    "acl_id": "acl",
    "openreview_id": "openreview",
    "openalex_id": "openalex",
    "dblp_key": "dblp",
    "pmid": "pmid",
    "pubmed_id": "pmid",
    "pmcid": "pmcid",
    "europepmc_id": "europepmc",
}

VENUE_ALIASES = {
    "arxiv": "arXiv",
    "arxiv.org": "arXiv",
    "arxiv (cornell university)": "arXiv",
    "arxiv/cornell university": "arXiv",
}


def text_blob(item: dict[str, Any]) -> str:
    return clean_text(" ".join([
        str(item.get("title", "")),
        str(item.get("abstract", "")),
        str(item.get("summary", "")),
        str(item.get("venue", "")),
        str(item.get("keywords", "")),
    ])).lower()


def _regex_hits(patterns: list[str], blob: str) -> list[str]:
    return [pattern for pattern in patterns if re.search(pattern, blob, flags=re.IGNORECASE)]


def tag_hits(item: dict[str, Any], topic: dict[str, Any], source_name: str = "") -> list[str]:
    blob = text_blob(item)
    tags = set(strip_derived_tags(as_list(item.get("tags"))))
    if source_name:
        tags.add(f"source:{source_name}")
    venue = clean_text(item.get("venue") or item.get("publicationVenue") or item.get("booktitle")).lower()
    if venue:
        tags.add(f"venue:{venue}")
    if item.get("arxiv_id") or item.get("arxiv") or "arxiv" in venue:
        tags.add("source_type:arxiv")
    for rule in topic.get("tag_rules", []) or []:
        if not isinstance(rule, dict):
            continue
        tag = clean_text(rule.get("tag"))
        patterns = rule.get("patterns") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        if tag and any(re.search(pattern, blob, flags=re.IGNORECASE) for pattern in patterns):
            tags.add(tag)
    return sorted(tags)


def canonical_venue(value: Any) -> str:
    venue = clean_text(value)
    if not venue:
        return ""
    normalized = re.sub(r"\s+", " ", venue.lower()).strip()
    return VENUE_ALIASES.get(normalized, venue)


_ARXIV_KEEP_PRIMARY_CATS = {"cs", "stat", "eess"}


def _arxiv_main_category(item: dict[str, Any]) -> str | None:
    cats = item.get("categories")
    if isinstance(cats, str):
        cats = [cats]
    if not isinstance(cats, list):
        return None
    for c in cats:
        if isinstance(c, str) and c:
            return c.split(".")[0].lower()
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "0", "false", "no", "n"}


def relevance_decision(item: dict[str, Any], topic: dict[str, Any], source_type: str = "") -> dict[str, Any]:
    """v3: normalize stage does NOT make precision decisions.

    Returns one of:
    - trusted (manual/imported papers)
    - out_of_scope (year too old / arXiv non-CS main cat)
    - pending_fusion (everything else — fusion stage assigns the final verdict)

    The semantic decision lives in stages.stage_score_papers (LLM judge +
    embedding + metadata fusion). normalize is now just a hard pre-filter.
    """
    year = parse_year(item.get("year") or item.get("published") or item.get("publicationDate"))
    required_role = normalize_role(item.get("paper_role"), default="")
    if source_type in {"manual", "imported_paper"} and _truthy(item.get("required")) and required_role:
        return {
            "included": True,
            "reason": "required_seed",
            "confidence": "trusted_required_seed",
            "year": year,
        }
    min_year = topic.get("min_year")
    if min_year and year and year < int(min_year):
        return {"included": False, "reason": "before_min_year", "confidence": "out_of_scope", "year": year}
    if source_type in {"manual", "imported_paper"}:
        return {"included": True, "reason": "trusted_import", "confidence": "trusted", "year": year}

    main_cat = _arxiv_main_category(item)
    if main_cat and main_cat not in _ARXIV_KEEP_PRIMARY_CATS:
        return {
            "included": False,
            "reason": f"arxiv_main_cat:{main_cat}",
            "confidence": "out_of_scope",
            "year": year,
        }

    return {
        "included": False,
        "reason": "pending_fusion_score",
        "confidence": "weak",
        "needs_review": True,
        "year": year,
    }


def extract_ids(raw: dict[str, Any]) -> dict[str, str]:
    ids: dict[str, str] = {}
    external = raw.get("externalIds") or raw.get("external_ids") or {}
    if isinstance(external, dict):
        for key, target in {
            "DOI": "doi",
            "ArXiv": "arxiv",
            "ACL": "acl",
            "CorpusId": "semantic_scholar_corpus",
        }.items():
            value = clean_text(external.get(key))
            if value:
                ids[target] = value

    for raw_key, id_name in ID_KEYS.items():
        value = clean_text(raw.get(raw_key))
        if value:
            ids[id_name] = value
    if clean_text(raw.get("doi")):
        ids["doi"] = clean_text(raw.get("doi")).removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    if clean_text(raw.get("arxiv_id")):
        ids["arxiv"] = re.sub(r"v\d+$", "", clean_text(raw.get("arxiv_id")).lower())
    return ids


def extract_urls(raw: dict[str, Any]) -> dict[str, str]:
    urls: dict[str, str] = {}
    for key, out_key in [
        ("url", "landing"),
        ("site", "landing"),
        ("pdf_url", "pdf"),
        ("pdf", "pdf"),
        ("github", "code"),
        ("project", "project"),
        ("openreview", "openreview"),
    ]:
        value = clean_text(raw.get(key))
        if value:
            urls[out_key] = value
    ids = extract_ids(raw)
    if ids.get("arxiv") and "landing" not in urls:
        urls["landing"] = f"https://arxiv.org/abs/{ids['arxiv']}"
    if ids.get("arxiv") and "pdf" not in urls:
        urls["pdf"] = f"https://arxiv.org/pdf/{ids['arxiv']}.pdf"
    return urls


def normalize_raw_candidate(candidate: dict[str, Any], topic: dict[str, Any]) -> dict[str, Any] | None:
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else candidate
    if not isinstance(raw, dict):
        return None

    source_name = clean_text(candidate.get("source_name") or candidate.get("source") or raw.get("source") or "unknown")
    source_type = clean_text(candidate.get("source_type") or raw.get("source_type") or "unknown")
    title = clean_text(raw.get("title"))
    if not title:
        return None

    year = parse_year(raw.get("year") or raw.get("published") or raw.get("publicationDate") or raw.get("updated"))
    ids = extract_ids(raw)
    urls = extract_urls(raw)
    paper_role = normalize_role(candidate.get("paper_role") or raw.get("paper_role"), default=CORE_METHOD)
    decision = relevance_decision(raw, topic, source_type)
    topic_signal_hits = sorted(set(as_list(candidate.get("topic_signal_hits")) + as_list(decision.get("topic_signal_hits"))))
    if topic_signal_hits:
        decision["topic_signal_hits"] = topic_signal_hits
    # v2 keyword_hits computation is gone; the field stays so catalog renderers
    # don't break. Discriminator hits are recorded in topic_signal_hits via the
    # discovery filter, which is the v3 equivalent.
    hits = sorted(set(as_list(raw.get("keyword_hits"))))
    tags = tag_hits(raw, topic, source_name)

    source_record = {
        "source_name": source_name,
        "source_type": source_type,
        "query": clean_text(candidate.get("query") or raw.get("query")),
        "fetched_at": clean_text(candidate.get("fetched_at")),
        "raw_path": clean_text(candidate.get("raw_path")),
        "source_run_id": clean_text(candidate.get("source_run_id") or raw.get("source_run_id")),
        "source_item_id": clean_text(candidate.get("source_item_id") or raw.get("source_item_id")),
        "source_url": clean_text(candidate.get("source_url") or raw.get("source_url")),
        "venue_raw": clean_text(raw.get("venue") or raw.get("publicationVenue") or raw.get("booktitle")),
        "discovery_confidence": clean_text(candidate.get("discovery_confidence") or raw.get("discovery_confidence")),
        "discovery_reason": clean_text(candidate.get("discovery_reason") or raw.get("discovery_reason")),
    }
    source_record = {k: v for k, v in source_record.items() if v}

    paper: dict[str, Any] = {
        "title": title,
        "normalized_title": normalize_title(title),
        "authors": compact_authors(raw.get("authors") or raw.get("author")),
        "year": year,
        "venue": canonical_venue(raw.get("venue") or raw.get("publicationVenue") or raw.get("booktitle")),
        "abstract": clean_text(raw.get("abstract") or raw.get("summary") or raw.get("tldr")),
        "summary": clean_text(raw.get("summary")),
        "ids": ids,
        "urls": urls,
        "sources": sorted(set(as_list(raw.get("sources")) + [source_name])),
        "source_records": [source_record],
        "keyword_hits": hits,
        "tags": strip_derived_tags(tags),
        "topic_signals": {
            "keyword_hits": hits,
            "topic_signal_hits": topic_signal_hits,
            "tags": tags,
            "decision_reason": decision["reason"],
        },
        "decision": decision,
        "paper_role": paper_role,
        "citation_count": max(0, int(float(raw.get("citation_count", raw.get("citationCount", 0)) or 0))),
        "reference_count": max(0, int(float(raw.get("reference_count", raw.get("referenceCount", 0)) or 0))),
        "gs_citation": max(0, int(float(raw.get("gs_citation", 0) or 0))),
        "system_tags": sorted(set(as_list(raw.get("system_tags")) + as_list(raw.get("notes")) + [decision["reason"]])),
    }
    if raw.get("library_id"):
        paper["source_library_id"] = clean_text(raw.get("library_id"))
        paper["library_id"] = clean_text(raw.get("library_id"))

    for top_key, id_key in [
        ("doi", "doi"),
        ("arxiv_id", "arxiv"),
        ("semantic_scholar_id", "semantic_scholar"),
        ("acl_id", "acl"),
        ("openreview_id", "openreview"),
        ("openalex_id", "openalex"),
        ("dblp_key", "dblp"),
    ]:
        if ids.get(id_key):
            paper[top_key] = ids[id_key]
    if urls.get("landing"):
        paper["url"] = urls["landing"]
    if urls.get("pdf"):
        paper["pdf_url"] = urls["pdf"]

    for optional in ("status", "track", "github", "project", "published", "updated", "categories"):
        if raw.get(optional) not in (None, "", [], {}):
            paper[optional] = raw[optional]

    paper["max_citation"] = max(paper["citation_count"], paper["gs_citation"])
    return paper


def identity_keys(paper: dict[str, Any]) -> list[str]:
    ids = paper.get("ids") or {}
    keys: list[str] = []
    if paper.get("source_library_id"):
        keys.append(f"source_library_id:{clean_text(paper['source_library_id']).lower()}")
    top_level_ids = {
        "doi": paper.get("doi"),
        "arxiv": paper.get("arxiv_id") or paper.get("arxiv"),
        "acl": paper.get("acl_id") or paper.get("acl"),
        "openreview": paper.get("openreview_id") or paper.get("openreview"),
        "semantic_scholar": paper.get("semantic_scholar_id") or paper.get("semantic_scholar"),
        "openalex": paper.get("openalex_id") or paper.get("openalex"),
        "dblp": paper.get("dblp_key") or paper.get("dblp"),
        "pmid": paper.get("pmid") or paper.get("pubmed_id"),
        "pmcid": paper.get("pmcid"),
        "europepmc": paper.get("europepmc_id") or paper.get("europepmc"),
    }
    for id_name in ("doi", "arxiv", "acl", "openreview", "semantic_scholar", "openalex", "dblp", "pmid", "pmcid", "europepmc"):
        value = clean_text(ids.get(id_name) or top_level_ids.get(id_name))
        if value:
            if id_name == "doi":
                value = value.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
            elif id_name == "arxiv":
                value = re.sub(r"v\d+$", "", value)
            keys.append(f"{id_name}:{value.lower()}")
    title = normalize_title(paper.get("title", ""))
    year = parse_year(paper.get("year"))
    if title:
        keys.append(f"title:{title}:{year or ''}")
        keys.append(f"title:{title}")
    return keys


def merge_paper(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if value in (None, "", [], {}):
            continue
        if key in {"sources", "keyword_hits", "system_tags", "tags"}:
            merged[key] = sorted(set(as_list(merged.get(key)) + as_list(value)))
        elif key == "source_records":
            merged.setdefault(key, [])
            merged[key].extend(value or [])
        elif key in {"ids", "urls", "topic_signals", "decision"} and isinstance(value, dict):
            base = dict(merged.get(key) or {})
            base.update({k: v for k, v in value.items() if v not in (None, "", [], {})})
            merged[key] = base
        elif key == "paper_role":
            if merged.get(key) in (None, "", CORE_METHOD):
                merged[key] = normalize_role(value, default=CORE_METHOD)
        elif key in {"citation_count", "gs_citation", "max_citation", "reference_count"}:
            merged[key] = max(int(float(merged.get(key) or 0)), int(float(value or 0)))
        elif key == "authors":
            if author_quality(value) > author_quality(merged.get(key)):
                merged[key] = value
        elif merged.get(key) in (None, "", [], {}):
            merged[key] = value
    merged["max_citation"] = max(int(float(merged.get("citation_count") or 0)), int(float(merged.get("gs_citation") or 0)))
    return merged


def author_quality(value: Any) -> int:
    text = clean_text(value)
    if not text:
        return 0
    parts = [part.strip() for part in re.split(r"\s*;\s*|\s*,\s*", text) if part.strip()]
    score = len(parts) * 10 + len(text)
    if " et al" in text.lower():
        score -= 200
    return score


# --- Publication-metadata fusion ------------------------------------------
# Published-venue indexes own a paper's real venue. Submission trackers
# (OpenReview / paperlists) report the *submission* target and a review
# decision, which must not override an authoritative published venue, nor be
# read as the paper's final publication status when the paper is published
# elsewhere.
_PUBLICATION_SOURCE_PRIORITY: dict[str, int] = {
    "acl_anthology": 1,
    "crossref": 2,
    "dblp": 3,
    "openalex": 4,
    "pubmed": 4,
    "europepmc": 4,
    "semanticscholar": 5,
    "arxiv": 6,
    "openreview": 7,
    "paperlists": 8,
}
_DEFAULT_SOURCE_PRIORITY = 5
_PUBLISHED_VENUE_SOURCES: set[str] = {
    "acl_anthology", "crossref", "dblp", "openalex", "pubmed", "europepmc",
}
_PREPRINT_VENUE_TOKENS: set[str] = {"arxiv", "corr", "openreview", "preprint", "biorxiv", "medrxiv"}
_ACCEPT_STATUS_TOKENS: tuple[str, ...] = ("poster", "oral", "spotlight", "long", "short", "findings", "accept")
_WITHDRAW_TOKENS: tuple[str, ...] = ("withdraw",)
_REJECT_TOKENS: tuple[str, ...] = ("reject",)


def _source_priority(name: str | None) -> int:
    return _PUBLICATION_SOURCE_PRIORITY.get(clean_text(name).lower(), _DEFAULT_SOURCE_PRIORITY)


def _finalize_venue(paper: dict[str, Any]) -> str:
    """Pick the venue from the most authoritative source that reports one.

    Order-independent: real published venues from high-priority sources beat a
    submission tracker's target venue (e.g. ACL Anthology "EACL" beats a
    paperlists OpenReview "ICLR" record), and preprint-server venues only win
    when nothing else reports a venue.
    """
    real: list[tuple[int, str]] = []
    preprint: list[tuple[int, str]] = []
    for rec in paper.get("source_records") or []:
        venue_raw = clean_text(rec.get("venue_raw"))
        if not venue_raw:
            continue
        prio = _source_priority(rec.get("source_name"))
        if venue_raw.lower() in _PREPRINT_VENUE_TOKENS:
            preprint.append((prio, venue_raw))
        else:
            real.append((prio, venue_raw))
    pool = real or preprint
    if not pool:
        return paper.get("venue") or ""
    pool.sort(key=lambda item: item[0])
    return canonical_venue(pool[0][1])


def _finalize_publication_status(paper: dict[str, Any]) -> str:
    """Derive a trustworthy publication status from all merged evidence.

    `status` (an OpenReview review decision) is noisy and source-specific; this
    field is the cross-source verdict. An explicit acceptance, or presence in a
    published-venue index / a DOI / an ACL id, outranks a submission tracker's
    "Reject"/"Withdraw" (a paper can be a rejected submission yet published
    elsewhere).
    """
    sources = {clean_text(s).lower() for s in as_list(paper.get("sources"))}
    ids = paper.get("ids") or {}
    status = (clean_text(paper.get("status")) or "").lower()
    published_evidence = bool((sources & _PUBLISHED_VENUE_SOURCES) or ids.get("doi") or ids.get("acl"))
    if any(tok in status for tok in _ACCEPT_STATUS_TOKENS):
        return "accepted"
    if published_evidence:
        return "published"
    if any(tok in status for tok in _WITHDRAW_TOKENS):
        return "withdrawn"
    if "desk" in status and any(tok in status for tok in _REJECT_TOKENS):
        return "desk_reject"
    if any(tok in status for tok in _REJECT_TOKENS):
        return "rejected"
    if "arxiv" in sources:
        return "preprint"
    if status:
        return "submitted"
    return "unknown"


def deduplicate_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pool: dict[str, dict[str, Any]] = {}
    index: dict[str, str] = {}
    collisions: dict[str, int] = defaultdict(int)

    for paper in papers:
        keys = identity_keys(paper)
        source_keys = [key for key in keys if key.startswith("source_library_id:")]
        if source_keys:
            canonical = index.get(source_keys[0], "")
        else:
            canonical = next((index[key] for key in keys if key in index), "")
        if canonical:
            pool[canonical] = merge_paper(pool[canonical], paper)
        else:
            canonical = keys[0] if keys else f"title_hash:{short_hash(paper.get('title', ''))}"
            pool[canonical] = paper
        for key in keys:
            index[key] = canonical

    unique = list(pool.values())
    for paper in unique:
        paper["venue"] = _finalize_venue(paper)
        paper["publication_status"] = _finalize_publication_status(paper)
        year = clean_text(paper.get("year")) or "unknown"
        base = paper.get("doi") or paper.get("arxiv_id") or paper.get("acl_id") or paper.get("semantic_scholar_id") or paper.get("title")
        slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", clean_text(paper.get("title")).lower()).strip("-")[:84].strip("-") or "untitled"
        paper_key = f"{year}_{slug}_{short_hash(base)}"
        collisions[paper_key] += 1
        if collisions[paper_key] > 1:
            paper_key = f"{paper_key}_{collisions[paper_key]}"
        paper["paper_key"] = paper_key
        if paper.get("source_library_id"):
            paper["library_id"] = paper["source_library_id"]
        else:
            paper["library_id"] = identity_keys(paper)[0] if identity_keys(paper) else f"paper_key:{paper_key}"

    unique.sort(key=lambda item: (-rank_score(item), -int(parse_year(item.get("year")) or 0), normalize_title(item.get("title", ""))))
    return unique


def rank_score(item: dict[str, Any]) -> float:
    score = 0.0
    year = parse_year(item.get("year")) or 0
    citation = max(int(float(item.get("citation_count") or 0)), int(float(item.get("gs_citation") or 0)))
    source_count = len(set(as_list(item.get("sources"))))
    score += min(citation, 200) / 5
    score += min(source_count, 5) * 4
    if year >= 2026:
        score += 8
    elif year == 2025:
        score += 6
    elif year == 2024:
        score += 4
    elif year == 2023:
        score += 2
    if item.get("doi") or item.get("arxiv_id") or item.get("acl_id"):
        score += 4
    if item.get("keyword_hits"):
        score += min(len(item.get("keyword_hits") or []), 6)
    if item.get("tags"):
        score += min(len(item.get("tags") or []), 6) * 0.5
    return score

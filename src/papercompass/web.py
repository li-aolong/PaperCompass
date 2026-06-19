from __future__ import annotations

import json
import mimetypes
import re
import threading
import urllib.parse
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .catalog import search_catalog
from .config import catalog_dir, data_dir, load_topic_config, manifests_dir, state_dir
from .fulltext import candidate_pdf_urls, fetch_ar5iv_markdown, update_fulltext_index, write_fulltext_markdown
from .normalize import rank_score
from .text import clean_text, format_author_list, normalize_title, read_json, title_tokens, write_json


STATIC_DIR = Path(__file__).resolve().parent / "static"


KEYWORD_GROUPS = [
    ("Chinese CSC", {"csc", "sighan", "chinese spelling correction", "chinese spelling check"}),
    ("Chinese GEC", {"chinese grammatical error correction", "chinese grammar error diagnosis"}),
    ("GEC", {"gec", "grammatical error correction", "grammar error correction"}),
    ("Grammar Error Detection", {"grammatical error detection", "error detection"}),
    ("Grammar Error Diagnosis", {"grammatical error diagnosis", "error diagnosis"}),
    ("Spelling Correction", {
        "spelling correction", "spell checking", "spelling check", "spelling error correction",
        "spelling error detection", "contextual spelling correction",
    }),
    ("Text/Writing Correction", {
        "text error correction", "chinese text correction", "learner text correction",
        "learner corpus", "writing correction", "writing feedback",
    }),
    ("Benchmarks/Datasets", {"errant", "conll-2014", "jfleg", "bea-2019", "mucgec", "fcgec", "nacgec"}),
]


def canonical_keyword(value: str) -> str:
    key = clean_text(value).lower()
    for label, values in KEYWORD_GROUPS:
        if key in values:
            return label
    return clean_text(value)


def keyword_matches(selected: str, values: list[str]) -> bool:
    if not selected:
        return True
    return selected in {canonical_keyword(value) for value in values or []}


def public_tags(values: list[str]) -> list[str]:
    tags = []
    for value in values or []:
        tag = clean_text(value)
        if not tag or tag.startswith("source:") or tag.startswith("venue:"):
            continue
        tags.append(tag)
    return sorted(set(tags))


def venue_label(value: str) -> str:
    text = clean_text(value)
    low = text.lower()
    if not text or text == "N/A":
        return "N/A"
    if low in {"arxiv", "arxiv.org"}:
        return "arXiv"
    if low == "acl" or "association for computational linguistics" in low:
        return "ACL"
    if low == "emnlp" or "empirical methods in natural language processing" in low:
        return "EMNLP"
    if low == "naacl" or "north american chapter" in low:
        return "NAACL"
    if low == "coling" or "international conference on computational linguistics" in low:
        return "COLING"
    if low == "aaai" or "aaai conference" in low:
        return "AAAI"
    if low == "ijcai" or "international joint conference on artificial intelligence" in low:
        return "IJCAI"
    if low == "iclr" or "learning representations" in low:
        return "ICLR"
    if low == "icml" or "machine learning" in low:
        return "ICML"
    if low in {"nips", "neurips"} or "neural information processing systems" in low:
        return "NeurIPS"
    if "natural language processing and chinese computing" in low:
        return "NLPCC"
    if low in {"tacl", "transactions of the association for computational linguistics"}:
        return "TACL"
    if low in {"computational linguistics", "language resources and evaluation"}:
        return "Computational Linguistics"
    return "Other venues"


def preferred_landing(paper: dict[str, Any]) -> tuple[str, str]:
    ids = paper.get("ids") or {}
    urls = paper.get("urls") or {}
    acl_id = clean_text(ids.get("acl") or paper.get("acl_id"))
    arxiv_id = clean_text(ids.get("arxiv") or paper.get("arxiv_id"))
    doi = clean_text(ids.get("doi") or paper.get("doi"))
    raw_landing = clean_text(urls.get("landing") or paper.get("url"))
    if acl_id and re.match(r"^\d{4}\.", acl_id):
        return f"https://aclanthology.org/{acl_id}/", "Publisher"
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}", "arXiv"
    if raw_landing and "semanticscholar.org" not in raw_landing:
        return raw_landing, "Publisher"
    if doi:
        return f"https://doi.org/{doi.removeprefix('https://doi.org/').removeprefix('http://doi.org/')}", "DOI"
    return raw_landing, "Landing"


def semantic_scholar_url(paper: dict[str, Any]) -> str:
    ids = paper.get("ids") or {}
    paper_id = clean_text(ids.get("semantic_scholar") or paper.get("semantic_scholar_id"))
    if paper_id:
        return f"https://www.semanticscholar.org/paper/{paper_id}"
    raw_landing = clean_text((paper.get("urls") or {}).get("landing") or paper.get("url"))
    return raw_landing if "semanticscholar.org" in raw_landing else ""


def source_label(source: str) -> str:
    normalized = source.lower()
    if "semanticscholar" in normalized and "eval" in normalized:
        return "Semantic Scholar + LLM score"
    labels = {
        "paperlists": "Top conference list",
        "semanticscholar": "Semantic Scholar",
        "openalex": "OpenAlex",
        "crossref": "Crossref",
        "dblp": "DBLP",
        "acl_anthology": "ACL Anthology",
        "europepmc": "Europe PMC",
        "pubmed": "PubMed",
        "openreview": "OpenReview",
        "arxiv": "arXiv",
        "existing_gec_library": "Migrated library",
        "semantic_scholar_targeted_search": "Semantic Scholar",
        "semantic_scholar_llm_scored_results": "Semantic Scholar + LLM score",
        "papercopilot_paperlists": "Top conference list",
        "arxiv_relevance": "arXiv relevance",
        "arxiv_submittedDate": "arXiv recent",
    }
    return labels.get(source, source.replace("_", " ").strip().title())


def load_papers(workspace: Path) -> list[dict[str, Any]]:
    papers = read_json(data_dir(workspace) / "papers.json", [])
    return papers if isinstance(papers, list) else []


def load_anchor_papers(workspace: Path) -> list[dict[str, Any]]:
    papers = read_json(data_dir(workspace) / "anchor_papers.json", [])
    return papers if isinstance(papers, list) else []


def load_catalog_index(workspace: Path, name: str, default: Any = None) -> Any:
    return read_json(catalog_dir(workspace) / "index" / name, default)


def is_workspace(path: Path) -> bool:
    return path.is_dir() and (path / "topic.yaml").exists()


def discover_workspace_paths(root: Path, default_workspace: Path) -> list[Path]:
    paths = {default_workspace.resolve()}
    if root.exists():
        for child in root.iterdir():
            if is_workspace(child):
                paths.add(child.resolve())
    return sorted(paths, key=lambda item: (item.name != default_workspace.name, item.name.lower()))


def workspace_record(workspace: Path, active_workspace: Path) -> dict[str, Any]:
    topic = load_topic_config(workspace)
    papers = load_papers(workspace)
    fulltext_index = read_json(catalog_dir(workspace) / "fulltext" / "index.json", {})
    return {
        "id": workspace.name,
        "topic_id": topic.get("topic_id") or workspace.name,
        "name": clean_text(topic.get("name")) or topic.get("topic_id") or workspace.name,
        "description": clean_text(topic.get("description")),
        "path": str(workspace),
        "paper_count": len(papers),
        "fulltext_count": len(fulltext_index or {}),
        "active": workspace.resolve() == active_workspace.resolve(),
    }


def list_workspaces(server: "PaperCompassServer", active_workspace: Path | None = None) -> dict[str, Any]:
    active = (active_workspace or server.default_workspace).resolve()
    records = [workspace_record(path, active) for path in discover_workspace_paths(server.workspaces_root, server.default_workspace)]
    return {
        "root": str(server.workspaces_root),
        "default": server.default_workspace.name,
        "current": active.name,
        "workspaces": records,
    }


def selected_workspace(server: "PaperCompassServer", params: dict[str, list[str]]) -> Path:
    requested = clean_text(params.get("workspace", [server.default_workspace.name])[0]) or server.default_workspace.name
    if "/" in requested or "\\" in requested or requested in {".", ".."}:
        raise KeyError("workspace not found")
    for path in discover_workspace_paths(server.workspaces_root, server.default_workspace):
        if path.name == requested:
            return path
    raise KeyError("workspace not found")


def paper_to_result(paper: dict[str, Any], compact: dict[str, Any] | None = None, score: float | None = None) -> dict[str, Any]:
    ids = paper.get("ids") or {}
    urls = paper.get("urls") or {}
    landing_url, landing_label = preferred_landing(paper)
    raw_venue = paper.get("venue") or (compact or {}).get("venue", "")
    result = {
        "paper_key": paper.get("paper_key") or (compact or {}).get("paper_key"),
        "title": paper.get("title") or (compact or {}).get("title", ""),
        "year": paper.get("year") or (compact or {}).get("year"),
        "venue": venue_label(raw_venue),
        "venue_raw": raw_venue,
        "authors": format_author_list(paper.get("authors") or (compact or {}).get("authors", "")),
        "authors_raw": paper.get("authors") or (compact or {}).get("authors", ""),
        "abstract": paper.get("abstract", ""),
        "keyword_hits": sorted({canonical_keyword(k) for k in paper.get("keyword_hits") or (compact or {}).get("keyword_hits", [])}),
        "keyword_hits_raw": paper.get("keyword_hits") or (compact or {}).get("keyword_hits", []),
        "tags": public_tags(paper.get("tags") or (compact or {}).get("tags", [])),
        "tags_raw": paper.get("tags") or (compact or {}).get("tags", []),
        "sources": [source_label(s) for s in paper.get("sources", [])],
        "max_citation": paper.get("max_citation", (compact or {}).get("max_citation", 0)),
        "ids": {
            "doi": ids.get("doi") or paper.get("doi", ""),
            "arxiv": ids.get("arxiv") or paper.get("arxiv_id", ""),
            "semantic_scholar": ids.get("semantic_scholar") or paper.get("semantic_scholar_id", ""),
            "acl": ids.get("acl") or paper.get("acl_id", ""),
        },
        "urls": {
            "landing": landing_url,
            "landing_label": landing_label,
            "semantic_scholar": semantic_scholar_url(paper),
            "doi": f"https://doi.org/{ids.get('doi') or paper.get('doi')}" if ids.get("doi") or paper.get("doi") else "",
            "pdf": urls.get("pdf") or paper.get("pdf_url", ""),
            "code": urls.get("code") or paper.get("github", ""),
            "project": urls.get("project") or paper.get("project", ""),
        },
    }
    if score is not None:
        result["match_score"] = score
    return result


def make_summary(workspace: Path) -> dict[str, Any]:
    topic = read_json(catalog_dir(workspace) / "manifest.json", {})
    manifest = read_json(manifests_dir(workspace) / "latest.json", {})
    papers = load_papers(workspace)
    anchors = load_anchor_papers(workspace)
    by_year: dict[str, int] = {}
    by_venue: dict[str, int] = {}
    for paper in papers:
        by_year[str(paper.get("year") or "unknown")] = by_year.get(str(paper.get("year") or "unknown"), 0) + 1
        venue = venue_label(paper.get("venue", ""))
        by_venue[venue] = by_venue.get(venue, 0) + 1
    fulltext_index = read_json(catalog_dir(workspace) / "fulltext" / "index.json", {})
    return {
        "workspace": str(workspace),
        "topic_id": manifest.get("topic_id") or workspace.name,
        "paper_count": len(papers),
        "anchor_count": len(anchors),
        "raw_candidate_count": manifest.get("raw_candidate_count"),
        "fulltext_count": len(fulltext_index or {}),
        "catalog_built_at": topic.get("built_at"),
        "years": dict(sorted(by_year.items(), reverse=True)),
        "top_venues": sorted(by_venue.items(), key=lambda item: item[1], reverse=True)[:12],
    }


AUTO_STAGE_LABELS = {
    "plan_direction": "方向拆解",
    "discover_iter1": "发现候选",
    "seed_check": "锚点检查",
    "seed_repair": "锚点修复",
    "discover_iter2": "二次发现",
    "qa_pass1": "初始 QA",
    "score_papers": "三通道评分",
    "build_after_score": "评分后构建",
    "resolve_boundary": "边界复核",
    "build_after_resolve_boundary": "复核后构建",
    "catalog": "Catalog",
    "qa_final": "最终 QA",
}


def latest_json(directory: Path, pattern: str) -> Path | None:
    files = sorted(directory.glob(pattern))
    return files[-1] if files else None


def workspace_artifact(workspace: Path, value: str) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = workspace / path
    try:
        resolved = path.resolve()
        if not str(resolved).startswith(str(workspace.resolve())):
            return None
        return resolved
    except OSError:
        return None


def load_quality_manifest(workspace: Path, summary: dict[str, Any]) -> dict[str, Any]:
    artifact = (summary.get("artifacts") or {}).get("qa_manifest") if isinstance(summary, dict) else ""
    path = workspace_artifact(workspace, clean_text(artifact))
    if not path or not path.exists():
        path = latest_json(manifests_dir(workspace), "quality_gates_*.json")
    data = read_json(path, {}) if path else {}
    return data if isinstance(data, dict) else {}


def reconcile_summary_with_quality(summary: dict[str, Any], qa: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if not qa:
        return summary, False
    current = dict(summary)
    stale = False
    qa_counts = qa.get("counts") if isinstance(qa.get("counts"), dict) else {}
    summary_counts = current.get("counts") if isinstance(current.get("counts"), dict) else {}
    if qa_counts:
        keys = ("papers", "anchors", "pending", "rejected")
        stale = any(summary_counts.get(key) != qa_counts.get(key) for key in keys)
        current["counts"] = qa_counts
    current["qa_status"] = qa.get("status")
    quality = dict(current.get("quality") if isinstance(current.get("quality"), dict) else {})
    quality.update({
        "qa_status": qa.get("status"),
        "critical": qa.get("critical") or [],
        "warnings": qa.get("warnings") or [],
        "qa_warnings": qa.get("warnings") or [],
    })
    current["quality"] = quality
    if qa.get("status") != "passed":
        current["safe_for_default_llm_retrieval"] = False
    return current, stale


def make_build_status(workspace: Path) -> dict[str, Any]:
    auto_dir = state_dir(workspace) / "auto"
    summary_path = auto_dir / "final_summary.json"
    state_path = auto_dir / "state.json"
    summary = read_json(summary_path, {})
    auto_state = read_json(state_path, {})
    summary = summary if isinstance(summary, dict) else {}
    auto_state = auto_state if isinstance(auto_state, dict) else {}
    qa = load_quality_manifest(workspace, summary)
    summary, summary_stale = reconcile_summary_with_quality(summary, qa)
    stages = []
    for name, item in (auto_state.get("stages") or {}).items():
        if not isinstance(item, dict):
            continue
        stages.append({
            "name": name,
            "label": AUTO_STAGE_LABELS.get(name, name.replace("_", " ")),
            "status": item.get("status", "unknown"),
            "started_at": item.get("started_at"),
            "ended_at": item.get("ended_at"),
            "counts": item.get("counts") or item.get("summary") or item.get("final_counts") or {},
            "warnings": item.get("warnings") or [],
            "critical": item.get("critical") or [],
            "batches": item.get("batches"),
            "brain_errors": item.get("brain_errors"),
            "brain_missing_scores": item.get("brain_missing_scores"),
            "truncated": bool(item.get("truncated")),
            "uncovered_capped": item.get("uncovered_capped", 0),
        })
    return {
        "workspace": str(workspace),
        "has_summary": bool(summary),
        "summary_path": str(summary_path) if summary_path.exists() else "",
        "state_path": str(state_path) if state_path.exists() else "",
        "summary_stale": summary_stale,
        "summary": summary,
        "state": {
            "created_at": auto_state.get("created_at"),
            "direction": auto_state.get("direction") or summary.get("direction"),
            "brain": auto_state.get("brain") or summary.get("brain"),
            "min_year": auto_state.get("min_year"),
        },
        "stages": stages,
        "qa": qa,
    }


def make_filters(workspace: Path) -> dict[str, Any]:
    by_year = load_catalog_index(workspace, "by_year.json", {})
    papers = load_papers(workspace)
    by_keyword: dict[str, int] = {}
    by_tag: dict[str, int] = {}
    by_venue: dict[str, int] = {}
    for paper in papers:
        seen_keywords = {canonical_keyword(k) for k in paper.get("keyword_hits", []) if clean_text(k)}
        for keyword in seen_keywords:
            by_keyword[keyword] = by_keyword.get(keyword, 0) + 1
        for tag in public_tags(paper.get("tags", [])):
            by_tag[tag] = by_tag.get(tag, 0) + 1
        venue = venue_label(paper.get("venue", ""))
        by_venue[venue] = by_venue.get(venue, 0) + 1
    tag_filters = dict(by_keyword)
    for key, value in by_tag.items():
        tag_filters[key] = tag_filters.get(key, 0) + value
    return {
        "years": [{"value": key, "count": len(value)} for key, value in sorted(by_year.items(), reverse=True)],
        "keywords": [
            {"value": key, "count": value}
            for key, value in sorted(tag_filters.items(), key=lambda item: item[1], reverse=True)
            if key != "__no_keyword__"
        ],
        "venues": [
            {"value": key, "count": value}
            for key, value in sorted(by_venue.items(), key=lambda item: item[1], reverse=True)
        ],
    }


def score_paper(paper: dict[str, Any], query: str) -> float:
    if not query:
        return rank_score(paper)
    normalized = normalize_title(query)
    tokens = title_tokens(query)
    if not tokens:
        tokens = [token for token in normalized.split() if len(token) >= 2]
    haystacks = {
        "title": normalize_title(paper.get("title", "")),
        "abstract": normalize_title(paper.get("abstract", "")),
        "authors": normalize_title(paper.get("authors", "")),
        "venue": normalize_title(venue_label(paper.get("venue", ""))),
        "year": normalize_title(paper.get("year", "")),
        "keywords": normalize_title(" ".join(canonical_keyword(k) for k in paper.get("keyword_hits", []) or [])),
        "tags": normalize_title(" ".join(public_tags(paper.get("tags", []) or []))),
    }
    score = 0.0
    title_hits = 0
    keyword_hits = 0
    author_hits = 0
    venue_hits = 0
    matched_tokens: set[str] = set()
    if normalized and normalized in haystacks["title"]:
        score += 60
        title_hits += len(tokens) or 1
        matched_tokens.update(tokens)
    phrase_in_abstract = bool(normalized and len(normalized) >= 10 and normalized in haystacks["abstract"])
    phrase_in_keywords = bool(normalized and len(normalized) >= 4 and (normalized in haystacks["keywords"] or normalized in haystacks["tags"]))
    if phrase_in_abstract:
        score += 12
        matched_tokens.update(tokens)
    if phrase_in_keywords:
        score += 18
        keyword_hits += len(tokens) or 1
        matched_tokens.update(tokens)
    for token in tokens:
        token_matched = False
        if token in haystacks["title"]:
            score += 10
            title_hits += 1
            token_matched = True
        if len(tokens) <= 3 and token in haystacks["keywords"]:
            score += 7
            keyword_hits += 1
            token_matched = True
        if len(tokens) <= 3 and token in haystacks["tags"]:
            score += 7
            keyword_hits += 1
            token_matched = True
        if token in haystacks["abstract"]:
            score += 3
            token_matched = True
        if token in haystacks["venue"]:
            score += 2
            venue_hits += 1
            token_matched = True
        if token in haystacks["year"]:
            score += 2
            token_matched = True
        if token in haystacks["authors"]:
            score += 1
            author_hits += 1
            token_matched = True
        if token_matched:
            matched_tokens.add(token)
    phrase_anchor = phrase_in_keywords or phrase_in_abstract or bool(normalized and normalized in haystacks["title"])
    if len(tokens) >= 2 and len(tokens) <= 3:
        anchor = phrase_anchor or len(matched_tokens) == len(set(tokens))
    elif len(tokens) >= 4:
        needed = max(3, int(len(set(tokens)) * 0.65 + 0.999))
        anchor = phrase_anchor or title_hits >= 3 or author_hits >= 2 or len(matched_tokens) >= needed
    else:
        anchor = title_hits or keyword_hits or author_hits or venue_hits or phrase_in_abstract
    if not anchor:
        return 0.0
    return score


def search_papers(workspace: Path, params: dict[str, list[str]]) -> dict[str, Any]:
    query = clean_text(params.get("q", [""])[0])
    year = clean_text(params.get("year", [""])[0])
    keyword = clean_text(params.get("keyword", [""])[0])
    venue = clean_text(params.get("venue", [""])[0])
    sort = clean_text(params.get("sort", ["relevance"])[0]) or "relevance"
    limit = max(1, min(int(params.get("limit", ["50"])[0] or 50), 200))
    offset = max(0, int(params.get("offset", ["0"])[0] or 0))
    papers = load_papers(workspace)
    filtered: list[tuple[float, dict[str, Any]]] = []

    for paper in papers:
        if year and str(paper.get("year")) != year:
            continue
        if keyword and not (
            keyword_matches(keyword, paper.get("keyword_hits") or [])
            or keyword in public_tags(paper.get("tags", []) or [])
        ):
            continue
        if venue and venue_label(paper.get("venue", "")) != venue:
            continue
        score = score_paper(paper, query)
        if query and score <= 0:
            continue
        filtered.append((score, paper))

    if query and not filtered:
        for hit in search_catalog(workspace, query, limit=limit):
            paper = read_json(workspace / hit["json_path"], {})
            filtered.append((float(hit.get("score") or 1), paper))

    if sort == "recent":
        filtered.sort(key=lambda item: (-(int(item[1].get("year") or 0)), -rank_score(item[1])))
    elif sort == "cited":
        filtered.sort(key=lambda item: (-int(item[1].get("max_citation") or 0), -(int(item[1].get("year") or 0))))
    else:
        filtered.sort(key=lambda item: (-item[0], -rank_score(item[1]), -int(item[1].get("year") or 0)))

    page = filtered[offset: offset + limit]
    return {
        "query": query,
        "total": len(filtered),
        "offset": offset,
        "limit": limit,
        "results": [paper_to_result(paper, score=score) for score, paper in page],
    }


def load_paper_by_key(workspace: Path, key: str) -> dict[str, Any] | None:
    id_lookup = load_catalog_index(workspace, "id_lookup.json", {})
    compact = id_lookup.get(key)
    if not compact:
        return None
    paper = read_json(workspace / compact["json_path"], {})
    result = paper_to_result(paper, compact)
    result["system_tags"] = paper.get("system_tags", [])
    result["tags"] = public_tags(paper.get("tags", []))
    result["source_records"] = [public_source_record(record) for record in paper.get("source_records", []) if isinstance(record, dict)]
    fulltext = read_json(catalog_dir(workspace) / "fulltext" / "index.json", {}).get(key)
    result["fulltext"] = fulltext
    return result


def load_raw_paper_by_key(workspace: Path, key: str) -> dict[str, Any] | None:
    id_lookup = load_catalog_index(workspace, "id_lookup.json", {})
    compact = id_lookup.get(key)
    if not compact:
        return None
    return read_json(workspace / compact["json_path"], {})


def public_source_record(record: dict[str, Any]) -> dict[str, str]:
    return {
        "source_name": source_label(clean_text(record.get("source_name"))),
        "source_type": clean_text(record.get("source_type")).replace("_", " ").strip().title(),
        "query": clean_text(record.get("query")),
        "source_run_id": clean_text(record.get("source_run_id")),
        "source_item_id": clean_text(record.get("source_item_id")),
        "source_url": clean_text(record.get("source_url")),
        "fetched_at": clean_text(record.get("fetched_at")),
    }


def read_markdown(workspace: Path, key: str) -> dict[str, Any]:
    id_lookup = load_catalog_index(workspace, "id_lookup.json", {})
    compact = id_lookup.get(key)
    if not compact:
        raise KeyError(key)
    path = workspace / compact["markdown_path"]
    return {"paper_key": key, "markdown": path.read_text(encoding="utf-8")}


def read_fulltext(workspace: Path, key: str) -> dict[str, Any]:
    index = read_json(catalog_dir(workspace) / "fulltext" / "index.json", {})
    record = index.get(key)
    if not record or not record.get("fulltext_path"):
        raise KeyError(key)
    path = workspace / record["fulltext_path"]
    return {"paper_key": key, "record": record, "markdown": path.read_text(encoding="utf-8")}


def fetch_markdown_only(workspace: Path, key: str, timeout: int = 45) -> dict[str, Any]:
    paper = load_raw_paper_by_key(workspace, key)
    if not paper:
        raise KeyError(key)
    retrieval = paper.get("retrieval") or {}
    paper_key = clean_text(retrieval.get("paper_key") or key)
    year = clean_text(retrieval.get("year") or paper.get("year")) or "unknown"

    index_path = catalog_dir(workspace) / "fulltext" / "index.json"
    existing = read_json(index_path, {}).get(paper_key)
    if existing and existing.get("fulltext_path") and (workspace / existing["fulltext_path"]).exists():
        return {"status": "exists", **existing}

    out_dir = catalog_dir(workspace) / "fulltext" / year / paper_key
    out_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    arxiv_id = clean_text(paper.get("arxiv_id") or (paper.get("ids") or {}).get("arxiv"))
    if arxiv_id:
        try:
            markdown, source_url, assets = fetch_ar5iv_markdown(arxiv_id, out_dir, timeout, download_assets=True)
            text_path = write_fulltext_markdown(out_dir, paper, source_url, markdown)
            record = {
                "paper_key": paper_key,
                "title": paper.get("title", ""),
                "year": year,
                "method": "ar5iv_html_to_markdown",
                "source_url": source_url,
                "fulltext_path": str(text_path.relative_to(workspace)),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "chars": len(markdown),
                "assets": assets,
            }
            write_json(out_dir / "metadata.json", record)
            update_fulltext_index(workspace, record)
            return {"status": "fetched", **record}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ar5iv failed: {exc}")

    pdf_urls = candidate_pdf_urls(paper)
    return {
        "status": "pdf_available" if pdf_urls else "unavailable",
        "paper_key": paper_key,
        "pdf_urls": pdf_urls,
        "errors": errors,
    }


class PaperCompassServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], workspace: Path) -> None:
        super().__init__(server_address, PaperCompassHandler)
        self.default_workspace = workspace.resolve()
        self.workspace = self.default_workspace
        self.workspaces_root = self.default_workspace.parent.resolve()


class PaperCompassHandler(BaseHTTPRequestHandler):
    server: PaperCompassServer

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                self.send_static("index.html")
            elif path.startswith("/static/"):
                self.send_static(path.removeprefix("/static/"))
            elif path == "/api/workspaces":
                workspace = selected_workspace(self.server, params) if params.get("workspace") else self.server.default_workspace
                self.send_json(list_workspaces(self.server, workspace))
            elif path == "/api/summary":
                self.send_json(make_summary(selected_workspace(self.server, params)))
            elif path == "/api/build-status":
                self.send_json(make_build_status(selected_workspace(self.server, params)))
            elif path == "/api/filters":
                self.send_json(make_filters(selected_workspace(self.server, params)))
            elif path == "/api/search":
                self.send_json(search_papers(selected_workspace(self.server, params), params))
            elif path.startswith("/api/paper/"):
                key = urllib.parse.unquote(path.removeprefix("/api/paper/"))
                paper = load_paper_by_key(selected_workspace(self.server, params), key)
                if not paper:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "paper not found")
                else:
                    self.send_json(paper)
            elif path.startswith("/api/markdown/"):
                key = urllib.parse.unquote(path.removeprefix("/api/markdown/"))
                self.send_json(read_markdown(selected_workspace(self.server, params), key))
            elif path.startswith("/api/fulltext-asset/"):
                rest = path.removeprefix("/api/fulltext-asset/")
                key, _, asset = rest.partition("/")
                self.send_fulltext_asset(selected_workspace(self.server, params), urllib.parse.unquote(key), urllib.parse.unquote(asset))
            elif path.startswith("/api/fulltext/"):
                key = urllib.parse.unquote(path.removeprefix("/api/fulltext/"))
                self.send_json(read_fulltext(selected_workspace(self.server, params), key))
            elif path.startswith("/api/fulltext-fetch/"):
                key = urllib.parse.unquote(path.removeprefix("/api/fulltext-fetch/"))
                self.send_json(fetch_markdown_only(selected_workspace(self.server, params), key))
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except KeyError as exc:
            self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def send_static(self, relative: str) -> None:
        safe = re.sub(r"[^a-zA-Z0-9._/-]", "", relative).strip("/")
        path = (STATIC_DIR / safe).resolve()
        if not str(path).startswith(str(STATIC_DIR.resolve())) or not path.exists() or path.is_dir():
            self.send_error_json(HTTPStatus.NOT_FOUND, "static file not found")
            return
        payload = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_fulltext_asset(self, workspace: Path, key: str, asset: str) -> None:
        index = read_json(catalog_dir(workspace) / "fulltext" / "index.json", {})
        record = index.get(key)
        if not record or not record.get("fulltext_path") or not asset:
            self.send_error_json(HTTPStatus.NOT_FOUND, "fulltext asset not found")
            return
        base = (workspace / record["fulltext_path"]).parent / "assets"
        safe_asset = Path(asset).name
        path = (base / safe_asset).resolve()
        if not str(path).startswith(str(base.resolve())) or not path.exists() or path.is_dir():
            self.send_error_json(HTTPStatus.NOT_FOUND, "fulltext asset not found")
            return
        payload = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"error": message}, status=status)


def serve(workspace: Path, host: str = "127.0.0.1", port: int = 8765) -> PaperCompassServer:
    server = PaperCompassServer((host, port), workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run_server(workspace: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = PaperCompassServer((host, port), workspace)
    print(f"PaperCompass UI: http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

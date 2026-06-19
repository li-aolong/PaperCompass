from __future__ import annotations

import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

from .build import build_workspace
from .catalog import build_catalog
from .config import cache_dir, data_dir, ensure_workspace_dirs, load_sources_config, load_topic_config
from .config import logs_dir, manifests_dir, raw_dir
from .config import portable_workspace_data, workspace_label, workspace_relative_path
from .source_budget import ensure_arxiv_budget_floor
from .text import as_list, clean_text, normalize_title, parse_year, read_json, short_hash, slugify
from .text import strip_derived_tags, write_json


USER_AGENT = "papercompass/0.1 (+https://github.com/local/papercompass)"
PAPERLISTS_RAW = "https://raw.githubusercontent.com/papercopilot/paperlists/main"
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1"
OPENALEX_API = "https://api.openalex.org"
CROSSREF_API = "https://api.crossref.org"
DBLP_API = "https://dblp.org/search/publ/api"
EUROPEPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PUBMED_EUTILS_API = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ACL_ANTHOLOGY_XML = "https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml"
OPENREVIEW_API = "https://api2.openreview.net"

CORE_DEFAULT_SOURCES = ["paperlists", "openalex", "crossref", "dblp", "arxiv"]


class RemoteBudget:
    def __init__(self, limit: int | None = None, *, name: str = "global", parent: "RemoteBudget | None" = None) -> None:
        self.limit = limit if limit and limit > 0 else None
        self.used = 0
        self.name = name
        self.parent = parent

    def consume(self, source: str, url: str) -> None:
        if self.limit is not None and self.used >= self.limit:
            raise RuntimeError(
                f"remote call budget exceeded: {self.name} {self.used}/{self.limit} before {source} {url}"
            )
        if self.parent is not None:
            self.parent.consume(source, url)
        self.used += 1

    def child(self, source: str, limit: int | None = None) -> "RemoteBudget":
        return RemoteBudget(limit, name=source, parent=self)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def run_id(prefix: str) -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{slugify(prefix, 60)}"


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)


def load_json_cache(path: Path) -> Any | None:
    try:
        return read_json(path, None)
    except json.JSONDecodeError:
        return None


def discovery_dirs(workspace: Path) -> dict[str, Path]:
    return {
        "cache": cache_dir(workspace) / "discovery",
        "raw_paperlists": raw_dir(workspace) / "paperlists",
        "raw_openalex": raw_dir(workspace) / "openalex",
        "raw_crossref": raw_dir(workspace) / "crossref",
        "raw_dblp": raw_dir(workspace) / "dblp",
        "raw_acl_anthology": raw_dir(workspace) / "acl_anthology",
        "raw_europepmc": raw_dir(workspace) / "europepmc",
        "raw_pubmed": raw_dir(workspace) / "pubmed",
        "raw_openreview": raw_dir(workspace) / "openreview",
        "raw_semanticscholar": raw_dir(workspace) / "semanticscholar",
        "raw_arxiv": raw_dir(workspace) / "arxiv",
        "logs": logs_dir(workspace),
        "manifests": manifests_dir(workspace),
        "data": data_dir(workspace),
    }


def ensure_discovery_dirs(workspace: Path) -> None:
    ensure_workspace_dirs(workspace)
    for path in discovery_dirs(workspace).values():
        path.mkdir(parents=True, exist_ok=True)


def year_range(topic: dict[str, Any], min_year: int | None, max_year: int | None) -> list[int]:
    start = min_year or parse_year(topic.get("min_year")) or 2022
    end = max_year or datetime.now().year
    if start > end:
        raise ValueError(f"min_year 不能大于 max_year：{start} > {end}")
    return list(range(start, end + 1))


def source_config(workspace: Path) -> dict[str, Any]:
    cfg = load_sources_config(workspace)
    sources = cfg.get("sources", {})
    return cfg.get("discovery") or sources.get("discovery") or {}


def topic_terms(topic: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for key in ("task_terms", "keywords", "datasets", "method_terms"):
        value = topic.get(key, [])
        if isinstance(value, str):
            value = [value]
        terms.extend(clean_text(item) for item in value if clean_text(item))
    return sorted(set(terms), key=str.lower)


def default_queries(topic: dict[str, Any]) -> list[str]:
    explicit = topic.get("search_queries") or topic.get("semantic_scholar_queries")
    if explicit:
        return [clean_text(q) for q in explicit if clean_text(q)]
    terms = [term for term in topic_terms(topic) if len(term.split()) >= 2]
    return terms[:16] or [clean_text(topic.get("name") or topic.get("topic_id") or "artificial intelligence")]


def default_arxiv_queries(topic: dict[str, Any]) -> list[str]:
    explicit = topic.get("arxiv_queries")
    if explicit:
        return [clean_text(q) for q in explicit if clean_text(q)]
    return [f'all:"{term}"' for term in default_queries(topic)[:12]]


def _topic_text(topic: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "topic_id",
        "name",
        "description",
        "direction_raw",
        "search_keyword_text",
    ):
        value = topic.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("search_hints", "discriminator_terms", "source_filter_terms", "venues", "target_venues"):
        value = topic.get(key) or []
        if isinstance(value, str):
            value = [value]
        parts.extend(str(item) for item in value if clean_text(item))
    scope = topic.get("publication_scope") if isinstance(topic.get("publication_scope"), dict) else {}
    for item in scope.get("preferred_venues") or []:
        parts.append(str(item))
    return clean_text(" ".join(parts)).lower()


def topic_prefers_acl_anthology(topic: dict[str, Any]) -> bool:
    blob = _topic_text(topic)
    return bool(re.search(
        r"\b(nlp|natural language|language model|llm|llms|linguistic|acl|emnlp|naacl|eacl|coling|tacl|aacl|gec|csc)\b",
        blob,
    ))


def topic_prefers_biomedical_sources(topic: dict[str, Any]) -> bool:
    blob = _topic_text(topic)
    return bool(re.search(
        r"\b(biomed|biomedical|clinical|medicine|medical|health|healthcare|patient|pubmed|pmc|gene|protein|drug|disease|biology|bioinformatics)\b",
        blob,
    ))


def topic_prefers_openreview(topic: dict[str, Any]) -> bool:
    blob = _topic_text(topic)
    return bool(re.search(r"\b(openreview|iclr|neurips|icml|tmlr|colm)\b", blob))


def default_discovery_sources(topic: dict[str, Any], cfg: dict[str, Any] | None = None) -> list[str]:
    selected = list(CORE_DEFAULT_SOURCES)
    if topic_prefers_acl_anthology(topic):
        selected.append("acl_anthology")
    if topic_prefers_biomedical_sources(topic):
        selected.extend(["europepmc", "pubmed"])
    openreview_cfg = (cfg or {}).get("openreview") if isinstance(cfg, dict) else {}
    if topic_prefers_openreview(topic) and isinstance(openreview_cfg, dict) and openreview_cfg.get("invitations"):
        selected.append("openreview")
    return list(dict.fromkeys(selected))


def default_venues(topic: dict[str, Any]) -> list[str]:
    explicit = topic.get("venues") or topic.get("target_venues")
    if explicit:
        return [clean_text(v).lower() for v in explicit if clean_text(v)]
    if clean_text(topic.get("topic_id")).lower() == "gec":
        return ["acl", "emnlp", "naacl", "coling"]
    return ["acl", "emnlp", "naacl", "coling", "iclr", "nips"]


def resolve_paperlists_venues(
    cli_venues: list[str] | None,
    cfg_venues: list[str] | None,
    topic: dict[str, Any],
) -> list[str]:
    """Resolve which paperlists venues to fetch.

    Priority: CLI override > sources.yaml > default_venues(topic). Empty list
    at either of the first two layers is taken AT FACE VALUE — the caller is
    explicitly disabling the venue baseline. Only `None` (key absent) falls
    back to the default fan-out.
    """
    if cli_venues is not None:
        venues = cli_venues
    elif cfg_venues is not None:
        venues = cfg_venues
    else:
        venues = default_venues(topic)
    return [clean_text(v).lower() for v in venues if clean_text(v)]


def http_get_json(
    url: str,
    timeout: int = 35,
    headers: dict[str, str] | None = None,
    attempts: int = 1,
    retry_status: set[int] | None = None,
    backoff: float = 5.0,
    budget: RemoteBudget | None = None,
    source: str = "http",
) -> Any:
    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    retry_status = retry_status or set()
    for attempt in range(max(1, attempts)):
        req = urllib.request.Request(url, headers=req_headers)
        try:
            if budget:
                budget.consume(source, url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in retry_status and attempt < attempts - 1:
                time.sleep(backoff * (attempt + 1))
                continue
            raise


def http_get_bytes(
    url: str,
    timeout: int = 35,
    headers: dict[str, str] | None = None,
    attempts: int = 1,
    retry_status: set[int] | None = None,
    backoff: float = 5.0,
    budget: RemoteBudget | None = None,
    source: str = "http",
) -> bytes:
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    retry_status = retry_status or set()
    for attempt in range(max(1, attempts)):
        req = urllib.request.Request(url, headers=req_headers)
        try:
            if budget:
                budget.consume(source, url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in retry_status and attempt < attempts - 1:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError) as exc:
            if attempt < attempts - 1 and transient_limit_error(str(exc)):
                time.sleep(backoff * (attempt + 1))
                continue
            raise


def coverage_path(workspace: Path) -> Path:
    return manifests_dir(workspace) / "source_coverage.json"


def source_runs_path(workspace: Path) -> Path:
    return logs_dir(workspace) / "source_runs.jsonl"


def provenance_path(workspace: Path) -> Path:
    return data_dir(workspace) / "candidate_provenance.jsonl"


def load_coverage(workspace: Path) -> dict[str, Any]:
    data = read_json(coverage_path(workspace), {})
    return data if isinstance(data, dict) else {}


def save_coverage(workspace: Path, coverage: dict[str, Any]) -> None:
    write_json(coverage_path(workspace), coverage)


def record_source_run(workspace: Path, record: dict[str, Any]) -> None:
    append_jsonl(source_runs_path(workspace), [record])


def record_provenance(workspace: Path, rows: list[dict[str, Any]]) -> None:
    if rows:
        append_jsonl(provenance_path(workspace), rows)


def coverage_health(
    status: str,
    *,
    cache_hit: bool = False,
    source_exhausted: bool | None = None,
    budget_complete: bool | None = None,
    risk_override: str | None = None,
) -> dict[str, Any]:
    status = clean_text(status) or "unknown"
    if cache_hit and status == "success":
        execution_status = "cache_hit"
    elif status == "missing":
        execution_status = "source_missing"
    elif status == "failed":
        execution_status = "failed"
    else:
        execution_status = status
    explicit_risk = clean_text(risk_override).lower()
    if explicit_risk in {"low", "medium", "high"}:
        risk = explicit_risk
    elif execution_status in {"failed", "rate_limited"}:
        risk = "high"
    elif execution_status == "budget_exhausted":
        risk = "medium"
    elif execution_status == "source_missing":
        risk = "medium"
    elif source_exhausted is False and budget_complete is False:
        risk = "medium"
    else:
        risk = "low"
    return {
        "execution_status": execution_status,
        "budget_complete": bool(budget_complete) if budget_complete is not None else status in {"success", "missing"},
        "source_exhausted": source_exhausted,
        "coverage_risk": risk,
    }


def semantic_scholar_optional_without_key(api_key: str, base_url: str) -> bool:
    return not clean_text(api_key) and "semanticscholar.org" in clean_text(base_url).lower()


def semantic_scholar_auth_status(api_key: str, base_url: str) -> str:
    api_key = clean_text(api_key)
    base = clean_text(base_url).lower()
    if api_key:
        return "api_key_configured"
    if "semanticscholar.org" in base:
        return "no_key_optional"
    return "anonymous"


def semantic_scholar_auth_problem(api_key: str, base_url: str, error_message: str) -> str:
    api_key = clean_text(api_key)
    base = clean_text(base_url).lower()
    error = clean_text(error_message).lower()
    if not api_key or "semanticscholar.org" not in base:
        return ""
    if "403" in error or "forbidden" in error:
        return "semanticscholar_api_key_forbidden"
    if "401" in error or "unauthorized" in error:
        return "semanticscholar_api_key_unauthorized"
    return ""


def semantic_scholar_auth_hint(problem: str) -> str:
    problem = clean_text(problem)
    if problem == "semanticscholar_api_key_forbidden":
        return "SEMANTIC_SCHOLAR_API_KEY 已配置但被官方 API 拒绝；请检查 key 是否有效、额度或权限是否可用，或临时移除该变量走无 key 可选路径。"
    if problem == "semanticscholar_api_key_unauthorized":
        return "SEMANTIC_SCHOLAR_API_KEY 已配置但未通过认证；请检查环境变量是否为有效 key。"
    return ""


def reusable_raw_path(workspace: Path, manifest: dict[str, Any], default_raw_path: Path, cache_hit: bool) -> tuple[Path, bool]:
    previous = clean_text(manifest.get("raw_output"))
    if cache_hit and previous and (workspace / previous).exists():
        return workspace / previous, False
    return default_raw_path, True


def cache_manifest_key(*parts: Any) -> str:
    return "/".join(slugify(str(part), 96) for part in parts if str(part) != "")


def candidate_identity(raw: dict[str, Any]) -> str:
    for key in ("doi", "arxiv_id", "acl_id", "openreview_id", "semantic_scholar_id", "openalex_id", "paper_id", "dblp_key", "pmid", "pmcid", "europepmc_id"):
        value = clean_text(raw.get(key))
        if value:
            return f"{key}:{value.lower()}"
    external = raw.get("externalIds") or raw.get("external_ids") or {}
    if isinstance(external, dict):
        for key, prefix in [("DOI", "doi"), ("ArXiv", "arxiv"), ("ACL", "acl")]:
            value = clean_text(external.get(key))
            if value:
                return f"{prefix}:{value.lower()}"
    title = normalize_title(raw.get("title", ""))
    year = parse_year(raw.get("year") or raw.get("published") or raw.get("publicationDate")) or ""
    return f"title:{title}:{year}"


def text_blob(item: dict[str, Any]) -> str:
    return clean_text(" ".join([
        str(item.get("title", "")),
        str(item.get("abstract", "")),
        str(item.get("summary", "")),
        str(item.get("keywords", "")),
        str(item.get("track", "")),
        str(item.get("venue", "")),
        str(item.get("bibtex", "")),
    ])).lower()


# Generic single-token words that appear in most NLP/ML papers — must NOT
# be promoted to discriminator anchors (they would match every paper).
_DISCRIMINATOR_GENERIC_TOKENS = {
    "agent", "agents", "llm", "llms", "model", "models", "language",
    "tool", "tools", "use", "uses", "calling", "function", "functions", "call",
    "ai", "system", "systems", "method", "methods", "framework", "approach",
    "paper", "study", "task", "tasks", "large", "small", "the", "of", "for",
    "on", "in", "with", "a", "an", "is", "to", "and", "at", "by", "based",
    "using", "via", "toward", "towards", "through", "across", "between",
    "from", "into", "as", "or", "are", "be",
    "selection", "generation", "reasoning", "planning", "constraints",
}

_CURATED_RECALL_ANCHORS = (
    "small language model",
    "small llm",
    "slm agent",
    "compact llm",
    "edge llm",
    "on-device language model",
    "on-device llm",
    "sub-7b",
    "sub-10b",
    "function-calling agent",
    "tool-augmented small model",
)

_ANCHOR_BLOCKLIST = {
    "on-device",
    "function-calling",
    "tool-calling",
    "structured outputs",
    "distillation",
    "mobile-side",
    "local ai assistant",
}

_AUTO_SINGLE_TOKEN_ANCHORS = {
    "tinyllama",
    "tinyagent",
    "mobilellm",
    "camphor",
    "octopus",
    "hammer",
}

_GENERIC_TOPIC_ONLY_ANCHORS = {
    "reasoning",
    "test time",
    "hidden state",
    "hidden states",
    "latent space",
    "latent spaces",
    "continuous space",
    "continuous spaces",
}

_GENERIC_STRIPPED_ANCHORS = {
    "latent space",
    "latent spaces",
    "continuous space",
    "continuous spaces",
}


def _normalize_topic_anchor(anchor: str) -> str:
    text = clean_text(anchor).lower()
    text = text.replace("-", " ")
    return " ".join(text.split())


def _is_generic_topic_anchor(anchor: str) -> bool:
    """True for anchors too broad to independently admit source candidates."""
    return _normalize_topic_anchor(anchor) in _GENERIC_TOPIC_ONLY_ANCHORS


def _extract_discriminators_from_phrases(phrases: list[str]) -> list[str]:
    """Auto-derive discriminator anchors from search_hints / search_keyword_text.

    Strips leading/trailing generic tokens; keeps phrases that have ≥2 tokens
    (and ≥8 chars total) OR a single token that contains digit/hyphen
    (e.g., "phi-3", "sub-7b") OR is ≥8 chars long ("tinyllama"). The result
    is broad — about 0.3% of paperlists titles match — but specific enough
    that the brain still has to score the survivors.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in phrases:
        phrase = clean_text(raw or "").lower().strip()
        if not phrase:
            continue
        for anchor in _CURATED_RECALL_ANCHORS:
            if anchor in phrase and anchor not in seen:
                out.append(anchor)
                seen.add(anchor)
        if any(anchor in phrase for anchor in _CURATED_RECALL_ANCHORS):
            continue
        toks = re.split(r"\s+", phrase)
        while toks and toks[-1] in _DISCRIMINATOR_GENERIC_TOKENS:
            toks.pop()
        while toks and toks[0] in _DISCRIMINATOR_GENERIC_TOKENS:
            toks.pop(0)
        stripped = " ".join(toks).strip()
        if stripped and _normalize_topic_anchor(stripped) in _GENERIC_STRIPPED_ANCHORS:
            original_toks = re.split(r"\s+", phrase)
            original = " ".join(original_toks).strip()
            if (
                2 <= len(original_toks) <= 6
                and original not in seen
                and len(original) >= 8
                and not _is_generic_topic_anchor(original)
            ):
                out.append(original)
                seen.add(original)
            continue
        if (
            not stripped
            or stripped in seen
            or stripped in _ANCHOR_BLOCKLIST
            or re.search(r"\b(privacy|deployment|structured outputs|distillation)\b", stripped)
        ):
            continue
        if len(toks) >= 2 and len(toks) <= 6 and len(stripped) >= 8:
            out.append(stripped)
            seen.add(stripped)
        elif len(toks) == 1 and (
            re.search(r"[0-9\-]", toks[0])
            or toks[0] in _AUTO_SINGLE_TOKEN_ANCHORS
        ):
            out.append(stripped)
            seen.add(stripped)
    return out


def _topic_discriminators(topic: dict[str, Any]) -> list[str]:
    """Resolve the discriminator anchor set for v3 source-level filtering.

    Brain-supplied `discriminator_terms` are useful precision anchors, but some
    brains overfit them to specific paper titles or system names. Always merge
    them with broad anchors auto-extracted from search_hints/search_keyword_text
    so source-level filtering keeps a recall pool for the downstream scorer.
    """
    explicit = [
        clean_text(t).lower()
        for t in (topic.get("discriminator_terms") or [])
        if isinstance(t, str) and clean_text(t)
    ]
    source_filter_terms = [
        clean_text(t).lower()
        for t in (
            list(topic.get("source_filter_terms") or [])
            + list(topic.get("recall_terms") or [])
        )
        if isinstance(t, str) and clean_text(t)
    ]
    if source_filter_terms:
        return [t for t in dict.fromkeys(source_filter_terms) if t]
    sources: list[str] = list(topic.get("search_hints") or [])
    kw_text = topic.get("search_keyword_text") or ""
    if isinstance(kw_text, str) and kw_text.strip():
        sources.extend([p for p in re.split(r"[,;.\n]", kw_text) if p.strip()])
    auto = _extract_discriminators_from_phrases(sources)
    return [t for t in dict.fromkeys(explicit + source_filter_terms + auto) if t]


def _topic_term_pattern(term: str) -> str:
    """Regex for a topic term with word and hyphen/space boundaries.

    The discovery gate is deliberately broad, but it must not use raw
    substring matching: short codenames such as "CODI" otherwise match
    "coding", "decoding", or "cognitive" and flood the review queue.
    """
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", clean_text(term).lower()) if p]
    if not parts:
        return ""
    sep = r"[\s\-]+"
    return rf"(?<![a-z0-9]){sep.join(parts)}(?![a-z0-9])"


def _topic_term_matches(term: str, haystack: str) -> bool:
    pattern = _topic_term_pattern(term)
    return bool(pattern and re.search(pattern, haystack, flags=re.IGNORECASE))


def topic_match_decision(item: dict[str, Any], topic: dict[str, Any]) -> dict[str, Any]:
    """v3 source-level filter for discovery results.

    Each candidate's title+abstract is checked against the topic's
    search_hints (full phrases from plan_direction) and discriminator_terms
    (specific anchors). A paper passes if any hint or discriminator is
    substring-present. The fusion stage downstream does the precision
    work; this filter only prevents wide-recall sources (paperlists venue
    baselines, broad OpenAlex queries) from dumping every conference paper
    into the pending pile.
    """
    year = parse_year(item.get("year") or item.get("published") or item.get("publicationDate"))
    min_year = parse_year(topic.get("min_year"))
    if min_year and year and year < min_year:
        return {"include": False, "confidence": "out_of_scope", "reason": "before_min_year", "hits": []}

    search_hints = [
        clean_text(h)
        for h in (topic.get("search_hints") or [])
        if clean_text(h)
    ]
    discriminators = _topic_discriminators(topic)
    if not search_hints and not discriminators:
        return {
            "include": True,
            "confidence": "weak",
            "reason": "v3_wide_recall_no_hints",
            "hits": [],
        }
    title = clean_text(item.get("title", "")).lower()
    abstract = clean_text(
        item.get("abstract", "") or item.get("summary", "")
    ).lower()
    haystack = (title + "  " + abstract).strip()
    hits = [h for h in search_hints if _topic_term_matches(h, haystack)]
    hits += [d for d in discriminators if _topic_term_matches(d, haystack) and d not in hits]
    strong_hits = [hit for hit in hits if not _is_generic_topic_anchor(hit)]
    if strong_hits:
        return {
            "include": True,
            "confidence": "weak",
            "reason": "v3_hint_match",
            "hits": hits,
        }
    if hits:
        return {
            "include": False,
            "confidence": "out_of_scope",
            "reason": "v3_generic_only_match",
            "hits": hits,
        }
    return {
        "include": False,
        "confidence": "out_of_scope",
        "reason": "v3_no_hint_match",
        "hits": [],
    }


def matches_topic(item: dict[str, Any], topic: dict[str, Any]) -> bool:
    return bool(topic_match_decision(item, topic)["include"])


def assign_tags(item: dict[str, Any], topic: dict[str, Any], source_name: str = "") -> list[str]:
    blob = text_blob(item)
    tags = {clean_text(tag) for tag in as_list(item.get("tags")) if clean_text(tag)}
    if source_name:
        tags.add(f"source:{source_name}")
    venue = clean_text(item.get("venue")).lower()
    if venue:
        tags.add(f"venue:{venue}")
    if clean_text(item.get("arxiv_id")) or "arxiv" in venue:
        tags.add("source_type:arxiv")
    if any(term in blob for term in ("dataset", "benchmark", "corpus", "shared task")):
        tags.add("paper_type:dataset_or_benchmark")
    if re.search(r"\b(chinese|中文|csc|sighan|mucgec|fcgec|nacgec)\b", blob, flags=re.IGNORECASE):
        tags.add("language:zh")
    if re.search(r"\bgrammatical error correction\b|\bgrammar error correction\b|\bgec\b", blob, flags=re.IGNORECASE):
        tags.add("task:gec")
    if re.search(r"\bgrammatical error detection\b|\bgrammar error diagnosis\b|\berror detection\b|\berror diagnosis\b", blob, flags=re.IGNORECASE):
        tags.add("task:ged")
    if re.search(r"\bspelling\b|\bspell checking\b|\bcsc\b", blob, flags=re.IGNORECASE):
        tags.add("task:spelling")
    if re.search(r"\bchinese spelling\b|\bcsc\b|\bsighan\b", blob, flags=re.IGNORECASE):
        tags.add("task:csc")

    for rule in topic.get("tag_rules", []) or []:
        if not isinstance(rule, dict):
            continue
        tag = clean_text(rule.get("tag"))
        if not tag:
            continue
        patterns = rule.get("patterns") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        if any(re.search(pattern, blob, flags=re.IGNORECASE) for pattern in patterns):
            tags.add(tag)
    return sorted(tags)


def wrap_candidate(
    raw: dict[str, Any],
    source_name: str,
    source_type: str,
    query: str,
    source_run_id: str,
    raw_path: Path,
    topic: dict[str, Any],
    source_item_id: str = "",
    source_url: str = "",
    force_include_reason: str = "",
    paper_role_hint: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = dict(raw)
    if paper_role_hint:
        raw["paper_role"] = paper_role_hint
    match = topic_match_decision(raw, topic)
    if force_include_reason:
        match = {
            "include": True,
            "confidence": "weak",
            "reason": force_include_reason,
            "hits": [query] if query else [],
        }
    confidence = clean_text(match.get("confidence"))
    if raw.get("tags"):
        raw["tags"] = strip_derived_tags(as_list(raw.get("tags")))
    fetched_at = now_iso()
    wrapper = {
        "source_name": source_name,
        "source_type": source_type,
        "query": query,
        "source_run_id": source_run_id,
        "source_item_id": source_item_id,
        "source_url": source_url,
        "fetched_at": fetched_at,
        "discovery_confidence": confidence,
        "discovery_reason": clean_text(match.get("reason")),
        "topic_signal_hits": match.get("hits") or [],
        "raw": raw,
    }
    if paper_role_hint:
        wrapper["paper_role"] = paper_role_hint
    provenance = {
        "candidate_id": candidate_identity(raw),
        "source": source_name,
        "source_type": source_type,
        "source_run_id": source_run_id,
        "query_or_list": query,
        "source_item_id": source_item_id,
        "source_url": source_url,
        "raw_path": str(raw_path),
        "fetched_at": fetched_at,
    }
    if force_include_reason:
        provenance["force_include_reason"] = force_include_reason
    if paper_role_hint:
        provenance["paper_role"] = paper_role_hint
    return wrapper, provenance


def _candidate_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _source_candidate_rank_key(row: dict[str, Any]) -> tuple[int, int, int, int, int, str]:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    title = clean_text(raw.get("title") or row.get("title")).lower()
    hits = [clean_text(hit).lower() for hit in as_list(row.get("topic_signal_hits")) if clean_text(hit)]
    strong_hits = [hit for hit in hits if not _is_generic_topic_anchor(hit)]
    title_hits = [hit for hit in strong_hits if _topic_term_matches(hit, title)]
    citation_count = _candidate_int(raw.get("citation_count") or raw.get("citationCount"))
    year = parse_year(raw.get("year") or raw.get("publicationDate") or raw.get("published")) or 0
    return (
        -len(title_hits),
        -len(strong_hits),
        -len(hits),
        -citation_count,
        -year,
        title,
    )


def _cap_source_candidates(
    rows: list[dict[str, Any]],
    provenance: list[dict[str, Any]],
    max_kept: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    if max_kept is None or max_kept <= 0 or len(rows) <= max_kept:
        return rows, provenance, len(rows)
    paired = sorted(zip(rows, provenance, strict=False), key=lambda pair: _source_candidate_rank_key(pair[0]))
    capped = paired[:max_kept]
    return [row for row, _ in capped], [prov for _, prov in capped], len(rows)


def paperlists_item_to_raw(item: dict[str, Any], venue: str, year: int) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "title": clean_text(item.get("title")),
        "year": year,
        "venue": venue.upper(),
        "abstract": clean_text(item.get("abstract")),
        "url": clean_text(item.get("site")),
        "pdf_url": clean_text(item.get("pdf")),
        "github": clean_text(item.get("github")),
        "project": clean_text(item.get("project")),
        "citation_count": int(float(item.get("gs_citation") or 0)),
        "status": clean_text(item.get("status")),
        "track": clean_text(item.get("track")),
        "bibtex": clean_text(item.get("bibtex")),
        "paperlists_id": clean_text(item.get("id")),
    }
    author = item.get("authors") or item.get("author")
    if isinstance(author, list):
        raw["authors"] = "; ".join(
            clean_text(a.get("name") if isinstance(a, dict) else a)
            for a in author
            if clean_text(a.get("name") if isinstance(a, dict) else a)
        )
    else:
        raw["authors"] = clean_text(author)
    item_id = clean_text(item.get("id"))
    site = clean_text(item.get("site"))
    if re.match(r"^\d{4}\.", item_id):
        raw["acl_id"] = item_id
    if "aclanthology.org/" in site:
        acl = site.rstrip("/").split("/")[-1]
        if acl:
            raw["acl_id"] = acl
    if "openreview.net" in site and item_id:
        raw["openreview_id"] = item_id
    if item.get("dblp"):
        raw["dblp_key"] = clean_text(item.get("dblp")).lstrip(";")
    return {k: v for k, v in raw.items() if v not in (None, "", [], {})}


def sync_paperlists(
    workspace: Path,
    topic: dict[str, Any],
    years: list[int],
    venues: list[str],
    refresh: bool = False,
    timeout: int = 35,
    budget: RemoteBudget | None = None,
) -> dict[str, Any]:
    coverage = load_coverage(workspace)
    total_kept = 0
    total_seen = 0
    runs = 0
    errors: list[dict[str, Any]] = []

    for venue in venues:
        for year in years:
            list_name = f"{venue}{year}"
            run = run_id(f"paperlists_{list_name}")
            cache_file = cache_dir(workspace) / "discovery" / "paperlists" / venue / f"{year}.json"
            key = cache_manifest_key("paperlists", venue, year)
            manifest = coverage.get(key, {})
            cache_hit = bool(cache_file.exists() and manifest.get("complete") and not refresh)
            started = now_iso()
            status = "success"
            data: list[dict[str, Any]] = []
            error_message = ""
            if cache_hit:
                cached = load_json_cache(cache_file)
                data = cached if isinstance(cached, list) else []
            else:
                url = f"{PAPERLISTS_RAW}/{venue}/{venue}{year}.json"
                try:
                    fetched = http_get_json(url, timeout=timeout, budget=budget, source="paperlists")
                    data = fetched if isinstance(fetched, list) else []
                    write_json(cache_file, data)
                except urllib.error.HTTPError as exc:
                    status = "missing" if exc.code == 404 else "failed"
                    error_message = f"HTTP {exc.code}: {exc.reason}"
                except Exception as exc:  # noqa: BLE001
                    status = "failed"
                    error_message = str(exc)

            raw_path, write_raw = reusable_raw_path(
                workspace,
                manifest,
                raw_dir(workspace) / "paperlists" / venue / f"{year}_{run}.jsonl",
                cache_hit,
            )
            rows: list[dict[str, Any]] = []
            provenance: list[dict[str, Any]] = []
            if status == "success":
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    total_seen += 1
                    raw = paperlists_item_to_raw(item, venue, year)
                    if not raw.get("title") or not matches_topic(raw, topic):
                        continue
                    wrapper, prov = wrap_candidate(
                        raw,
                        source_name="paperlists",
                        source_type="paperlists",
                        query=list_name,
                        source_run_id=run,
                        raw_path=raw_path.relative_to(workspace),
                        topic=topic,
                        source_item_id=clean_text(item.get("id")),
                        source_url=clean_text(item.get("site")),
                    )
                    rows.append(wrapper)
                    provenance.append(prov)
                if write_raw:
                    append_jsonl(raw_path, rows)
                    record_provenance(workspace, provenance)
                total_kept += len(rows)
            else:
                errors.append({"source": "paperlists", "venue": venue, "year": year, "error": error_message})

            finished = now_iso()
            coverage[key] = {
                "source": "paperlists",
                "venue": venue,
                "year": year,
                "complete": status in {"success", "missing"},
                "status": status,
                **coverage_health(
                    status,
                    cache_hit=cache_hit,
                    source_exhausted=True if status == "success" else None,
                    budget_complete=True,
                    risk_override="low" if status == "missing" else None,
                ),
                "cache_path": str(cache_file.relative_to(workspace)),
                "result_count": len(data),
                "kept_count": len(rows),
                "raw_output": str(raw_path.relative_to(workspace)),
                "last_run_id": run,
                "fetched_at": finished,
                "errors": [error_message] if error_message else [],
            }
            record_source_run(workspace, {
                "run_id": run,
                "source": "paperlists",
                "operation": "fetch",
                "query": list_name,
                "bucket": str(year),
                "params": {"venue": venue, "year": year},
                "started_at": started,
                "finished_at": finished,
                "status": status,
                "cache_hit": cache_hit,
                "cache_path": str(cache_file.relative_to(workspace)),
                "fetched_count": len(data),
                "kept_count": len(rows),
                "raw_output": str(raw_path.relative_to(workspace)),
                "written_count": len(rows) if write_raw else 0,
                "errors": [error_message] if error_message else [],
            })
            runs += 1
    save_coverage(workspace, coverage)
    return {"source": "paperlists", "runs": runs, "seen": total_seen, "kept": total_kept, "errors": errors[:20]}


def semantic_year_buckets(years: list[int], strategy: str) -> list[str]:
    strategy = clean_text(strategy).lower() or "range"
    if not years:
        return []
    if strategy == "yearly":
        return [str(year) for year in years]
    if len(years) == 1:
        return [str(years[0])]
    return [f"{years[0]}-{years[-1]}"]


def semantic_scholar_url(
    base_url: str,
    query: str,
    year_bucket: str,
    fields: str,
    mode: str,
    sort: str = "",
    token: str = "",
    limit: int = 100,
    offset: int = 0,
) -> str:
    endpoint = "paper/search/bulk" if mode == "bulk" else "paper/search"
    params: dict[str, Any] = {
        "query": query,
        "fields": fields,
        "year": year_bucket,
    }
    if mode == "bulk":
        if sort:
            params["sort"] = sort
        if token:
            params["token"] = token
    else:
        params["limit"] = min(limit, 100)
        params["offset"] = offset
    return f"{base_url.rstrip('/')}/{endpoint}?{urllib.parse.urlencode(params)}"


def ss_item_to_raw(item: dict[str, Any]) -> dict[str, Any]:
    venue = item.get("venue") or item.get("publicationVenue") or {}
    if isinstance(venue, dict):
        venue = venue.get("name", "")
    external = item.get("externalIds") or {}
    authors = item.get("authors") or []
    author_text = "; ".join(clean_text(a.get("name")) for a in authors if isinstance(a, dict) and clean_text(a.get("name")))
    raw: dict[str, Any] = {
        "title": clean_text(item.get("title")),
        "authors": author_text,
        "year": item.get("year"),
        "venue": clean_text(venue),
        "abstract": clean_text(item.get("abstract")),
        "url": clean_text(item.get("url")),
        "citation_count": int(float(item.get("citationCount") or 0)),
        "reference_count": int(float(item.get("referenceCount") or 0)),
        "semantic_scholar_id": clean_text(item.get("paperId")),
        "externalIds": external,
    }
    if isinstance(external, dict):
        if external.get("DOI"):
            raw["doi"] = clean_text(external.get("DOI"))
        if external.get("ArXiv"):
            raw["arxiv_id"] = clean_text(external.get("ArXiv"))
        if external.get("ACL"):
            raw["acl_id"] = clean_text(external.get("ACL"))
    open_access_pdf = item.get("openAccessPdf")
    if isinstance(open_access_pdf, dict) and open_access_pdf.get("url"):
        raw["pdf_url"] = clean_text(open_access_pdf.get("url"))
    if raw.get("arxiv_id") and not raw.get("pdf_url"):
        raw["pdf_url"] = f"https://arxiv.org/pdf/{raw['arxiv_id']}.pdf"
    return {k: v for k, v in raw.items() if v not in (None, "", [], {})}


def openalex_abstract(item: dict[str, Any]) -> str:
    index = item.get("abstract_inverted_index")
    if not isinstance(index, dict):
        return ""
    positioned: list[tuple[int, str]] = []
    for word, positions in index.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            try:
                positioned.append((int(position), str(word)))
            except (TypeError, ValueError):
                continue
    return clean_text(" ".join(word for _, word in sorted(positioned)))


def openalex_item_to_raw(item: dict[str, Any]) -> dict[str, Any]:
    location = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
    best_oa = item.get("best_oa_location") if isinstance(item.get("best_oa_location"), dict) else {}
    source = location.get("source") if isinstance(location.get("source"), dict) else {}
    open_access = item.get("open_access") if isinstance(item.get("open_access"), dict) else {}
    ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
    authors = []
    for authorship in item.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
        name = clean_text(author.get("display_name"))
        if name:
            authors.append(name)
    keywords = [
        clean_text(keyword.get("display_name"))
        for keyword in item.get("keywords") or []
        if isinstance(keyword, dict) and clean_text(keyword.get("display_name"))
    ]
    topics = [
        clean_text(topic.get("display_name"))
        for topic in item.get("topics") or []
        if isinstance(topic, dict) and clean_text(topic.get("display_name"))
    ]
    primary_topic = item.get("primary_topic") if isinstance(item.get("primary_topic"), dict) else {}
    raw: dict[str, Any] = {
        "openalex_id": clean_text(item.get("id")),
        "doi": clean_text(item.get("doi")),
        "arxiv_id": clean_text(ids.get("arxiv")),
        "title": clean_text(item.get("display_name") or item.get("title")),
        "authors": "; ".join(authors),
        "year": item.get("publication_year"),
        "publicationDate": clean_text(item.get("publication_date")),
        "venue": clean_text(source.get("display_name")),
        "abstract": openalex_abstract(item),
        "url": clean_text(item.get("id") or location.get("landing_page_url") or open_access.get("oa_url")),
        "pdf_url": clean_text(location.get("pdf_url") or best_oa.get("pdf_url") or open_access.get("oa_url")),
        "citation_count": int(float(item.get("cited_by_count") or 0)),
        "openalex_type": clean_text(item.get("type")),
        "keywords": "; ".join(keywords),
        "topics": "; ".join(topics),
        "primary_topic": clean_text(primary_topic.get("display_name")),
    }
    return {k: v for k, v in raw.items() if v not in (None, "", [], {})}


def openalex_select_fields() -> str:
    return ",".join([
        "id",
        "doi",
        "display_name",
        "title",
        "publication_year",
        "publication_date",
        "type",
        "cited_by_count",
        "primary_location",
        "best_oa_location",
        "open_access",
        "authorships",
        "ids",
        "abstract_inverted_index",
        "keywords",
        "topics",
        "primary_topic",
    ])


def openalex_query_specs(cfg: dict[str, Any], topic: dict[str, Any]) -> list[dict[str, Any]]:
    configured = cfg.get("queries") or default_queries(topic)
    specs: list[dict[str, Any]] = []
    for item in configured:
        if isinstance(item, str):
            text = clean_text(item)
            if text:
                specs.append({"text": text})
            continue
        if not isinstance(item, dict):
            continue
        text = clean_text(item.get("text") or item.get("query"))
        if not text:
            continue
        spec = dict(item)
        spec["text"] = text
        specs.append(spec)
    return specs


def openalex_url(base_url: str, params: dict[str, Any]) -> str:
    clean_params = {key: value for key, value in params.items() if value not in (None, "", [], {})}
    return f"{base_url.rstrip('/')}/works?{urllib.parse.urlencode(clean_params, doseq=True)}"


def sync_openalex(
    workspace: Path,
    topic: dict[str, Any],
    years: list[int],
    queries: list[dict[str, Any]],
    base_url: str = OPENALEX_API,
    api_key: str = "",
    page_size: int = 100,
    max_pages: int = 2,
    weak_max_pages: int = 1,
    modes: list[str] | None = None,
    sorts: list[str] | None = None,
    topic_ids: list[str] | None = None,
    refresh: bool = False,
    timeout: int = 35,
    sleep_seconds: float = 0.25,
    budget: RemoteBudget | None = None,
) -> dict[str, Any]:
    coverage = load_coverage(workspace)
    started = now_iso()
    total_seen = 0
    total_kept = 0
    runs = 0
    errors: list[dict[str, Any]] = []
    year_bucket = f"{years[0]}-{years[-1]}" if len(years) > 1 else str(years[0])
    default_modes = modes or ["exact"]
    default_sorts = sorts or ["relevance"]
    default_topic_ids = [clean_text(item) for item in as_list(topic_ids) if clean_text(item)]
    stop_reason = ""

    for spec in queries:
        if stop_reason:
            break
        query = clean_text(spec.get("text"))
        if not query:
            continue
        strength = clean_text(spec.get("strength", "strong")).lower() or "strong"
        spec_modes = [clean_text(mode) for mode in as_list(spec.get("modes") or spec.get("mode") or default_modes) if clean_text(mode)]
        spec_sorts = [clean_text(sort) for sort in as_list(spec.get("sorts") or spec.get("sort") or default_sorts) if clean_text(sort)]
        spec_topic_ids = [clean_text(item) for item in as_list(spec.get("topic_ids") or default_topic_ids) if clean_text(item)]
        pages = int(spec.get("max_pages") or (weak_max_pages if strength == "weak" else max_pages))
        spec_page_size = int(spec.get("page_size") or page_size)
        topic_filter_values = spec_topic_ids or [""]

        for mode in spec_modes:
            if stop_reason:
                break
            for sort in spec_sorts:
                if stop_reason:
                    break
                for topic_filter in topic_filter_values:
                    if stop_reason:
                        break
                    run = run_id(f"openalex_{mode}_{query}")
                    key = cache_manifest_key("openalex", mode, query, year_bucket, sort or "relevance", topic_filter or "no-topic")
                    manifest = coverage.get(key, {})
                    cache_root = cache_dir(workspace) / "discovery" / "openalex" / slugify(query, 80) / mode / year_bucket / slugify(sort or "relevance", 80) / slugify(topic_filter or "no-topic", 80)
                    raw_path, write_raw = reusable_raw_path(
                        workspace,
                        manifest,
                        raw_dir(workspace) / "openalex" / slugify(query, 80) / f"{year_bucket}_{mode}_{slugify(sort or 'relevance', 40)}_{run}.jsonl",
                        bool(manifest.get("complete") and not refresh),
                    )
                    rows: list[dict[str, Any]] = []
                    provenance: list[dict[str, Any]] = []
                    cursor = "*"
                    status = "success"
                    error_message = ""
                    expected_total = None
                    pages_fetched = 0
                    bucket_seen = 0
                    cache_hit_all = bool(manifest.get("complete") and not refresh)
                    items: list[dict[str, Any]] = []

                    for page in range(1, max(1, pages) + 1):
                        page_cache = cache_root / f"page_{page}.json"
                        page_cache_hit = bool(page_cache.exists() and cache_hit_all)
                        data: dict[str, Any] = {}
                        if page_cache_hit:
                            cached = load_json_cache(page_cache)
                            data = cached if isinstance(cached, dict) else {}
                        else:
                            filters = [f"publication_year:{year_bucket}", "type:article"]
                            if topic_filter:
                                filters.append(f"primary_topic.id:{topic_filter}")
                            params: dict[str, Any] = {
                                "filter": ",".join(filters),
                                "per-page": min(max(1, spec_page_size), 100),
                                "cursor": cursor,
                                "select": clean_text(spec.get("select")) or openalex_select_fields(),
                            }
                            if mode == "semantic":
                                params["search.semantic"] = query
                            elif mode == "title":
                                params["filter"] = f"{params['filter']},title.search:{query}"
                            elif mode == "search":
                                params["search"] = query
                            else:
                                params["search.exact"] = query
                            if sort and sort != "relevance":
                                params["sort"] = sort
                            if api_key:
                                params["api_key"] = api_key
                            url = openalex_url(base_url, params)
                            try:
                                data = http_get_json(
                                    url,
                                    timeout=timeout,
                                    attempts=2,
                                    retry_status={429, 500, 502, 503, 504},
                                    backoff=3.0,
                                    budget=budget,
                                    source="openalex",
                                )
                                write_json(page_cache, data)
                            except Exception as exc:  # noqa: BLE001
                                error_message = str(exc)
                                if budget_exhausted_error(error_message):
                                    status = "budget_exhausted"
                                    stop_reason = error_message
                                else:
                                    status = "failed"
                                errors.append({"source": "openalex", "query": query, "page": page, "error": error_message})
                                break

                        if not isinstance(data, dict):
                            status = "failed"
                            error_message = "invalid response"
                            break
                        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
                        expected_total = meta.get("count", expected_total)
                        items = data.get("results") if isinstance(data.get("results"), list) else []
                        pages_fetched += 1
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            bucket_seen += 1
                            total_seen += 1
                            raw = openalex_item_to_raw(item)
                            force_include = strength == "must_recall"
                            if not raw.get("title") or (not force_include and not matches_topic(raw, topic)):
                                continue
                            wrapper, prov = wrap_candidate(
                                raw,
                                source_name="openalex",
                                source_type="openalex",
                                query=query,
                                source_run_id=run,
                                raw_path=raw_path.relative_to(workspace),
                                topic=topic,
                                source_item_id=clean_text(item.get("id")),
                                source_url=clean_text(item.get("id")),
                                force_include_reason="must_recall_query" if force_include else "",
                                paper_role_hint=clean_text(spec.get("paper_role")),
                            )
                            wrapper["query_mode"] = mode
                            wrapper["query_strength"] = strength
                            rows.append(wrapper)
                            provenance.append(prov)
                        cursor = clean_text(meta.get("next_cursor"))
                        if not cursor or not items:
                            break
                        if not page_cache_hit:
                            time.sleep(sleep_seconds)

                    if write_raw:
                        append_jsonl(raw_path, rows)
                        record_provenance(workspace, provenance)
                    total_kept += len(rows)
                    finished = now_iso()
                    source_exhausted = status == "success" and (not cursor or not items)
                    budget_complete = status == "success" and (pages_fetched >= pages or source_exhausted)
                    coverage[key] = {
                        "source": "openalex",
                        "query": query,
                        "mode": mode,
                        "sort": sort,
                        "topic_filter": topic_filter,
                        "year_bucket": year_bucket,
                        "complete": status == "success",
                        "status": status,
                        **coverage_health(status, cache_hit=cache_hit_all, source_exhausted=source_exhausted, budget_complete=budget_complete),
                        "cache_path": str(cache_root.relative_to(workspace)),
                        "result_count": bucket_seen,
                        "kept_count": len(rows),
                        "expected_total": expected_total,
                        "pages_fetched": pages_fetched,
                        "raw_output": str(raw_path.relative_to(workspace)),
                        "last_run_id": run,
                        "fetched_at": finished,
                        "errors": [error_message] if error_message else [],
                    }
                    record_source_run(workspace, {
                        "run_id": run,
                        "source": "openalex",
                        "operation": "fetch",
                        "query": query,
                        "bucket": year_bucket,
                        "params": {
                            "mode": mode,
                            "sort": sort,
                            "topic_filter": topic_filter,
                            "pages": pages,
                            "page_size": min(max(1, page_size), 100),
                            "authenticated": bool(api_key),
                        },
                        "started_at": started,
                        "finished_at": finished,
                        "status": status,
                        "cache_hit": cache_hit_all,
                        "cache_path": str(cache_root.relative_to(workspace)),
                        "fetched_count": bucket_seen,
                        "kept_count": len(rows),
                        "raw_output": str(raw_path.relative_to(workspace)),
                        "written_count": len(rows) if write_raw else 0,
                        "errors": [error_message] if error_message else [],
                    })
                    runs += 1
    if stop_reason:
        errors.append({"source": "openalex", "error": stop_reason})
        record_source_run(workspace, {
            "run_id": run_id("openalex_budget_exhausted"),
            "source": "openalex",
            "operation": "skip_remaining",
            "query": "",
            "bucket": "",
            "started_at": now_iso(),
            "finished_at": now_iso(),
            "status": "budget_exhausted",
            "errors": [stop_reason],
        })
    save_coverage(workspace, coverage)
    return {
        "source": "openalex",
        "runs": runs,
        "seen": total_seen,
        "kept": total_kept,
        "errors": errors[:20],
        "stopped_early": bool(stop_reason),
        "stop_reason": stop_reason,
    }


def _date_parts_year(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    parts = value.get("date-parts")
    if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
        return parse_year(parts[0][0])
    return None


def crossref_item_to_raw(item: dict[str, Any]) -> dict[str, Any]:
    title_values = item.get("title") if isinstance(item.get("title"), list) else []
    venue_values = item.get("container-title") if isinstance(item.get("container-title"), list) else []
    authors: list[str] = []
    for author in item.get("author") or []:
        if not isinstance(author, dict):
            continue
        name = clean_text(" ".join(
            part for part in [author.get("given"), author.get("family")] if clean_text(part)
        ))
        if name:
            authors.append(name)
    year = (
        _date_parts_year(item.get("published-print"))
        or _date_parts_year(item.get("published-online"))
        or _date_parts_year(item.get("issued"))
    )
    doi = clean_text(item.get("DOI"))
    raw = {
        "title": clean_text(title_values[0] if title_values else item.get("title")),
        "authors": "; ".join(authors),
        "year": year,
        "venue": clean_text(venue_values[0] if venue_values else ""),
        "abstract": clean_text(re.sub(r"<[^>]+>", " ", clean_text(item.get("abstract")))),
        "url": clean_text(item.get("URL") or (f"https://doi.org/{doi}" if doi else "")),
        "doi": doi,
        "crossref_type": clean_text(item.get("type")),
        "reference_count": int(float(item.get("reference-count") or 0)),
        "citation_count": int(float(item.get("is-referenced-by-count") or 0)),
    }
    return {k: v for k, v in raw.items() if v not in (None, "", [], {})}


def dblp_item_to_raw(item: dict[str, Any]) -> dict[str, Any]:
    info = item.get("info") if isinstance(item.get("info"), dict) else {}
    authors_value = info.get("authors")
    authors: list[str] = []
    if isinstance(authors_value, dict):
        author = authors_value.get("author")
        if isinstance(author, list):
            authors = [clean_text(a.get("text") if isinstance(a, dict) else a) for a in author]
        elif author:
            authors = [clean_text(author.get("text") if isinstance(author, dict) else author)]
    raw = {
        "title": clean_text(info.get("title")),
        "authors": "; ".join(a for a in authors if a),
        "year": parse_year(info.get("year")),
        "venue": clean_text(info.get("venue")),
        "url": clean_text(info.get("ee") or info.get("url")),
        "doi": clean_text(info.get("doi")),
        "dblp_key": clean_text(info.get("key")),
        "dblp_type": clean_text(info.get("type")),
    }
    return {k: v for k, v in raw.items() if v not in (None, "", [], {})}


def europepmc_item_to_raw(item: dict[str, Any]) -> dict[str, Any]:
    urls: list[str] = []
    url_list = item.get("fullTextUrlList") if isinstance(item.get("fullTextUrlList"), dict) else {}
    for row in url_list.get("fullTextUrl") or []:
        if isinstance(row, dict) and clean_text(row.get("url")):
            urls.append(clean_text(row.get("url")))
    doi = clean_text(item.get("doi"))
    pmid = clean_text(item.get("pmid"))
    raw = {
        "title": clean_text(item.get("title")),
        "authors": clean_text(item.get("authorString")),
        "year": parse_year(item.get("pubYear") or item.get("firstPublicationDate")),
        "publicationDate": clean_text(item.get("firstPublicationDate")),
        "venue": clean_text(item.get("journalTitle") or item.get("bookOrReportDetails")),
        "abstract": clean_text(item.get("abstractText")),
        "url": urls[0] if urls else clean_text(f"https://europepmc.org/article/{item.get('source')}/{pmid}" if pmid else ""),
        "pdf_url": next((u for u in urls if u.lower().endswith(".pdf")), ""),
        "doi": doi,
        "pmid": pmid,
        "pmcid": clean_text(item.get("pmcid")),
        "citation_count": int(float(item.get("citedByCount") or 0)),
        "europepmc_id": clean_text(item.get("id")),
    }
    return {k: v for k, v in raw.items() if v not in (None, "", [], {})}


def _source_year_bucket(years: list[int]) -> str:
    if not years:
        return ""
    return f"{years[0]}-{years[-1]}" if len(years) > 1 else str(years[0])


def _year_in_scope(raw: dict[str, Any], years: list[int]) -> bool:
    year = parse_year(raw.get("year") or raw.get("publicationDate") or raw.get("published"))
    return not year or not years or year in set(years)


def _sync_structured_search_source(
    workspace: Path,
    topic: dict[str, Any],
    years: list[int],
    *,
    source_name: str,
    queries: list[str],
    make_url: Any,
    items_from_payload: Any,
    item_to_raw: Any,
    item_id: Any,
    item_url: Any,
    next_token_from_payload: Any = None,
    max_results: int = 50,
    page_size: int = 25,
    max_pages: int = 1,
    refresh: bool = False,
    timeout: int = 35,
    sleep_seconds: float = 0.25,
    budget: RemoteBudget | None = None,
) -> dict[str, Any]:
    coverage = load_coverage(workspace)
    total_seen = 0
    total_kept = 0
    runs = 0
    errors: list[dict[str, Any]] = []
    year_bucket = _source_year_bucket(years)
    stop_reason = ""
    for query in queries:
        if stop_reason:
            break
        fetched_for_query = 0
        page_token = ""
        for page in range(max(1, max_pages)):
            if stop_reason or fetched_for_query >= max_results:
                break
            limit = min(max(1, page_size), max_results - fetched_for_query)
            offset = page * page_size
            run = run_id(f"{source_name}_{short_hash(query)}_{page}")
            key = cache_manifest_key(source_name, query, year_bucket, page)
            cache_file = cache_dir(workspace) / "discovery" / source_name / slugify(query, 80) / year_bucket / f"page_{page:04d}.json"
            manifest = coverage.get(key, {})
            cache_hit = bool(cache_file.exists() and manifest.get("status") == "success" and not refresh)
            started = now_iso()
            status = "success"
            payload: dict[str, Any] = {}
            error_message = ""
            url = make_url(query, year_bucket, limit, offset, page_token)
            if cache_hit:
                cached = load_json_cache(cache_file)
                payload = cached if isinstance(cached, dict) else {}
            else:
                try:
                    payload = http_get_json(
                        url,
                        timeout=timeout,
                        attempts=2,
                        retry_status={429, 500, 502, 503, 504},
                        backoff=max(1.0, sleep_seconds * 4),
                        budget=budget,
                        source=source_name,
                    )
                    if not isinstance(payload, dict):
                        payload = {}
                    write_json(cache_file, payload)
                except Exception as exc:  # noqa: BLE001
                    error_message = str(exc)
                    if budget_exhausted_error(error_message):
                        status = "budget_exhausted"
                        stop_reason = error_message
                    else:
                        status = "rate_limited" if transient_limit_error(error_message) else "failed"
                    payload = {}
            items = items_from_payload(payload)
            if not isinstance(items, list):
                items = []
            next_page_token = (
                clean_text(next_token_from_payload(payload))
                if callable(next_token_from_payload) and status == "success"
                else ""
            )
            raw_path, write_raw = reusable_raw_path(
                workspace,
                manifest,
                raw_dir(workspace) / source_name / slugify(query, 80) / f"{year_bucket}_page_{page:04d}_{run}.jsonl",
                cache_hit,
            )
            rows: list[dict[str, Any]] = []
            provenance: list[dict[str, Any]] = []
            if status == "success":
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    total_seen += 1
                    fetched_for_query += 1
                    raw = item_to_raw(item)
                    if not raw.get("title") or not _year_in_scope(raw, years) or not matches_topic(raw, topic):
                        continue
                    wrapper, prov = wrap_candidate(
                        raw,
                        source_name=source_name,
                        source_type=source_name,
                        query=query,
                        source_run_id=run,
                        raw_path=raw_path.relative_to(workspace),
                        topic=topic,
                        source_item_id=item_id(item, raw),
                        source_url=item_url(item, raw),
                    )
                    rows.append(wrapper)
                    provenance.append(prov)
                if write_raw:
                    append_jsonl(raw_path, rows)
                    record_provenance(workspace, provenance)
                total_kept += len(rows)
            else:
                errors.append({"source": source_name, "query": query, "page": page, "error": error_message})
            source_exhausted = status == "success" and (
                len(items) < limit
                or (callable(next_token_from_payload) and (not next_page_token or next_page_token == page_token))
            )
            budget_complete = status == "success" and (source_exhausted or page + 1 >= max_pages)
            finished = now_iso()
            coverage[key] = {
                "source": source_name,
                "query": query,
                "year_bucket": year_bucket,
                "page_index": page,
                "complete": status == "success",
                "status": status,
                **coverage_health(status, cache_hit=cache_hit, source_exhausted=source_exhausted if status == "success" else None, budget_complete=budget_complete),
                "cache_path": str(cache_file.relative_to(workspace)),
                "result_count": len(items),
                "kept_count": len(rows),
                "raw_output": str(raw_path.relative_to(workspace)),
                "last_run_id": run,
                "fetched_at": finished,
                "errors": [error_message] if error_message else [],
            }
            record_source_run(workspace, {
                "run_id": run,
                "source": source_name,
                "operation": "fetch",
                "query": query,
                "bucket": year_bucket,
                "params": {"page": page, "limit": limit, "offset": offset, "url": url},
                "started_at": started,
                "finished_at": finished,
                "status": status,
                "cache_hit": cache_hit,
                "cache_path": str(cache_file.relative_to(workspace)),
                "fetched_count": len(items),
                "kept_count": len(rows),
                "raw_output": str(raw_path.relative_to(workspace)),
                "written_count": len(rows) if write_raw else 0,
                "errors": [error_message] if error_message else [],
            })
            runs += 1
            if not cache_hit and status == "success":
                time.sleep(sleep_seconds)
            page_token = next_page_token
            if status != "success" or source_exhausted:
                break
    if stop_reason:
        errors.append({"source": source_name, "error": stop_reason})
    save_coverage(workspace, coverage)
    return {"source": source_name, "runs": runs, "seen": total_seen, "kept": total_kept, "errors": errors[:20], "stopped_early": bool(stop_reason), "stop_reason": stop_reason}


def sync_crossref(
    workspace: Path,
    topic: dict[str, Any],
    years: list[int],
    queries: list[str],
    *,
    base_url: str = CROSSREF_API,
    max_results: int = 50,
    page_size: int = 25,
    max_pages: int = 1,
    mailto: str = "",
    refresh: bool = False,
    timeout: int = 35,
    sleep_seconds: float = 0.25,
    budget: RemoteBudget | None = None,
) -> dict[str, Any]:
    def make_url(query: str, year_bucket: str, limit: int, offset: int, page_token: str = "") -> str:
        params: dict[str, Any] = {
            "query.title": query,
            "rows": limit,
            "offset": offset,
            "select": "DOI,title,author,published-print,published-online,issued,container-title,URL,type,abstract,reference-count,is-referenced-by-count",
        }
        filters = []
        if years:
            filters.extend([f"from-pub-date:{years[0]}-01-01", f"until-pub-date:{years[-1]}-12-31"])
        if filters:
            params["filter"] = ",".join(filters)
        if mailto:
            params["mailto"] = mailto
        return f"{base_url.rstrip('/')}/works?{urllib.parse.urlencode(params)}"

    return _sync_structured_search_source(
        workspace,
        topic,
        years,
        source_name="crossref",
        queries=queries,
        make_url=make_url,
        items_from_payload=lambda payload: ((payload.get("message") or {}).get("items") or []),
        item_to_raw=crossref_item_to_raw,
        item_id=lambda item, raw: clean_text(raw.get("doi")),
        item_url=lambda item, raw: clean_text(raw.get("url")),
        max_results=max_results,
        page_size=page_size,
        max_pages=max_pages,
        refresh=refresh,
        timeout=timeout,
        sleep_seconds=sleep_seconds,
        budget=budget,
    )


def sync_dblp(
    workspace: Path,
    topic: dict[str, Any],
    years: list[int],
    queries: list[str],
    *,
    base_url: str = DBLP_API,
    max_results: int = 50,
    page_size: int = 25,
    max_pages: int = 1,
    refresh: bool = False,
    timeout: int = 35,
    sleep_seconds: float = 0.25,
    budget: RemoteBudget | None = None,
) -> dict[str, Any]:
    def make_url(query: str, year_bucket: str, limit: int, offset: int, page_token: str = "") -> str:
        params = {"q": query, "format": "json", "h": limit, "f": offset}
        return f"{base_url}?{urllib.parse.urlencode(params)}"

    return _sync_structured_search_source(
        workspace,
        topic,
        years,
        source_name="dblp",
        queries=queries,
        make_url=make_url,
        items_from_payload=lambda payload: (((payload.get("result") or {}).get("hits") or {}).get("hit") or []),
        item_to_raw=dblp_item_to_raw,
        item_id=lambda item, raw: clean_text(raw.get("dblp_key") or raw.get("doi")),
        item_url=lambda item, raw: clean_text(raw.get("url")),
        max_results=max_results,
        page_size=page_size,
        max_pages=max_pages,
        refresh=refresh,
        timeout=timeout,
        sleep_seconds=sleep_seconds,
        budget=budget,
    )


def sync_europepmc(
    workspace: Path,
    topic: dict[str, Any],
    years: list[int],
    queries: list[str],
    *,
    base_url: str = EUROPEPMC_API,
    max_results: int = 50,
    page_size: int = 25,
    max_pages: int = 1,
    refresh: bool = False,
    timeout: int = 35,
    sleep_seconds: float = 0.25,
    budget: RemoteBudget | None = None,
) -> dict[str, Any]:
    def make_url(query: str, year_bucket: str, limit: int, offset: int, page_token: str = "") -> str:
        date_filter = f" FIRST_PDATE:[{years[0]}-01-01 TO {years[-1]}-12-31]" if years else ""
        params = {
            "query": f"{query}{date_filter}",
            "format": "json",
            "pageSize": limit,
            "cursorMark": page_token or "*",
            "resultType": "core",
        }
        return f"{base_url}?{urllib.parse.urlencode(params)}"

    return _sync_structured_search_source(
        workspace,
        topic,
        years,
        source_name="europepmc",
        queries=queries,
        make_url=make_url,
        items_from_payload=lambda payload: ((payload.get("resultList") or {}).get("result") or []),
        item_to_raw=europepmc_item_to_raw,
        item_id=lambda item, raw: clean_text(raw.get("pmid") or raw.get("pmcid") or raw.get("doi") or raw.get("europepmc_id")),
        item_url=lambda item, raw: clean_text(raw.get("url")),
        next_token_from_payload=lambda payload: payload.get("nextCursorMark"),
        max_results=max_results,
        page_size=page_size,
        max_pages=max_pages,
        refresh=refresh,
        timeout=timeout,
        sleep_seconds=sleep_seconds,
        budget=budget,
    )


def pubmed_summary_to_raw(item: dict[str, Any]) -> dict[str, Any]:
    authors = item.get("authors") if isinstance(item.get("authors"), list) else []
    author_text = "; ".join(clean_text(a.get("name")) for a in authors if isinstance(a, dict) and clean_text(a.get("name")))
    uid = clean_text(item.get("uid"))
    raw = {
        "title": clean_text(item.get("title")),
        "authors": author_text,
        "year": parse_year(item.get("pubdate") or item.get("epubdate") or item.get("sortpubdate")),
        "publicationDate": clean_text(item.get("pubdate") or item.get("epubdate")),
        "venue": clean_text(item.get("fulljournalname") or item.get("source")),
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/" if uid else "",
        "doi": next(
            (
                clean_text(article_id.get("value"))
                for article_id in item.get("articleids") or []
                if isinstance(article_id, dict) and clean_text(article_id.get("idtype")).lower() == "doi"
            ),
            "",
        ),
        "pmid": uid,
        "pubmed_id": uid,
    }
    return {k: v for k, v in raw.items() if v not in (None, "", [], {})}


def sync_pubmed(
    workspace: Path,
    topic: dict[str, Any],
    years: list[int],
    queries: list[str],
    *,
    base_url: str = PUBMED_EUTILS_API,
    max_results: int = 30,
    page_size: int = 20,
    refresh: bool = False,
    timeout: int = 35,
    sleep_seconds: float = 0.35,
    api_key: str = "",
    budget: RemoteBudget | None = None,
) -> dict[str, Any]:
    coverage = load_coverage(workspace)
    total_seen = 0
    total_kept = 0
    runs = 0
    errors: list[dict[str, Any]] = []
    year_bucket = _source_year_bucket(years)
    for query in queries:
        term = query
        if years:
            term = f"({query}) AND ({years[0]}:{years[-1]}[dp])"
        run = run_id(f"pubmed_{short_hash(query)}")
        key = cache_manifest_key("pubmed", query, year_bucket)
        cache_file = cache_dir(workspace) / "discovery" / "pubmed" / slugify(query, 80) / year_bucket / "summary.json"
        manifest = coverage.get(key, {})
        cache_hit = bool(cache_file.exists() and manifest.get("status") == "success" and not refresh)
        started = now_iso()
        status = "success"
        payload: dict[str, Any] = {}
        error_message = ""
        if cache_hit:
            cached = load_json_cache(cache_file)
            payload = cached if isinstance(cached, dict) else {}
        else:
            try:
                esearch_params: dict[str, Any] = {
                    "db": "pubmed",
                    "term": term,
                    "retmode": "json",
                    "retmax": min(max_results, page_size),
                }
                if api_key:
                    esearch_params["api_key"] = api_key
                esearch_url = f"{base_url.rstrip('/')}/esearch.fcgi?{urllib.parse.urlencode(esearch_params)}"
                esearch = http_get_json(
                    esearch_url,
                    timeout=timeout,
                    attempts=2,
                    retry_status={429, 500, 502, 503, 504},
                    backoff=2.0,
                    budget=budget,
                    source="pubmed",
                )
                ids = ((esearch.get("esearchresult") or {}).get("idlist") or []) if isinstance(esearch, dict) else []
                if ids:
                    summary_params: dict[str, Any] = {
                        "db": "pubmed",
                        "id": ",".join(ids[: min(max_results, page_size)]),
                        "retmode": "json",
                    }
                    if api_key:
                        summary_params["api_key"] = api_key
                    summary_url = f"{base_url.rstrip('/')}/esummary.fcgi?{urllib.parse.urlencode(summary_params)}"
                    payload = http_get_json(
                        summary_url,
                        timeout=timeout,
                        attempts=2,
                        retry_status={429, 500, 502, 503, 504},
                        backoff=2.0,
                        budget=budget,
                        source="pubmed",
                    )
                else:
                    payload = {"result": {"uids": []}}
                write_json(cache_file, payload)
            except Exception as exc:  # noqa: BLE001
                error_message = str(exc)
                status = "budget_exhausted" if budget_exhausted_error(error_message) else "rate_limited" if transient_limit_error(error_message) else "failed"
                payload = {"result": {"uids": []}}
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        uids = result.get("uids") if isinstance(result.get("uids"), list) else []
        raw_path, write_raw = reusable_raw_path(
            workspace,
            manifest,
            raw_dir(workspace) / "pubmed" / slugify(query, 80) / f"{year_bucket}_{run}.jsonl",
            cache_hit,
        )
        rows: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        if status == "success":
            for uid in uids[:max_results]:
                item = result.get(str(uid))
                if not isinstance(item, dict):
                    continue
                total_seen += 1
                raw = pubmed_summary_to_raw(item)
                if not raw.get("title") or not matches_topic(raw, topic):
                    continue
                wrapper, prov = wrap_candidate(
                    raw,
                    source_name="pubmed",
                    source_type="pubmed",
                    query=query,
                    source_run_id=run,
                    raw_path=raw_path.relative_to(workspace),
                    topic=topic,
                    source_item_id=clean_text(raw.get("pmid")),
                    source_url=clean_text(raw.get("url")),
                )
                rows.append(wrapper)
                provenance.append(prov)
            if write_raw:
                append_jsonl(raw_path, rows)
                record_provenance(workspace, provenance)
            total_kept += len(rows)
        else:
            errors.append({"source": "pubmed", "query": query, "error": error_message})
        finished = now_iso()
        source_exhausted = status == "success" and len(uids) < min(max_results, page_size)
        coverage[key] = {
            "source": "pubmed",
            "query": query,
            "year_bucket": year_bucket,
            "complete": status == "success",
            "status": status,
            **coverage_health(status, cache_hit=cache_hit, source_exhausted=source_exhausted if status == "success" else None, budget_complete=status == "success"),
            "cache_path": str(cache_file.relative_to(workspace)),
            "result_count": len(uids),
            "kept_count": len(rows),
            "raw_output": str(raw_path.relative_to(workspace)),
            "last_run_id": run,
            "fetched_at": finished,
            "errors": [error_message] if error_message else [],
        }
        record_source_run(workspace, {
            "run_id": run,
            "source": "pubmed",
            "operation": "fetch",
            "query": query,
            "bucket": year_bucket,
            "params": {"term": term, "max_results": max_results, "authenticated": bool(api_key)},
            "started_at": started,
            "finished_at": finished,
            "status": status,
            "cache_hit": cache_hit,
            "cache_path": str(cache_file.relative_to(workspace)),
            "fetched_count": len(uids),
            "kept_count": len(rows),
            "raw_output": str(raw_path.relative_to(workspace)),
            "written_count": len(rows) if write_raw else 0,
            "errors": [error_message] if error_message else [],
        })
        runs += 1
        if not cache_hit:
            time.sleep(sleep_seconds)
    save_coverage(workspace, coverage)
    return {"source": "pubmed", "runs": runs, "seen": total_seen, "kept": total_kept, "errors": errors[:20]}


def acl_paper_to_raw(paper: ET.Element, venue: str, year: int) -> dict[str, Any]:
    paper_id = clean_text(paper.attrib.get("id"))
    acl_id = f"{year}.{venue}-{paper_id}" if paper_id and not paper_id.startswith(str(year)) else paper_id
    authors: list[str] = []
    for author in paper.findall("author"):
        first = clean_text(author.findtext("first"))
        last = clean_text(author.findtext("last"))
        name = clean_text(" ".join(part for part in (first, last) if part))
        if name:
            authors.append(name)
    raw = {
        "title": clean_text(paper.findtext("title")),
        "authors": "; ".join(authors),
        "year": year,
        "venue": venue.upper(),
        "abstract": clean_text(paper.findtext("abstract")),
        "url": f"https://aclanthology.org/{acl_id}/" if acl_id else "",
        "pdf_url": f"https://aclanthology.org/{acl_id}.pdf" if acl_id else "",
        "acl_id": acl_id,
    }
    return {k: v for k, v in raw.items() if v not in (None, "", [], {})}


def sync_acl_anthology(
    workspace: Path,
    topic: dict[str, Any],
    years: list[int],
    venues: list[str],
    *,
    base_url: str = ACL_ANTHOLOGY_XML,
    refresh: bool = False,
    timeout: int = 35,
    sleep_seconds: float = 0.25,
    budget: RemoteBudget | None = None,
) -> dict[str, Any]:
    coverage = load_coverage(workspace)
    total_seen = 0
    total_kept = 0
    runs = 0
    errors: list[dict[str, Any]] = []
    for venue in venues:
        venue = clean_text(venue).lower()
        if not venue:
            continue
        for year in years:
            run = run_id(f"acl_anthology_{year}_{venue}")
            key = cache_manifest_key("acl_anthology", venue, year)
            cache_file = cache_dir(workspace) / "discovery" / "acl_anthology" / venue / f"{year}.xml"
            manifest = coverage.get(key, {})
            cache_hit = bool(cache_file.exists() and manifest.get("status") == "success" and not refresh)
            started = now_iso()
            status = "success"
            error_message = ""
            payload = b""
            if cache_hit:
                try:
                    payload = cache_file.read_bytes()
                except OSError as exc:
                    status = "failed"
                    error_message = str(exc)
            else:
                url = f"{base_url.rstrip('/')}/{year}.{venue}.xml"
                try:
                    payload = http_get_bytes(
                        url,
                        timeout=timeout,
                        attempts=2,
                        retry_status={429, 500, 502, 503, 504},
                        backoff=2.0,
                        budget=budget,
                        source="acl_anthology",
                    )
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_bytes(payload)
                except urllib.error.HTTPError as exc:
                    status = "missing" if exc.code == 404 else "failed"
                    error_message = f"HTTP {exc.code}: {exc.reason}"
                except Exception as exc:  # noqa: BLE001
                    error_message = str(exc)
                    status = "budget_exhausted" if budget_exhausted_error(error_message) else "rate_limited" if transient_limit_error(error_message) else "failed"
            papers: list[ET.Element] = []
            if status == "success":
                try:
                    root = ET.fromstring(payload)
                    papers = list(root.findall(".//paper"))
                except ET.ParseError as exc:
                    status = "failed"
                    error_message = f"XML parse error: {exc}"
            raw_path, write_raw = reusable_raw_path(
                workspace,
                manifest,
                raw_dir(workspace) / "acl_anthology" / venue / f"{year}_{run}.jsonl",
                cache_hit,
            )
            rows: list[dict[str, Any]] = []
            provenance: list[dict[str, Any]] = []
            if status == "success":
                for paper in papers:
                    total_seen += 1
                    raw = acl_paper_to_raw(paper, venue, year)
                    if not raw.get("title") or not matches_topic(raw, topic):
                        continue
                    wrapper, prov = wrap_candidate(
                        raw,
                        source_name="acl_anthology",
                        source_type="acl_anthology",
                        query=f"{year}.{venue}",
                        source_run_id=run,
                        raw_path=raw_path.relative_to(workspace),
                        topic=topic,
                        source_item_id=clean_text(raw.get("acl_id")),
                        source_url=clean_text(raw.get("url")),
                    )
                    rows.append(wrapper)
                    provenance.append(prov)
                if write_raw:
                    append_jsonl(raw_path, rows)
                    record_provenance(workspace, provenance)
                total_kept += len(rows)
            else:
                errors.append({"source": "acl_anthology", "venue": venue, "year": year, "error": error_message})
            finished = now_iso()
            coverage[key] = {
                "source": "acl_anthology",
                "venue": venue,
                "year": year,
                "complete": status in {"success", "missing"},
                "status": status,
                **coverage_health(status, cache_hit=cache_hit, source_exhausted=True if status == "success" else None, budget_complete=True, risk_override="low" if status == "missing" else None),
                "cache_path": str(cache_file.relative_to(workspace)),
                "result_count": len(papers),
                "kept_count": len(rows),
                "raw_output": str(raw_path.relative_to(workspace)),
                "last_run_id": run,
                "fetched_at": finished,
                "errors": [error_message] if error_message else [],
            }
            record_source_run(workspace, {
                "run_id": run,
                "source": "acl_anthology",
                "operation": "fetch",
                "query": f"{year}.{venue}",
                "bucket": str(year),
                "params": {"venue": venue, "year": year},
                "started_at": started,
                "finished_at": finished,
                "status": status,
                "cache_hit": cache_hit,
                "cache_path": str(cache_file.relative_to(workspace)),
                "fetched_count": len(papers),
                "kept_count": len(rows),
                "raw_output": str(raw_path.relative_to(workspace)),
                "written_count": len(rows) if write_raw else 0,
                "errors": [error_message] if error_message else [],
            })
            runs += 1
            if not cache_hit and status == "success":
                time.sleep(sleep_seconds)
    save_coverage(workspace, coverage)
    return {"source": "acl_anthology", "runs": runs, "seen": total_seen, "kept": total_kept, "errors": errors[:20]}


def _openreview_content_value(content: dict[str, Any], key: str) -> Any:
    value = content.get(key)
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def openreview_note_to_raw(note: dict[str, Any], venue_hint: str = "") -> dict[str, Any]:
    content = note.get("content") if isinstance(note.get("content"), dict) else {}
    authors_value = _openreview_content_value(content, "authors") or _openreview_content_value(content, "authorids") or []
    if isinstance(authors_value, list):
        authors = "; ".join(clean_text(a) for a in authors_value if clean_text(a))
    else:
        authors = clean_text(authors_value)
    title = clean_text(_openreview_content_value(content, "title"))
    abstract = clean_text(_openreview_content_value(content, "abstract"))
    venue = clean_text(venue_hint or note.get("domain") or note.get("venueid") or note.get("invitation"))
    note_id = clean_text(note.get("id"))
    raw = {
        "title": title,
        "authors": authors,
        "year": parse_year(note.get("cdate") or note.get("tcdate")),
        "venue": venue,
        "abstract": abstract,
        "url": f"https://openreview.net/forum?id={note_id}" if note_id else "",
        "pdf_url": f"https://openreview.net/pdf?id={note_id}" if note_id else "",
        "openreview_id": note_id,
        "openreview_invitation": clean_text(note.get("invitation")),
    }
    return {k: v for k, v in raw.items() if v not in (None, "", [], {})}


def sync_openreview(
    workspace: Path,
    topic: dict[str, Any],
    years: list[int],
    invitations: list[str],
    *,
    base_url: str = OPENREVIEW_API,
    limit: int = 50,
    max_pages: int = 1,
    refresh: bool = False,
    timeout: int = 35,
    sleep_seconds: float = 0.25,
    budget: RemoteBudget | None = None,
) -> dict[str, Any]:
    if not invitations:
        return {
            "source": "openreview",
            "runs": 0,
            "seen": 0,
            "kept": 0,
            "errors": [{"phase": "config", "error": "openreview invitations not configured"}],
            "status": "skipped_not_configured",
        }
    coverage = load_coverage(workspace)
    total_seen = 0
    total_kept = 0
    runs = 0
    errors: list[dict[str, Any]] = []
    for invitation in invitations:
        invitation = clean_text(invitation)
        if not invitation:
            continue
        for page in range(max(1, max_pages)):
            offset = page * limit
            run = run_id(f"openreview_{short_hash(invitation)}_{page}")
            key = cache_manifest_key("openreview", invitation, page)
            cache_file = cache_dir(workspace) / "discovery" / "openreview" / slugify(invitation, 80) / f"page_{page:04d}.json"
            manifest = coverage.get(key, {})
            cache_hit = bool(cache_file.exists() and manifest.get("status") == "success" and not refresh)
            started = now_iso()
            status = "success"
            error_message = ""
            payload: dict[str, Any] = {}
            if cache_hit:
                cached = load_json_cache(cache_file)
                payload = cached if isinstance(cached, dict) else {}
            else:
                params = {"invitation": invitation, "limit": limit, "offset": offset}
                url = f"{base_url.rstrip('/')}/notes?{urllib.parse.urlencode(params)}"
                try:
                    payload = http_get_json(
                        url,
                        timeout=timeout,
                        attempts=2,
                        retry_status={429, 500, 502, 503, 504},
                        backoff=2.0,
                        budget=budget,
                        source="openreview",
                    )
                    if not isinstance(payload, dict):
                        payload = {}
                    write_json(cache_file, payload)
                except Exception as exc:  # noqa: BLE001
                    error_message = str(exc)
                    status = "budget_exhausted" if budget_exhausted_error(error_message) else "rate_limited" if transient_limit_error(error_message) else "failed"
                    payload = {}
            notes = payload.get("notes") if isinstance(payload.get("notes"), list) else []
            raw_path, write_raw = reusable_raw_path(
                workspace,
                manifest,
                raw_dir(workspace) / "openreview" / slugify(invitation, 80) / f"page_{page:04d}_{run}.jsonl",
                cache_hit,
            )
            rows: list[dict[str, Any]] = []
            provenance: list[dict[str, Any]] = []
            if status == "success":
                for note in notes:
                    if not isinstance(note, dict):
                        continue
                    total_seen += 1
                    raw = openreview_note_to_raw(note, invitation)
                    if not raw.get("title") or not _year_in_scope(raw, years) or not matches_topic(raw, topic):
                        continue
                    wrapper, prov = wrap_candidate(
                        raw,
                        source_name="openreview",
                        source_type="openreview",
                        query=invitation,
                        source_run_id=run,
                        raw_path=raw_path.relative_to(workspace),
                        topic=topic,
                        source_item_id=clean_text(raw.get("openreview_id")),
                        source_url=clean_text(raw.get("url")),
                    )
                    rows.append(wrapper)
                    provenance.append(prov)
                if write_raw:
                    append_jsonl(raw_path, rows)
                    record_provenance(workspace, provenance)
                total_kept += len(rows)
            else:
                errors.append({"source": "openreview", "invitation": invitation, "page": page, "error": error_message})
            finished = now_iso()
            source_exhausted = status == "success" and len(notes) < limit
            coverage[key] = {
                "source": "openreview",
                "invitation": invitation,
                "page_index": page,
                "complete": status == "success",
                "status": status,
                **coverage_health(status, cache_hit=cache_hit, source_exhausted=source_exhausted if status == "success" else None, budget_complete=status == "success"),
                "cache_path": str(cache_file.relative_to(workspace)),
                "result_count": len(notes),
                "kept_count": len(rows),
                "raw_output": str(raw_path.relative_to(workspace)),
                "last_run_id": run,
                "fetched_at": finished,
                "errors": [error_message] if error_message else [],
            }
            record_source_run(workspace, {
                "run_id": run,
                "source": "openreview",
                "operation": "fetch",
                "query": invitation,
                "bucket": str(page),
                "params": {"invitation": invitation, "limit": limit, "offset": offset},
                "started_at": started,
                "finished_at": finished,
                "status": status,
                "cache_hit": cache_hit,
                "cache_path": str(cache_file.relative_to(workspace)),
                "fetched_count": len(notes),
                "kept_count": len(rows),
                "raw_output": str(raw_path.relative_to(workspace)),
                "written_count": len(rows) if write_raw else 0,
                "errors": [error_message] if error_message else [],
            })
            runs += 1
            if not cache_hit:
                time.sleep(sleep_seconds)
            if status != "success" or source_exhausted:
                break
    save_coverage(workspace, coverage)
    return {"source": "openreview", "runs": runs, "seen": total_seen, "kept": total_kept, "errors": errors[:20]}


def sync_semantic_scholar(
    workspace: Path,
    topic: dict[str, Any],
    years: list[int],
    queries: list[str],
    max_results: int = 1000,
    page_size: int = 100,
    api_key: str = "",
    base_url: str = SEMANTIC_SCHOLAR_API,
    mode: str = "bulk",
    year_strategy: str = "range",
    sorts: list[str] | None = None,
    max_pages: int = 1,
    refresh: bool = False,
    timeout: int = 45,
    sleep_seconds: float = 6.0,
    no_key_sleep_seconds: float = 6.0,
    retry_attempts: int | None = None,
    rate_limit_error_limit: int = 1,
    max_kept_per_run: int | None = None,
    budget: RemoteBudget | None = None,
) -> dict[str, Any]:
    coverage = load_coverage(workspace)
    api_key = clean_text(api_key)
    mode = clean_text(mode).lower() or "bulk"
    if mode not in {"bulk", "relevance"}:
        raise ValueError(f"不支持的 Semantic Scholar mode：{mode}")
    fields = "paperId,title,abstract,url,year,citationCount,referenceCount,publicationVenue,venue,authors,externalIds,publicationDate,openAccessPdf"
    optional_without_key = semantic_scholar_optional_without_key(api_key, base_url)
    auth_status = semantic_scholar_auth_status(api_key, base_url)
    headers = {"x-api-key": api_key} if api_key and "semanticscholar.org" in base_url else {}
    if api_key and "semanticscholar.org" not in base_url:
        headers = {"Authorization": f"Bearer {api_key}"}
    effective_sleep = sleep_seconds if api_key else max(sleep_seconds, no_key_sleep_seconds)
    attempts = retry_attempts if retry_attempts and retry_attempts > 0 else (2 if api_key else 1)
    retry_status = {429, 500, 502, 503, 504} if not api_key else {403, 429, 500, 502, 503, 504}
    year_buckets = semantic_year_buckets(years, year_strategy)
    active_sorts = [clean_text(sort) for sort in (sorts or ["citationCount:desc", "publicationDate:desc"]) if clean_text(sort)]
    if mode == "relevance":
        active_sorts = [""]
    total_kept = 0
    total_seen = 0
    runs = 0
    errors: list[dict[str, Any]] = []
    consecutive_limit_errors = 0
    stop_reason = ""

    for query in queries:
        if stop_reason:
            break
        for year_bucket in year_buckets:
            if stop_reason:
                break
            for sort in active_sorts:
                if stop_reason:
                    break
                fetched_for_bucket = 0
                page_index = 0
                next_token = ""
                offset = 0
                while page_index < max_pages and fetched_for_bucket < max_results:
                    if stop_reason:
                        break
                    limit = min(page_size, max_results - fetched_for_bucket)
                    sort_slug = slugify(sort or "relevance", 60)
                    page_label = f"page_{page_index:04d}" if mode == "bulk" else f"offset_{offset:05d}"
                    run = run_id(f"semanticscholar_{short_hash(query)}_{year_bucket}_{sort_slug}_{page_label}")
                    key = cache_manifest_key("semanticscholar", mode, query, year_bucket, sort_slug, page_label)
                    cache_file = cache_dir(workspace) / "discovery" / "semanticscholar" / slugify(query, 80) / str(year_bucket) / sort_slug / f"{page_label}.json"
                    manifest = coverage.get(key, {})
                    cache_hit = bool(cache_file.exists() and manifest.get("status") == "success" and not refresh)
                    started = now_iso()
                    status = "success"
                    payload: dict[str, Any] = {}
                    error_message = ""
                    if cache_hit:
                        cached = load_json_cache(cache_file)
                        payload = cached if isinstance(cached, dict) else {}
                    else:
                        url = semantic_scholar_url(
                            base_url,
                            query,
                            year_bucket,
                            fields,
                            mode,
                            sort=sort,
                            token=next_token,
                            limit=limit,
                            offset=offset,
                        )
                        try:
                            payload = http_get_json(
                                url,
                                timeout=timeout,
                                headers=headers,
                                attempts=attempts,
                                retry_status=retry_status,
                                backoff=max(6.0, effective_sleep * 2),
                                budget=budget,
                                source="semanticscholar",
                            )
                            if not isinstance(payload, dict):
                                payload = {"data": []}
                            write_json(cache_file, payload)
                        except Exception as exc:  # noqa: BLE001
                            error_message = str(exc)
                            if budget_exhausted_error(error_message):
                                status = "budget_exhausted"
                                stop_reason = error_message
                            else:
                                status = "rate_limited" if transient_limit_error(error_message) else "failed"
                            payload = {"data": []}
                    all_data = payload.get("data") if isinstance(payload.get("data"), list) else []
                    remaining = max(0, max_results - fetched_for_bucket)
                    data = all_data[:remaining] if remaining else []
                    raw_path, write_raw = reusable_raw_path(
                        workspace,
                        manifest,
                        raw_dir(workspace) / "semanticscholar" / slugify(query, 80) / f"{year_bucket}_{sort_slug}_{page_label}_{run}.jsonl",
                        cache_hit,
                    )
                    rows: list[dict[str, Any]] = []
                    provenance: list[dict[str, Any]] = []
                    kept_before_cap = 0
                    if status == "success":
                        consecutive_limit_errors = 0
                        for item in data:
                            if not isinstance(item, dict):
                                continue
                            total_seen += 1
                            raw = ss_item_to_raw(item)
                            if not raw.get("title") or not matches_topic(raw, topic):
                                continue
                            wrapper, prov = wrap_candidate(
                                raw,
                                source_name="semanticscholar",
                                source_type="semanticscholar",
                                query=query,
                                source_run_id=run,
                                raw_path=raw_path.relative_to(workspace),
                                topic=topic,
                                source_item_id=clean_text(item.get("paperId")),
                                source_url=clean_text(item.get("url")),
                            )
                            rows.append(wrapper)
                            provenance.append(prov)
                        rows, provenance, kept_before_cap = _cap_source_candidates(
                            rows,
                            provenance,
                            max_kept_per_run,
                        )
                        if write_raw:
                            append_jsonl(raw_path, rows)
                            record_provenance(workspace, provenance)
                        total_kept += len(rows)
                    else:
                        errors.append({"source": "semanticscholar", "query": query, "bucket": year_bucket, "page": page_index, "sort": sort, "error": error_message})
                        if status == "budget_exhausted":
                            stop_reason = error_message
                        elif status == "rate_limited":
                            consecutive_limit_errors += 1
                            if rate_limit_error_limit > 0 and consecutive_limit_errors >= rate_limit_error_limit:
                                stop_reason = (
                                    "semanticscholar rate-limit/timeout breaker after "
                                    f"{consecutive_limit_errors} consecutive failures"
                                )
                        else:
                            consecutive_limit_errors = 0
                    response_token = clean_text(payload.get("token"))
                    next_offset = payload.get("next")
                    processed_count = len(data)
                    fetched_for_bucket += processed_count
                    source_exhausted = (
                        status == "success" and (
                        (mode == "bulk" and not response_token)
                        or (mode == "relevance" and (not next_offset or len(all_data) < limit))
                        )
                    )
                    bucket_complete = status == "success" and (
                        (mode == "bulk" and (not response_token or page_index + 1 >= max_pages or fetched_for_bucket >= max_results))
                        or (mode == "relevance" and (not next_offset or fetched_for_bucket >= max_results or len(all_data) < limit))
                    )
                    budget_complete = status == "success" and (page_index + 1 >= max_pages or fetched_for_bucket >= max_results or source_exhausted)
                    auth_problem = semantic_scholar_auth_problem(api_key, base_url, error_message)
                    auth_hint = semantic_scholar_auth_hint(auth_problem)
                    coverage[key] = {
                        "source": "semanticscholar",
                        "optional_source": optional_without_key,
                        "optional_reason": (
                            "semanticscholar_no_api_key" if optional_without_key else ""
                        ),
                        "authenticated": bool(api_key),
                        "auth_status": auth_status,
                        "auth_problem": auth_problem,
                        "auth_hint": auth_hint,
                        "mode": mode,
                        "query": query,
                        "year": year_bucket,
                        "sort": sort,
                        "page_index": page_index,
                        "offset": offset if mode == "relevance" else None,
                        "limit": limit if mode == "relevance" else None,
                        "complete": status == "success",
                        "bucket_complete": bucket_complete,
                        "status": status,
                        **coverage_health(
                            status,
                            cache_hit=cache_hit,
                            source_exhausted=source_exhausted if status == "success" else None,
                            budget_complete=budget_complete,
                            risk_override=(
                                "low"
                                if optional_without_key
                                else "medium" if status == "rate_limited" else None
                            ),
                        ),
                        "cache_path": str(cache_file.relative_to(workspace)),
                        "result_count": len(all_data),
                        "processed_count": processed_count,
                        "kept_count": len(rows),
                        "kept_before_cap": kept_before_cap,
                        "max_kept_per_run": max_kept_per_run,
                        "raw_output": str(raw_path.relative_to(workspace)),
                        "expected_total": payload.get("total"),
                        "next_token": "present" if response_token else "",
                        "next_offset": next_offset,
                        "target_max": max_results,
                        "target_max_pages": max_pages if mode == "bulk" else None,
                        "last_run_id": run,
                        "fetched_at": now_iso(),
                        "errors": [error_message] if error_message else [],
                    }
                    if stop_reason:
                        coverage[key]["rate_limit_breaker_triggered"] = True
                        coverage[key]["breaker_reason"] = stop_reason
                    record_source_run(workspace, {
                        "run_id": run,
                        "source": "semanticscholar",
                        "operation": "fetch",
                        "query": query,
                        "bucket": str(year_bucket),
                        "params": {
                            "mode": mode,
                            "year": year_bucket,
                            "sort": sort,
                            "page_index": page_index,
                            "offset": offset if mode == "relevance" else None,
                            "limit": limit if mode == "relevance" else None,
                            "base_url": base_url,
                            "authenticated": bool(api_key),
                            "auth_status": auth_status,
                            "auth_problem": auth_problem,
                        },
                        "started_at": started,
                        "finished_at": now_iso(),
                        "status": status,
                        "cache_hit": cache_hit,
                        "cache_path": str(cache_file.relative_to(workspace)),
                        "fetched_count": len(all_data),
                        "processed_count": processed_count,
                        "kept_count": len(rows),
                        "kept_before_cap": kept_before_cap,
                        "max_kept_per_run": max_kept_per_run,
                        "raw_output": str(raw_path.relative_to(workspace)),
                        "written_count": len(rows) if write_raw else 0,
                        "errors": [error_message] if error_message else [],
                    })
                    runs += 1
                    save_coverage(workspace, coverage)
                    budget_exhausted = budget_exhausted_error(error_message)
                    if not cache_hit and not budget_exhausted:
                        time.sleep(effective_sleep)
                    if status != "success" or bucket_complete:
                        break
                    if mode == "bulk":
                        next_token = response_token
                        page_index += 1
                    else:
                        offset = int(next_offset or (offset + limit))
                        page_index += 1
                    if not budget_exhausted:
                        time.sleep(min(1.0, effective_sleep))
    if stop_reason:
        errors.append({"source": "semanticscholar", "error": stop_reason})
        stop_status = "budget_exhausted" if budget_exhausted_error(stop_reason) else "rate_limited"
        record_source_run(workspace, {
            "run_id": run_id(f"semanticscholar_{stop_status}"),
            "source": "semanticscholar",
            "operation": "skip_remaining",
            "query": "",
            "bucket": "",
            "started_at": now_iso(),
            "finished_at": now_iso(),
            "status": stop_status,
            "errors": [stop_reason],
        })
    return {
        "source": "semanticscholar",
        "runs": runs,
        "seen": total_seen,
        "kept": total_kept,
        "errors": errors[:20],
        "stopped_early": bool(stop_reason),
        "stop_reason": stop_reason,
    }


def arxiv_year_query(query: str, year: int) -> str:
    query = clean_text(query)
    if "submittedDate:[" in query:
        return query
    return f"({query}) AND submittedDate:[{year}01010000 TO {year}12312359]"


def arxiv_search(
    query: str,
    start: int = 0,
    max_results: int = 100,
    sort_by: str = "relevance",
    timeout: int = 35,
    retry_attempts: int = 1,
    budget: RemoteBudget | None = None,
) -> dict[str, Any]:
    params = urllib.parse.urlencode({
        "search_query": query,
        "start": start,
        "max_results": max_results,
        "sortBy": sort_by,
        "sortOrder": "descending",
    })
    url = f"https://export.arxiv.org/api/query?{params}"
    payload = http_get_bytes(
        url,
        timeout=timeout,
        headers={"Accept": "application/atom+xml"},
        attempts=max(1, retry_attempts),
        retry_status={500, 502, 503, 504},
        backoff=8.0,
        budget=budget,
        source="arxiv",
    )
    root = ET.fromstring(payload)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    }
    total = root.findtext("opensearch:totalResults", default="", namespaces=ns)
    items: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns):
        arxiv_id = entry.findtext("atom:id", default="", namespaces=ns).split("/abs/")[-1]
        arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
        published = entry.findtext("atom:published", default="", namespaces=ns)
        updated = entry.findtext("atom:updated", default="", namespaces=ns)
        authors = "; ".join(
            clean_text(author.findtext("atom:name", default="", namespaces=ns))
            for author in entry.findall("atom:author", ns)
        )
        items.append({
            "title": clean_text(entry.findtext("atom:title", default="", namespaces=ns)),
            "authors": authors,
            "year": parse_year(published) or parse_year(updated),
            "venue": "arXiv",
            "abstract": clean_text(entry.findtext("atom:summary", default="", namespaces=ns)),
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            "arxiv_id": arxiv_id,
            "doi": f"10.48550/arXiv.{arxiv_id}",
            "published": published[:10],
            "updated": updated[:10],
            "categories": [cat.attrib.get("term", "") for cat in entry.findall("atom:category", ns)],
        })
    return {"total": int(total or 0), "data": items}


def transient_limit_error(message: str) -> bool:
    lowered = clean_text(message).lower()
    return (
        "429" in lowered
        or "too many requests" in lowered
        or "read operation timed out" in lowered
        or "timed out" in lowered
        or "timeout" in lowered
        or "temporarily unavailable" in lowered
        or "connection reset" in lowered
    )


def budget_exhausted_error(message: str) -> bool:
    return "remote call budget exceeded" in clean_text(message).lower()


def _positive_int(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _config_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    lowered = clean_text(value).lower()
    if not lowered:
        return default
    return lowered not in {"0", "false", "no", "off"}


def _source_budget_override(cfg: dict[str, Any], source: str) -> int | None:
    for key in ("source_budgets", "budgets"):
        values = cfg.get(key)
        if isinstance(values, dict) and source in values:
            return _positive_int(values.get(source))
    source_cfg = cfg.get(source)
    if isinstance(source_cfg, dict):
        return _positive_int(source_cfg.get("max_remote_calls"))
    return None


def _default_source_budget_limit(
    source: str,
    total_limit: int | None,
    *,
    years: list[int],
    venues: list[str] | None = None,
) -> int | None:
    if total_limit is None:
        return None
    if source == "paperlists":
        venue_years = len(years) * len(venues or [])
        cap = max(8, math.ceil(total_limit * 0.25))
        return min(venue_years, cap) if venue_years else cap
    if source == "openalex":
        return max(16, math.ceil(total_limit * 0.35))
    if source in {"crossref", "dblp"}:
        return max(6, math.ceil(total_limit * 0.15))
    if source == "acl_anthology":
        return max(6, math.ceil(total_limit * 0.18))
    if source in {"europepmc", "pubmed"}:
        return max(4, math.ceil(total_limit * 0.10))
    if source == "openreview":
        return max(4, math.ceil(total_limit * 0.10))
    if source == "arxiv":
        return max(24, math.ceil(total_limit * 0.50))
    if source == "semanticscholar":
        return max(6, math.ceil(total_limit * 0.18))
    if source == "gemini_search":
        return max(2, math.ceil(total_limit * 0.10))
    return None


def arxiv_transient_limit_error(message: str) -> bool:
    return transient_limit_error(message)


def sync_arxiv_discovery(
    workspace: Path,
    topic: dict[str, Any],
    years: list[int],
    queries: list[str],
    max_results: int = 100,
    page_size: int = 100,
    sort_by: str = "relevance",
    refresh: bool = False,
    timeout: int = 35,
    sleep_seconds: float = 3.2,
    rate_limit_error_limit: int = 3,
    retry_attempts: int = 1,
    recent_year_count: int | None = None,
    budget: RemoteBudget | None = None,
    recent_first: bool = True,
) -> dict[str, Any]:
    coverage = load_coverage(workspace)
    total_kept = 0
    total_seen = 0
    runs = 0
    errors: list[dict[str, Any]] = []
    consecutive_limit_errors = 0
    stop_reason = ""
    active_years = sorted(years, reverse=True) if recent_first else list(years)
    if recent_year_count and recent_year_count > 0:
        active_years = active_years[:recent_year_count]
    for query in queries:
        if stop_reason:
            break
        for year in active_years:
            if stop_reason:
                break
            offset = 0
            while offset < max_results:
                if stop_reason:
                    break
                limit = min(page_size, max_results - offset)
                year_query = arxiv_year_query(query, year)
                run = run_id(f"arxiv_{short_hash(year_query)}_{offset}")
                key = cache_manifest_key("arxiv", year_query, sort_by, offset)
                cache_file = cache_dir(workspace) / "discovery" / "arxiv" / slugify(query, 80) / str(year) / f"{sort_by}_{offset:05d}.json"
                manifest = coverage.get(key, {})
                cache_hit = bool(cache_file.exists() and manifest.get("status") == "success" and not refresh)
                started = now_iso()
                status = "success"
                payload: dict[str, Any] = {}
                error_message = ""
                if cache_hit:
                    cached = load_json_cache(cache_file)
                    payload = cached if isinstance(cached, dict) else {"data": []}
                else:
                    try:
                        payload = arxiv_search(
                            year_query,
                            start=offset,
                            max_results=limit,
                            sort_by=sort_by,
                            timeout=timeout,
                            retry_attempts=retry_attempts,
                            budget=budget,
                        )
                        write_json(cache_file, payload)
                    except Exception as exc:  # noqa: BLE001
                        error_message = str(exc)
                        if budget_exhausted_error(error_message):
                            status = "budget_exhausted"
                            stop_reason = error_message
                        else:
                            status = "failed"
                        payload = {"data": [], "total": 0}
                data = payload.get("data") if isinstance(payload.get("data"), list) else []
                raw_path, write_raw = reusable_raw_path(
                    workspace,
                    manifest,
                    raw_dir(workspace) / "arxiv" / slugify(query, 80) / f"{year}_{offset:05d}_{run}.jsonl",
                    cache_hit,
                )
                rows: list[dict[str, Any]] = []
                provenance: list[dict[str, Any]] = []
                if status == "success":
                    # A zero-result arXiv response during a rate-limit window
                    # should not reset the breaker. In practice export.arxiv
                    # often alternates 429s with empty successes; treating the
                    # latter as healthy makes one source stall the full run.
                    if data:
                        consecutive_limit_errors = 0
                    for raw in data:
                        total_seen += 1
                        if not raw.get("title") or not matches_topic(raw, topic):
                            continue
                        wrapper, prov = wrap_candidate(
                            raw,
                            source_name="arxiv",
                            source_type="arxiv",
                            query=query,
                            source_run_id=run,
                            raw_path=raw_path.relative_to(workspace),
                            topic=topic,
                            source_item_id=clean_text(raw.get("arxiv_id")),
                            source_url=clean_text(raw.get("url")),
                        )
                        rows.append(wrapper)
                        provenance.append(prov)
                    if write_raw:
                        append_jsonl(raw_path, rows)
                        record_provenance(workspace, provenance)
                    total_kept += len(rows)
                else:
                    errors.append({"source": "arxiv", "query": query, "year": year, "offset": offset, "error": error_message})
                    if budget_exhausted_error(error_message):
                        stop_reason = error_message
                    elif arxiv_transient_limit_error(error_message):
                        consecutive_limit_errors += 1
                        if (
                            rate_limit_error_limit > 0
                            and consecutive_limit_errors >= rate_limit_error_limit
                        ):
                            stop_reason = (
                                f"arxiv rate-limit/timeout breaker after "
                                f"{consecutive_limit_errors} consecutive failures"
                            )
                    else:
                        consecutive_limit_errors = 0
                total = int(payload.get("total") or 0)
                source_exhausted = status == "success" and (offset + limit >= total or len(data) < limit)
                bucket_complete = status == "success" and (offset + limit >= min(total, max_results) or len(data) < limit)
                budget_complete = status == "success" and bucket_complete
                transient_error = bool(error_message and arxiv_transient_limit_error(error_message))
                coverage[key] = {
                    "source": "arxiv",
                    "query": query,
                    "year": year,
                    "offset": offset,
                    "limit": limit,
                    "complete": status == "success",
                    "bucket_complete": bucket_complete,
                    "status": status,
                    **coverage_health(
                        status,
                        cache_hit=cache_hit,
                        source_exhausted=source_exhausted,
                        budget_complete=budget_complete,
                        risk_override="medium" if transient_error else None,
                    ),
                    "cache_path": str(cache_file.relative_to(workspace)),
                    "result_count": len(data),
                    "kept_count": len(rows),
                    "raw_output": str(raw_path.relative_to(workspace)),
                    "expected_total": total,
                    "next_offset": None if bucket_complete else offset + limit,
                    "target_max": max_results,
                    "last_run_id": run,
                    "fetched_at": now_iso(),
                    "errors": [error_message] if error_message else [],
                }
                if transient_error:
                    coverage[key]["transient_error"] = True
                    coverage[key]["retry_recommended"] = True
                if stop_reason:
                    coverage[key]["rate_limit_breaker_triggered"] = True
                    coverage[key]["breaker_reason"] = stop_reason
                record_source_run(workspace, {
                    "run_id": run,
                    "source": "arxiv",
                    "operation": "fetch",
                    "query": query,
                    "bucket": str(year),
                    "params": {"year": year, "limit": limit, "offset": offset, "sort_by": sort_by},
                    "started_at": started,
                    "finished_at": now_iso(),
                    "status": status,
                    "cache_hit": cache_hit,
                    "cache_path": str(cache_file.relative_to(workspace)),
                    "fetched_count": len(data),
                    "kept_count": len(rows),
                    "raw_output": str(raw_path.relative_to(workspace)),
                    "written_count": len(rows) if write_raw else 0,
                    "errors": [error_message] if error_message else [],
                })
                runs += 1
                save_coverage(workspace, coverage)
                # Skip the inter-call sleep when the failure was the budget
                # exhaustion check itself — there was no real network call,
                # so backoff is just dead time. Same goes for cache hits.
                budget_exhausted = budget_exhausted_error(error_message)
                if not cache_hit and not budget_exhausted:
                    time.sleep(sleep_seconds)
                if status != "success" or bucket_complete:
                    break
                offset += limit
    if stop_reason:
        errors.append({"source": "arxiv", "error": stop_reason})
        stop_status = "budget_exhausted" if budget_exhausted_error(stop_reason) else "rate_limited"
        record_source_run(workspace, {
            "run_id": run_id(f"arxiv_{stop_status}"),
            "source": "arxiv",
            "operation": "skip_remaining",
            "query": "",
            "bucket": "",
            "started_at": now_iso(),
            "finished_at": now_iso(),
            "status": stop_status,
            "errors": [stop_reason],
        })
    return {
        "source": "arxiv",
        "runs": runs,
        "seen": total_seen,
        "kept": total_kept,
        "errors": errors[:20],
        "stopped_early": bool(stop_reason),
        "stop_reason": stop_reason,
    }


def make_coverage_report(workspace: Path) -> dict[str, Any]:
    coverage = load_coverage(workspace)
    papers = read_json(data_dir(workspace) / "papers.json", [])
    if not isinstance(papers, list):
        papers = []
    by_year: dict[str, int] = {}
    by_venue: dict[str, int] = {}
    by_tag: dict[str, int] = {}
    by_source: dict[str, int] = {}
    metadata_gaps = {"no_abstract": 0, "no_external_id": 0, "no_pdf_or_url": 0}
    for paper in papers:
        year = str(paper.get("year") or "unknown")
        by_year[year] = by_year.get(year, 0) + 1
        venue = clean_text(paper.get("venue") or "N/A")
        by_venue[venue] = by_venue.get(venue, 0) + 1
        for tag in paper.get("tags", []) or []:
            by_tag[tag] = by_tag.get(tag, 0) + 1
        for source in paper.get("sources", []) or []:
            by_source[source] = by_source.get(source, 0) + 1
        ids = paper.get("ids") or {}
        urls = paper.get("urls") or {}
        if not clean_text(paper.get("abstract")):
            metadata_gaps["no_abstract"] += 1
        if not any(ids.get(key) for key in ("doi", "arxiv", "acl", "semantic_scholar", "openreview", "openalex", "dblp", "pmid", "pmcid", "europepmc")):
            metadata_gaps["no_external_id"] += 1
        if not (urls.get("landing") or urls.get("pdf") or paper.get("url") or paper.get("pdf_url")):
            metadata_gaps["no_pdf_or_url"] += 1
    incomplete = [
        {key: manifest}
        for key, manifest in coverage.items()
        if isinstance(manifest, dict)
        and (
            clean_text(manifest.get("execution_status") or manifest.get("status")) in {"failed", "rate_limited", "budget_exhausted"}
            or manifest.get("source_exhausted") is False
        )
    ]
    status_counts: dict[str, int] = {}
    risk_counts: dict[str, int] = {}
    for manifest in coverage.values():
        if not isinstance(manifest, dict):
            continue
        status = clean_text(manifest.get("execution_status") or manifest.get("status") or "unknown")
        risk = clean_text(manifest.get("coverage_risk") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        risk_counts[risk] = risk_counts.get(risk, 0) + 1
    report = {
        "built_at": now_iso(),
        "paper_count": len(papers),
        "coverage_entries": len(coverage),
        "incomplete_source_buckets": len(incomplete),
        "source_status_counts": dict(sorted(status_counts.items())),
        "coverage_risk_counts": dict(sorted(risk_counts.items())),
        "by_year": dict(sorted(by_year.items(), reverse=True)),
        "top_venues": sorted(by_venue.items(), key=lambda item: item[1], reverse=True)[:30],
        "top_tags": sorted(by_tag.items(), key=lambda item: item[1], reverse=True)[:50],
        "top_sources": sorted(by_source.items(), key=lambda item: item[1], reverse=True)[:30],
        "metadata_gaps": metadata_gaps,
        "incomplete_examples": incomplete[:20],
        "next_actions": [
            "续抓 incomplete source buckets",
            "检查 metadata_gaps 中无外部 ID 或无摘要的论文",
            "按 by_year/by_venue/by_tag 查缺口后追加对应 source/query/bucket",
        ],
    }
    write_json(manifests_dir(workspace) / "coverage_report.json", report)
    return report


def run_discovery(
    workspace: Path,
    sources: list[str] | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    refresh: bool = False,
    build: bool = True,
    catalog: bool = True,
    paperlists_venues: list[str] | None = None,
    timeout: int = 35,
    max_remote_calls: int | None = None,
) -> dict[str, Any]:
    ensure_discovery_dirs(workspace)
    topic = load_topic_config(workspace)
    cfg = source_config(workspace)
    years = year_range(topic, min_year, max_year)
    selected = sources or cfg.get("sources") or default_discovery_sources(topic, cfg)
    selected = [clean_text(source).lower() for source in selected if clean_text(source)]
    results: list[dict[str, Any]] = []
    started = now_iso()
    configured_budget = max_remote_calls if max_remote_calls is not None else cfg.get("max_remote_calls")
    total_remote_limit = _positive_int(configured_budget)
    budget = RemoteBudget(total_remote_limit)
    source_budgets: dict[str, RemoteBudget] = {}

    def scoped_budget(source: str, default_limit: int | None) -> RemoteBudget:
        limit = _source_budget_override(cfg, source)
        if limit is None:
            limit = default_limit
        child = budget.child(source, limit)
        source_budgets[source] = child
        return child

    if "paperlists" in selected:
        venues = resolve_paperlists_venues(
            paperlists_venues, cfg.get("paperlists", {}).get("venues"), topic
        )
        paperlists_budget = scoped_budget(
            "paperlists",
            _default_source_budget_limit("paperlists", total_remote_limit, years=years, venues=venues),
        )
        results.append(sync_paperlists(
            workspace,
            topic,
            years,
            venues=venues,
            refresh=refresh,
            timeout=timeout,
            budget=paperlists_budget,
        ))

    if "openalex" in selected:
        oa_cfg = cfg.get("openalex", {})
        api_key_env = clean_text(oa_cfg.get("api_key_env", ""))
        api_key = clean_text(oa_cfg.get("api_key", ""))
        if api_key_env and not api_key:
            api_key = os.getenv(api_key_env, "")
        results.append(sync_openalex(
            workspace,
            topic,
            years,
            queries=openalex_query_specs(oa_cfg, topic),
            base_url=clean_text(oa_cfg.get("base_url", "")) or OPENALEX_API,
            api_key=api_key,
            page_size=int(oa_cfg.get("page_size", 100)),
            max_pages=int(oa_cfg.get("max_pages", 2)),
            weak_max_pages=int(oa_cfg.get("weak_max_pages", 1)),
            modes=[clean_text(mode) for mode in as_list(oa_cfg.get("modes", ["exact"])) if clean_text(mode)],
            sorts=[clean_text(sort) for sort in as_list(oa_cfg.get("sorts", ["relevance"])) if clean_text(sort)],
            topic_ids=[clean_text(item) for item in as_list(oa_cfg.get("topic_ids")) if clean_text(item)],
            refresh=refresh,
            timeout=int(oa_cfg.get("timeout", timeout)),
            sleep_seconds=float(oa_cfg.get("sleep_seconds", 0.25)),
            budget=scoped_budget(
                "openalex",
                _default_source_budget_limit("openalex", total_remote_limit, years=years),
            ),
        ))

    if "crossref" in selected:
        cr_cfg = cfg.get("crossref", {})
        queries = cr_cfg.get("queries") or default_queries(topic)
        results.append(sync_crossref(
            workspace,
            topic,
            years,
            queries=[clean_text(q) for q in queries if clean_text(q)],
            base_url=clean_text(cr_cfg.get("base_url", "")) or CROSSREF_API,
            max_results=int(cr_cfg.get("max_results", 50)),
            page_size=int(cr_cfg.get("page_size", 25)),
            max_pages=int(cr_cfg.get("max_pages", 1)),
            mailto=clean_text(cr_cfg.get("mailto") or os.getenv(clean_text(cr_cfg.get("mailto_env", "")), "")),
            refresh=refresh,
            timeout=int(cr_cfg.get("timeout", timeout)),
            sleep_seconds=float(cr_cfg.get("sleep_seconds", 0.25)),
            budget=scoped_budget(
                "crossref",
                _default_source_budget_limit("crossref", total_remote_limit, years=years),
            ),
        ))

    if "dblp" in selected:
        dblp_cfg = cfg.get("dblp", {})
        queries = dblp_cfg.get("queries") or default_queries(topic)
        results.append(sync_dblp(
            workspace,
            topic,
            years,
            queries=[clean_text(q) for q in queries if clean_text(q)],
            base_url=clean_text(dblp_cfg.get("base_url", "")) or DBLP_API,
            max_results=int(dblp_cfg.get("max_results", 50)),
            page_size=int(dblp_cfg.get("page_size", 25)),
            max_pages=int(dblp_cfg.get("max_pages", 1)),
            refresh=refresh,
            timeout=int(dblp_cfg.get("timeout", timeout)),
            sleep_seconds=float(dblp_cfg.get("sleep_seconds", 0.25)),
            budget=scoped_budget(
                "dblp",
                _default_source_budget_limit("dblp", total_remote_limit, years=years),
            ),
        ))

    if "acl_anthology" in selected:
        acl_cfg = cfg.get("acl_anthology", {})
        venues = [clean_text(v).lower() for v in as_list(acl_cfg.get("venues") or ["acl", "emnlp", "naacl", "eacl", "coling"]) if clean_text(v)]
        results.append(sync_acl_anthology(
            workspace,
            topic,
            years,
            venues=venues,
            base_url=clean_text(acl_cfg.get("base_url", "")) or ACL_ANTHOLOGY_XML,
            refresh=refresh,
            timeout=int(acl_cfg.get("timeout", timeout)),
            sleep_seconds=float(acl_cfg.get("sleep_seconds", 0.25)),
            budget=scoped_budget(
                "acl_anthology",
                _default_source_budget_limit("acl_anthology", total_remote_limit, years=years),
            ),
        ))

    if "europepmc" in selected:
        epmc_cfg = cfg.get("europepmc", {})
        queries = epmc_cfg.get("queries") or default_queries(topic)
        results.append(sync_europepmc(
            workspace,
            topic,
            years,
            queries=[clean_text(q) for q in queries if clean_text(q)],
            base_url=clean_text(epmc_cfg.get("base_url", "")) or EUROPEPMC_API,
            max_results=int(epmc_cfg.get("max_results", 50)),
            page_size=int(epmc_cfg.get("page_size", 25)),
            max_pages=int(epmc_cfg.get("max_pages", 1)),
            refresh=refresh,
            timeout=int(epmc_cfg.get("timeout", timeout)),
            sleep_seconds=float(epmc_cfg.get("sleep_seconds", 0.25)),
            budget=scoped_budget(
                "europepmc",
                _default_source_budget_limit("europepmc", total_remote_limit, years=years),
            ),
        ))

    if "pubmed" in selected:
        pubmed_cfg = cfg.get("pubmed", {})
        queries = pubmed_cfg.get("queries") or default_queries(topic)
        api_key_env = clean_text(pubmed_cfg.get("api_key_env", ""))
        api_key = clean_text(pubmed_cfg.get("api_key", ""))
        if api_key_env and not api_key:
            api_key = os.getenv(api_key_env, "")
        results.append(sync_pubmed(
            workspace,
            topic,
            years,
            queries=[clean_text(q) for q in queries if clean_text(q)],
            base_url=clean_text(pubmed_cfg.get("base_url", "")) or PUBMED_EUTILS_API,
            max_results=int(pubmed_cfg.get("max_results", 30)),
            page_size=int(pubmed_cfg.get("page_size", 20)),
            refresh=refresh,
            timeout=int(pubmed_cfg.get("timeout", timeout)),
            sleep_seconds=float(pubmed_cfg.get("sleep_seconds", 0.35)),
            api_key=api_key,
            budget=scoped_budget(
                "pubmed",
                _default_source_budget_limit("pubmed", total_remote_limit, years=years),
            ),
        ))

    if "openreview" in selected:
        or_cfg = cfg.get("openreview", {})
        results.append(sync_openreview(
            workspace,
            topic,
            years,
            invitations=[clean_text(item) for item in as_list(or_cfg.get("invitations")) if clean_text(item)],
            base_url=clean_text(or_cfg.get("base_url", "")) or OPENREVIEW_API,
            limit=int(or_cfg.get("limit", 50)),
            max_pages=int(or_cfg.get("max_pages", 1)),
            refresh=refresh,
            timeout=int(or_cfg.get("timeout", timeout)),
            sleep_seconds=float(or_cfg.get("sleep_seconds", 0.25)),
            budget=scoped_budget(
                "openreview",
                _default_source_budget_limit("openreview", total_remote_limit, years=years),
            ),
        ))

    if "semanticscholar" in selected:
        ss_cfg = cfg.get("semanticscholar", {})
        queries = ss_cfg.get("queries") or default_queries(topic)
        api_key_env = clean_text(ss_cfg.get("api_key_env", ""))
        api_key = clean_text(ss_cfg.get("api_key", ""))
        if api_key_env and not api_key:
            api_key = os.getenv(api_key_env, "")
        results.append(sync_semantic_scholar(
            workspace,
            topic,
            years,
            queries=[clean_text(q) for q in queries if clean_text(q)],
            max_results=int(ss_cfg.get("max_results", 1000)),
            page_size=int(ss_cfg.get("page_size", 100)),
            api_key=api_key,
            base_url=clean_text(ss_cfg.get("base_url", "")) or SEMANTIC_SCHOLAR_API,
            mode=clean_text(ss_cfg.get("mode", "bulk")) or "bulk",
            year_strategy=clean_text(ss_cfg.get("year_strategy", "range")) or "range",
            sorts=[clean_text(sort) for sort in as_list(ss_cfg.get("sorts", ["citationCount:desc", "publicationDate:desc"])) if clean_text(sort)],
            max_pages=int(ss_cfg.get("max_pages", 1)),
            refresh=refresh,
            timeout=int(ss_cfg.get("timeout", timeout)),
            sleep_seconds=float(ss_cfg.get("sleep_seconds", 6.0)),
            no_key_sleep_seconds=float(ss_cfg.get("no_key_sleep_seconds", 6.0)),
            retry_attempts=int(ss_cfg["retry_attempts"]) if ss_cfg.get("retry_attempts") not in (None, "") else None,
            rate_limit_error_limit=int(ss_cfg.get("rate_limit_error_limit", 1)),
            max_kept_per_run=_positive_int(ss_cfg.get("max_kept_per_run")),
            budget=scoped_budget(
                "semanticscholar",
                _default_source_budget_limit("semanticscholar", total_remote_limit, years=years),
            ),
        ))

    if "arxiv" in selected:
        arxiv_cfg = cfg.get("arxiv", {})
        queries = arxiv_cfg.get("queries") or default_arxiv_queries(topic)
        if clean_text(arxiv_cfg.get("budget_policy") or "auto_floor").lower() != "fixed":
            arxiv_cfg["queries"] = queries
            ensure_arxiv_budget_floor(arxiv_cfg)
        results.append(sync_arxiv_discovery(
            workspace,
            topic,
            years,
            queries=[clean_text(q) for q in queries if clean_text(q)],
            max_results=int(arxiv_cfg.get("max_results", 100)),
            page_size=int(arxiv_cfg.get("page_size", 100)),
            sort_by=clean_text(arxiv_cfg.get("sort_by", "relevance")) or "relevance",
            refresh=refresh,
            timeout=int(arxiv_cfg.get("timeout", timeout)),
            sleep_seconds=float(arxiv_cfg.get("sleep_seconds", 3.2)),
            rate_limit_error_limit=int(arxiv_cfg.get("rate_limit_error_limit", 3)),
            retry_attempts=int(arxiv_cfg.get("retry_attempts", 1)),
            recent_year_count=_positive_int(arxiv_cfg.get("recent_year_count")),
            budget=scoped_budget(
                "arxiv",
                _default_source_budget_limit("arxiv", total_remote_limit, years=years),
            ),
            recent_first=_config_bool(arxiv_cfg.get("recent_first", True), True),
        ))

    if "gemini_search" in selected:
        gs_cfg = cfg.get("gemini_search", {}) or {}
        if gs_cfg.get("enabled", False):
            from .sources.gemini_search import sync_gemini_search

            gs_queries = gs_cfg.get("queries") or default_queries(topic)
            results.append(sync_gemini_search(
                workspace,
                topic,
                years,
                queries=[clean_text(q) for q in gs_queries if clean_text(q)],
                direction=clean_text(topic.get("description") or topic.get("name") or ""),
                max_results_per_query=int(gs_cfg.get("max_results_per_query", 15)),
                max_queries=int(gs_cfg.get("max_queries", 6)),
                timeout=int(gs_cfg.get("timeout", 300)),
                refresh=refresh,
                budget=scoped_budget(
                    "gemini_search",
                    _default_source_budget_limit("gemini_search", total_remote_limit, years=years),
                ),
            ))
        else:
            results.append({
                "source": "gemini_search",
                "runs": 0,
                "seen": 0,
                "kept": 0,
                "errors": [{"phase": "config", "error": "discovery.gemini_search.enabled is false"}],
                "status": "skipped_disabled",
            })

    build_result: dict[str, Any] | None = None
    catalog_result: dict[str, Any] | None = None
    if build:
        build_result = build_workspace(workspace)
        if catalog:
            catalog_result = build_catalog(workspace)

    report = make_coverage_report(workspace) if build else {}
    payload = {
        "started_at": started,
        "finished_at": now_iso(),
        "workspace": workspace_label(workspace),
        "years": years,
        "sources": selected,
        "source_results": results,
        "build": build_result,
        "catalog": catalog_result,
        "coverage_report": (
            workspace_relative_path(workspace, manifests_dir(workspace) / "coverage_report.json")
            if build
            else ""
        ),
        "paper_count": report.get("paper_count"),
        "remote_calls_used": budget.used,
        "remote_calls_limit": budget.limit,
        "source_budgets": {
            source: {"used": source_budget.used, "limit": source_budget.limit}
            for source, source_budget in source_budgets.items()
        },
    }
    payload = portable_workspace_data(workspace, payload)
    write_json(manifests_dir(workspace) / f"discovery_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", payload)
    return payload

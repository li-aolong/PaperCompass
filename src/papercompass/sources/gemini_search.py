"""Optional discovery source: Gemini CLI with Google Search grounding.

Opt-in. Requires `gemini` on PATH. The user must (a) include `gemini_search` in
`sources.yaml::discovery.sources` AND (b) set `discovery.gemini_search.enabled:
true`. If either is missing, `sync_gemini_search` returns a skip stub without
side effects.

Strategy: for each user-configured query, ask the gemini CLI to use Google
Search grounding and return a structured JSON list of papers. Each paper is
post-processed (extract arxiv_id from url when gemini omits the structured ID)
and wrapped via the standard `wrap_candidate` helper, so downstream build /
classification / weak-review treats it identically to other sources.

Hallucination safeguards:
- prompt explicitly forbids fabrication and demands grounding to a real URL
- post-filter: drop entries with no usable URL or arxiv_id/doi
- `strength` defaults to "weak" → results enter the weak-review queue, where
  the brain three-way decision filters out leftover noise
- 1 grounding call counts as 1 `RemoteBudget.consume()`; budget exhaustion
  short-circuits remaining queries gracefully
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import ensure_workspace_dirs, raw_dir
from ..plugins.brain import (
    BrainInvocationError,
    BrainResponse,
    BrainUnavailable,
    GeminiPlugin,
)
from ..text import clean_text
from .registry import DiscoveryContext, SourceCapabilities, SourcePreflight, SourceQuery


_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")


class GeminiSearchSourcePlugin:
    name = "gemini_search"
    description = "Gemini Search assisted recall"
    capabilities = SourceCapabilities(
        name="gemini_search",
        requires_auth=True,
        supports_incremental=False,
        supports_cursor=False,
        supports_since=False,
        default_rate_limit_seconds=0.0,
    )

    def preflight(self, context: DiscoveryContext) -> SourcePreflight:
        enabled = bool(context.source_config.get("enabled", False))
        warnings: list[str] = []
        status = "ok" if enabled else "disabled"
        auth_state = "cli_configured"
        if enabled and not GeminiPlugin.is_available():
            status = "warning"
            auth_state = "missing_cli"
            warnings.append("gemini_cli_missing")
        return SourcePreflight(
            source=self.name,
            status=status,
            auth_state=auth_state,
            warnings=warnings,
        )

    def plan_queries(self, context: DiscoveryContext) -> list[SourceQuery]:
        return []

    def fetch(self, query: SourceQuery, context: DiscoveryContext) -> list[dict[str, Any]]:
        raise NotImplementedError("gemini_search has not migrated fetch() yet")

    def normalize(self, item: dict[str, Any], query: SourceQuery, context: DiscoveryContext) -> dict[str, Any]:
        normalized = _normalize_paper_row(item)
        return normalized or {}

    def run(self, context: DiscoveryContext) -> dict[str, Any]:
        from ..discovery import RemoteBudget, default_queries

        cfg = context.source_config or {}
        if not cfg.get("enabled", False):
            return {
                "source": self.name,
                "runs": 0,
                "seen": 0,
                "kept": 0,
                "errors": [{"phase": "config", "error": "discovery.gemini_search.enabled is false"}],
                "status": "skipped_disabled",
            }
        queries = cfg.get("queries") or default_queries(context.topic)
        year_window = (context.years[0], context.years[-1]) if context.years else None
        return sync_gemini_search(
            context.workspace,
            context.topic,
            year_window,
            queries=[clean_text(q) for q in queries if clean_text(q)],
            direction=clean_text(context.topic.get("description") or context.topic.get("name") or ""),
            max_results_per_query=int(cfg.get("max_results_per_query", 15)),
            max_queries=int(cfg.get("max_queries", 6)),
            timeout=int(cfg.get("timeout", 300)),
            refresh=context.refresh,
            budget=context.budget or RemoteBudget(None),
        )


def _papers_schema(min_year: int | None) -> dict[str, Any]:
    """JSON schema for the gemini grounding response.

    All fields are strings (empty = unknown) to stay compatible with OpenAI
    strict mode which cannot mix `null` with primitive types via plain
    `type:[..., "null"]`. The brain plugin's `_strict_schema` will inject
    `additionalProperties:false` and full `required` lists.
    """
    return {
        "type": "object",
        "properties": {
            "papers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "year": {"type": "integer"},
                        "authors": {"type": "string"},
                        "arxiv_id": {"type": "string"},
                        "doi": {"type": "string"},
                        "url": {"type": "string"},
                        "venue": {"type": "string"},
                        "abstract": {"type": "string"},
                        "source_url": {"type": "string"},
                    },
                },
            }
        },
    }


_PROMPT_TEMPLATE = """Use Google Search grounding to find recent academic papers about a research topic.

# Research direction
{direction}

# Search query (run with grounding)
{query}

# Constraints
- Year range: {year_constraint}
- Return up to {max_results} papers
- Prefer arxiv.org / aclanthology.org / openreview.net / acm.org / ieeexplore.ieee.org / nature.com / sciencedirect.com
- For each paper, ground the entry to a real URL via your grounding tool. If you cannot ground a paper to a real source, omit it.

# Per-paper fields (string, "" if unknown)
- title (required)
- year (integer)
- authors (semicolon-separated)
- arxiv_id (e.g. "2305.12345"; "" if not arXiv)
- doi (e.g. "10.18653/v1/2024.acl-long.123"; "" if unknown)
- url (the landing page; required)
- venue (conference/journal name)
- abstract (≤300 words; one-line summary if you cannot find a real abstract)
- source_url (the URL you actually grounded to; same as url for direct hits)

# Hard rules
- DO NOT fabricate. If you cannot ground, omit.
- DO NOT include papers outside the year range.
- DO NOT include reviews of the topic itself ("survey of X"). Include actual technical contributions.

Return a single JSON object: {{"papers": [...]}}."""


def _build_prompt(
    direction: str,
    query: str,
    *,
    years: tuple[int, int] | None,
    max_results: int,
) -> str:
    if years and years[0] and years[1]:
        year_constraint = f"{years[0]}-{years[1]}"
    elif years and years[0]:
        year_constraint = f"≥ {years[0]}"
    else:
        year_constraint = "(unspecified)"
    return _PROMPT_TEMPLATE.format(
        direction=clean_text(direction) or "(unspecified)",
        query=clean_text(query),
        year_constraint=year_constraint,
        max_results=max_results,
    )


def _normalize_paper_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce one entry from the gemini response into the raw format used by
    `wrap_candidate`. Drops rows without title or any usable locator."""
    title = clean_text(row.get("title"))
    if not title:
        return None
    arxiv_id = clean_text(row.get("arxiv_id"))
    url = clean_text(row.get("url"))
    if not arxiv_id and url:
        match = _ARXIV_ID_RE.search(url)
        if match:
            arxiv_id = match.group(1)
    doi = clean_text(row.get("doi"))
    if not (arxiv_id or doi or url):
        return None
    return {
        "title": title,
        "year": row.get("year"),
        "authors": clean_text(row.get("authors")),
        "arxiv_id": arxiv_id,
        "doi": doi,
        "url": url,
        "venue": clean_text(row.get("venue")),
        "abstract": clean_text(row.get("abstract")),
        "source_url": clean_text(row.get("source_url")) or url,
    }


def _filter_by_year(rows: list[dict[str, Any]], years: tuple[int, int] | None) -> list[dict[str, Any]]:
    if not years:
        return rows
    lo, hi = years
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            year = int(row.get("year") or 0)
        except (TypeError, ValueError):
            year = 0
        if year and lo and year < lo:
            continue
        if year and hi and year > hi:
            continue
        out.append(row)
    return out


def sync_gemini_search(
    workspace: Path,
    topic: dict[str, Any],
    years: tuple[int, int] | None,
    *,
    queries: list[str],
    direction: str = "",
    max_results_per_query: int = 15,
    max_queries: int = 6,
    timeout: int = 300,
    refresh: bool = False,
    budget: Any,
    plugin: GeminiPlugin | None = None,
) -> dict[str, Any]:
    """Run the gemini-grounded discovery for the configured queries.

    Returns the same shape used by other discovery sources:
        {"source": "gemini_search", "runs": int, "seen": int, "kept": int,
         "errors": list[dict], "status": str}

    `refresh` is accepted for API parity but currently ignored — gemini
    grounding is non-cacheable since the same query may return different
    papers across runs. Each call is one `budget.consume()` regardless of how
    many papers come back.
    """
    ensure_workspace_dirs(workspace)
    direction = clean_text(direction) or clean_text(topic.get("description")) or clean_text(topic.get("name")) or ""
    queries = [clean_text(q) for q in queries if clean_text(q)]
    queries = queries[: max(0, int(max_queries or 0))] if max_queries else queries
    result_skeleton: dict[str, Any] = {
        "source": "gemini_search",
        "runs": 0,
        "seen": 0,
        "kept": 0,
        "errors": [],
        "status": "skipped",
    }
    if not queries:
        result_skeleton["status"] = "skipped_no_queries"
        return result_skeleton
    try:
        brain = plugin or GeminiPlugin()
        if not brain.is_available():
            raise BrainUnavailable("gemini CLI not on PATH")
    except BrainUnavailable as exc:
        result_skeleton["status"] = "skipped_no_cli"
        result_skeleton["errors"].append({"phase": "init", "error": str(exc)})
        return result_skeleton

    # local imports to avoid a top-level cycle with the parent discovery module
    from ..discovery import (
        append_jsonl,
        coverage_health,
        load_coverage,
        now_iso,
        record_provenance,
        record_source_run,
        run_id as make_run_id,
        save_coverage,
        wrap_candidate,
    )

    run_id = make_run_id("gemini_search")
    raw_path = raw_dir(workspace) / "gemini_search" / f"{run_id}_gemini_search.jsonl"
    coverage = load_coverage(workspace)
    runs = 0
    total_seen = 0
    total_kept = 0
    errors: list[dict[str, Any]] = []
    schema = _papers_schema(min_year=(years or (None, None))[0])

    for query_idx, query in enumerate(queries):
        started = now_iso()
        cache_key = f"gemini_search/{query}"
        try:
            budget.consume("gemini_search", query)
        except RuntimeError as exc:
            errors.append({"query": query, "phase": "budget", "error": str(exc)})
            coverage[cache_key] = {
                "source": "gemini_search",
                "query": query,
                "complete": False,
                "status": "failed",
                **coverage_health("failed", budget_complete=False),
                "result_count": 0,
                "kept_count": 0,
                "raw_output": str(raw_path.relative_to(workspace)),
                "fetched_at": now_iso(),
                "errors": [str(exc)],
            }
            save_coverage(workspace, coverage)
            break

        prompt = _build_prompt(
            direction=direction,
            query=query,
            years=years,
            max_results=int(max_results_per_query),
        )
        status = "success"
        error_message = ""
        resp: BrainResponse | None = None
        try:
            resp = brain.ask(prompt, schema=schema, timeout=timeout, retries=1)
        except BrainInvocationError as exc:
            status = "failed"
            error_message = str(exc)[:500]

        rows: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        seen_count = 0
        if resp is not None and isinstance(resp.parsed, dict):
            papers = resp.parsed.get("papers")
            if isinstance(papers, list):
                normalized = [r for r in (_normalize_paper_row(p) for p in papers if isinstance(p, dict)) if r]
                seen_count = len(papers)
                normalized = _filter_by_year(normalized, years)
                for raw in normalized:
                    wrapper, prov = wrap_candidate(
                        raw,
                        source_name="gemini_search",
                        source_type="gemini_search",
                        query=query,
                        source_run_id=run_id,
                        raw_path=raw_path,
                        topic=topic,
                        source_item_id=raw.get("arxiv_id") or raw.get("doi") or "",
                        source_url=raw.get("source_url") or raw.get("url") or "",
                    )
                    rows.append(wrapper)
                    provenance.append(prov)
            else:
                status = "failed"
                error_message = "gemini response missing 'papers' array"
        elif resp is not None:
            status = "failed"
            error_message = "gemini response not parseable as JSON"

        if rows:
            append_jsonl(raw_path, rows)
            record_provenance(workspace, provenance)
        total_seen += seen_count
        total_kept += len(rows)
        runs += 1
        coverage[cache_key] = {
            "source": "gemini_search",
            "query": query,
            "complete": status == "success",
            "status": status,
            **coverage_health(status, budget_complete=status == "success"),
            "result_count": seen_count,
            "kept_count": len(rows),
            "raw_output": str(raw_path.relative_to(workspace)),
            "expected_total": seen_count,
            "next_offset": None,
            "target_max": int(max_results_per_query),
            "last_run_id": run_id,
            "fetched_at": now_iso(),
            "errors": [error_message] if error_message else [],
        }
        record_source_run(workspace, {
            "run_id": run_id,
            "source": "gemini_search",
            "operation": "fetch",
            "query": query,
            "params": {
                "max_results": int(max_results_per_query),
                "year_lo": (years or (None, None))[0],
                "year_hi": (years or (None, None))[1],
            },
            "started_at": started,
            "finished_at": now_iso(),
            "status": status,
            "fetched_count": seen_count,
            "kept_count": len(rows),
            "raw_output": str(raw_path.relative_to(workspace)),
            "written_count": len(rows),
            "errors": [error_message] if error_message else [],
        })
        if error_message:
            errors.append({"query": query, "phase": "ask", "error": error_message})
        save_coverage(workspace, coverage)

    return {
        "source": "gemini_search",
        "runs": runs,
        "seen": total_seen,
        "kept": total_kept,
        "errors": errors[:20],
        "status": "completed" if runs else "skipped",
    }

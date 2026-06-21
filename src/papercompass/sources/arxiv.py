from __future__ import annotations

import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

from ..build import write_run_log
from ..config import (
    ensure_workspace_dirs,
    load_sources_config,
    load_topic_config,
    raw_dir,
    workspace_relative_path,
)
from ..text import clean_text, parse_year, write_jsonl
from .registry import DiscoveryContext, SourceCapabilities, SourcePreflight, SourceQuery


USER_AGENT = "papercompass/0.1"


class ArxivSourcePlugin:
    name = "arxiv"
    description = "arXiv preprint metadata"
    capabilities = SourceCapabilities(
        name="arxiv",
        requires_auth=False,
        supports_incremental=True,
        supports_cursor=False,
        supports_since=True,
        default_rate_limit_seconds=3.2,
    )

    def preflight(self, context: DiscoveryContext) -> SourcePreflight:
        sleep_seconds = float(context.source_config.get("sleep_seconds", self.capabilities.default_rate_limit_seconds))
        return SourcePreflight(
            source=self.name,
            status="ok",
            auth_state="not_required",
            effective_rate_limit_seconds=sleep_seconds,
        )

    def plan_queries(self, context: DiscoveryContext) -> list[SourceQuery]:
        from ..discovery import (
            _config_bool,
            _positive_int,
            arxiv_year_query,
            cache_manifest_key,
            default_arxiv_queries,
        )
        from ..source_budget import ensure_arxiv_budget_floor

        cfg = dict(context.source_config)
        queries = cfg.get("queries") or default_arxiv_queries(context.topic)
        queries = [clean_text(query) for query in queries if clean_text(query)]
        if clean_text(cfg.get("budget_policy") or "auto_floor").lower() != "fixed":
            cfg["queries"] = queries
            ensure_arxiv_budget_floor(cfg)
        max_results = int(cfg.get("max_results", 100))
        page_size = max(1, int(cfg.get("page_size", 100)))
        sort_by = clean_text(cfg.get("sort_by", "relevance")) or "relevance"
        recent_first = _config_bool(cfg.get("recent_first", True), True)
        active_years = sorted(context.years, reverse=True) if recent_first else list(context.years)
        recent_year_count = _positive_int(cfg.get("recent_year_count"))
        if recent_year_count:
            active_years = active_years[:recent_year_count]

        planned: list[SourceQuery] = []
        for query in queries:
            for year in active_years:
                year_query = arxiv_year_query(query, year)
                for offset in range(0, max_results, page_size):
                    limit = min(page_size, max_results - offset)
                    planned.append(SourceQuery(
                        source=self.name,
                        query_key=cache_manifest_key("arxiv", year_query, sort_by, offset),
                        query=year_query,
                        since=f"{year}-01-01",
                        params={
                            "base_query": query,
                            "year": year,
                            "offset": offset,
                            "limit": limit,
                            "sort_by": sort_by,
                            "timeout": int(cfg.get("timeout", context.timeout)),
                            "retry_attempts": int(cfg.get("retry_attempts", 1)),
                            "sleep_seconds": float(cfg.get("sleep_seconds", 3.2)),
                        },
                    ))
        return planned

    def fetch(self, query: SourceQuery, context: DiscoveryContext) -> list[dict[str, Any]]:
        from ..discovery import arxiv_search

        payload = arxiv_search(
            query.query,
            start=int(query.params.get("offset", 0)),
            max_results=int(query.params.get("limit", 100)),
            sort_by=clean_text(query.params.get("sort_by", "relevance")) or "relevance",
            timeout=int(query.params.get("timeout", context.timeout)),
            retry_attempts=int(query.params.get("retry_attempts", 1)),
            budget=context.budget,
        )
        data = payload.get("data") if isinstance(payload, dict) else []
        return [item for item in data if isinstance(item, dict)]

    def normalize(self, item: dict[str, Any], query: SourceQuery, context: DiscoveryContext) -> dict[str, Any]:
        raw = dict(item)
        raw["title"] = clean_text(raw.get("title"))
        raw["abstract"] = clean_text(raw.get("abstract"))
        raw["venue"] = clean_text(raw.get("venue")) or "arXiv"
        raw["arxiv_id"] = clean_text(raw.get("arxiv_id"))
        if raw.get("published") and not raw.get("year"):
            raw["year"] = parse_year(raw.get("published"))
        if raw.get("arxiv_id") and not raw.get("url"):
            raw["url"] = f"https://arxiv.org/abs/{raw['arxiv_id']}"
        if raw.get("arxiv_id") and not raw.get("pdf_url"):
            raw["pdf_url"] = f"https://arxiv.org/pdf/{raw['arxiv_id']}.pdf"
        return {key: value for key, value in raw.items() if value not in (None, "", [], {})}

    def run(self, context: DiscoveryContext) -> dict[str, Any]:
        from ..discovery import (
            _config_bool,
            _positive_int,
            default_arxiv_queries,
            sync_arxiv_discovery,
        )
        from ..source_budget import ensure_arxiv_budget_floor

        cfg = dict(context.source_config)
        queries = cfg.get("queries") or default_arxiv_queries(context.topic)
        if clean_text(cfg.get("budget_policy") or "auto_floor").lower() != "fixed":
            cfg["queries"] = queries
            ensure_arxiv_budget_floor(cfg)
        return sync_arxiv_discovery(
            context.workspace,
            context.topic,
            context.years,
            queries=[clean_text(q) for q in queries if clean_text(q)],
            max_results=int(cfg.get("max_results", 100)),
            page_size=int(cfg.get("page_size", 100)),
            sort_by=clean_text(cfg.get("sort_by", "relevance")) or "relevance",
            refresh=context.refresh,
            timeout=int(cfg.get("timeout", context.timeout)),
            sleep_seconds=float(cfg.get("sleep_seconds", 3.2)),
            rate_limit_error_limit=int(cfg.get("rate_limit_error_limit", 3)),
            retry_attempts=int(cfg.get("retry_attempts", 1)),
            recent_year_count=_positive_int(cfg.get("recent_year_count")),
            budget=context.budget,
            recent_first=_config_bool(cfg.get("recent_first", True), True),
        )


def arxiv_search(query: str, max_results: int = 25, sort_by: str = "relevance", timeout: int = 35) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": sort_by,
        "sortOrder": "descending",
    })
    url = f"https://export.arxiv.org/api/query?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()

    root = ET.fromstring(payload)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
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
    return items


def sync_arxiv(workspace: Path, source_name: str = "arxiv") -> dict[str, Any]:
    ensure_workspace_dirs(workspace)
    topic = load_topic_config(workspace)
    sources = load_sources_config(workspace).get("sources", {})
    cfg = sources.get(source_name) or sources.get("arxiv") or {}
    queries = cfg.get("queries") or topic.get("arxiv_queries") or []
    if not queries:
        queries = [f'all:"{kw}"' for kw in topic.get("keywords", [])[:8]]
    if not queries:
        raise ValueError("arXiv source 没有 queries；请在 sources.yaml 或 topic.yaml 中配置")

    max_results = int(cfg.get("max_results", 25))
    sort_by_values = cfg.get("sort_by") or ["relevance", "submittedDate"]
    if isinstance(sort_by_values, str):
        sort_by_values = [sort_by_values]
    sleep_seconds = float(cfg.get("sleep_seconds", 0.35))
    timeout = int(cfg.get("timeout", 35))

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    fetched_at = datetime.now().isoformat(timespec="seconds")
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for sort_by in sort_by_values:
        for query in queries:
            try:
                results = arxiv_search(query, max_results=max_results, sort_by=sort_by, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                errors.append({"query": query, "sort_by": sort_by, "error": str(exc)})
                continue
            for item in results:
                rows.append({
                    "source_name": source_name,
                    "source_type": "arxiv",
                    "query": query,
                    "sort_by": sort_by,
                    "fetched_at": fetched_at,
                    "raw": item,
                })
            time.sleep(sleep_seconds)

    out_path = raw_dir(workspace) / "arxiv" / f"{run_id}_{source_name}.jsonl"
    count = write_jsonl(out_path, rows)
    payload = {
        "run_id": run_id,
        "source": source_name,
        "output": workspace_relative_path(workspace, out_path),
        "query_count": len(queries) * len(sort_by_values),
        "candidate_count": count,
        "error_count": len(errors),
    }
    if errors:
        payload["errors"] = errors[:10]
    log = write_run_log(workspace, "sync_arxiv", payload)
    return {**payload, "log": workspace_relative_path(workspace, log)}

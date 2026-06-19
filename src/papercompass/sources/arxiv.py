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


USER_AGENT = "papercompass/0.1"


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

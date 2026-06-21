from __future__ import annotations

import time
from typing import Any

from ..text import as_list, clean_text, slugify
from .registry import DiscoveryContext, SourceCapabilities, SourcePreflight, SourceQuery


class SemanticScholarSourcePlugin:
    name = "semanticscholar"
    description = "Semantic Scholar metadata"
    capabilities = SourceCapabilities(
        name="semanticscholar",
        requires_auth=False,
        supports_incremental=True,
        supports_cursor=True,
        supports_since=False,
        default_rate_limit_seconds=6.0,
    )

    def preflight(self, context: DiscoveryContext) -> SourcePreflight:
        from ..discovery import SEMANTIC_SCHOLAR_API, resolve_secret, semantic_scholar_auth_status

        cfg = context.source_config
        base_url = clean_text(cfg.get("base_url", "")) or SEMANTIC_SCHOLAR_API
        api_key = resolve_secret(
            cfg,
            value_key="api_key",
            env_key="api_key_env",
            default_env="SEMANTIC_SCHOLAR_API_KEY",
        )
        auth_state = semantic_scholar_auth_status(api_key, base_url)
        warnings: list[str] = []
        if auth_state == "anonymous":
            warnings.append("semanticscholar_anonymous_optional_source")
        return SourcePreflight(
            source=self.name,
            status="ok",
            auth_state=auth_state,
            warnings=warnings,
            effective_rate_limit_seconds=float(cfg.get("sleep_seconds", self.capabilities.default_rate_limit_seconds)),
        )

    def plan_queries(self, context: DiscoveryContext) -> list[SourceQuery]:
        from ..discovery import (
            cache_manifest_key,
            default_queries,
            semantic_year_buckets,
        )

        cfg = context.source_config
        queries = cfg.get("queries") or default_queries(context.topic)
        mode = clean_text(cfg.get("mode", "bulk")).lower() or "bulk"
        year_strategy = clean_text(cfg.get("year_strategy", "range")) or "range"
        sorts = [
            clean_text(sort)
            for sort in as_list(cfg.get("sorts", ["citationCount:desc", "publicationDate:desc"]))
            if clean_text(sort)
        ]
        if mode == "relevance":
            sorts = [""]
        planned: list[SourceQuery] = []
        for query in [clean_text(q) for q in queries if clean_text(q)]:
            for year_bucket in semantic_year_buckets(context.years, year_strategy):
                for sort in sorts:
                    sort_slug = slugify(sort or "relevance", 60)
                    planned.append(SourceQuery(
                        source=self.name,
                        query_key=cache_manifest_key("semanticscholar", mode, query, year_bucket, sort_slug),
                        query=query,
                        params={
                            "mode": mode,
                            "year_bucket": year_bucket,
                            "sort": sort,
                            "base_query": query,
                            "max_results": int(cfg.get("max_results", 1000)),
                            "page_size": int(cfg.get("page_size", 100)),
                            "max_pages": int(cfg.get("max_pages", 1)),
                            "base_url": clean_text(cfg.get("base_url", "")),
                            "timeout": int(cfg.get("timeout", context.timeout)),
                            "sleep_seconds": float(cfg.get("sleep_seconds", 6.0)),
                            "no_key_sleep_seconds": float(cfg.get("no_key_sleep_seconds", 6.0)),
                            "retry_attempts": cfg.get("retry_attempts"),
                        },
                    ))
        return planned

    def fetch(self, query: SourceQuery, context: DiscoveryContext) -> list[dict[str, Any]]:
        from ..discovery import (
            SEMANTIC_SCHOLAR_API,
            http_get_json,
            resolve_secret,
            semantic_scholar_url,
        )

        cfg = context.source_config
        params = dict(query.params)
        api_key = resolve_secret(
            cfg,
            value_key="api_key",
            env_key="api_key_env",
            default_env="SEMANTIC_SCHOLAR_API_KEY",
        )
        base_url = clean_text(params.get("base_url")) or SEMANTIC_SCHOLAR_API
        mode = clean_text(params.get("mode", "bulk")).lower() or "bulk"
        if mode not in {"bulk", "relevance"}:
            raise ValueError(f"不支持的 Semantic Scholar mode：{mode}")
        fields = "paperId,title,abstract,url,year,citationCount,referenceCount,publicationVenue,venue,authors,externalIds,publicationDate,openAccessPdf"
        headers = {"x-api-key": api_key} if api_key and "semanticscholar.org" in base_url else {}
        if api_key and "semanticscholar.org" not in base_url:
            headers = {"Authorization": f"Bearer {api_key}"}
        sleep_seconds = float(params.get("sleep_seconds", 6.0))
        no_key_sleep_seconds = float(params.get("no_key_sleep_seconds", 6.0))
        effective_sleep = sleep_seconds if api_key else max(sleep_seconds, no_key_sleep_seconds)
        retry_attempts = params.get("retry_attempts")
        attempts = int(retry_attempts) if retry_attempts not in (None, "") else (2 if api_key else 1)
        retry_status = {429, 500, 502, 503, 504} if not api_key else {403, 429, 500, 502, 503, 504}
        max_results = max(1, int(params.get("max_results", 1000)))
        page_size = min(max(1, int(params.get("page_size", 100))), 100)
        max_pages = max(1, int(params.get("max_pages", 1)))
        year_bucket = clean_text(params.get("year_bucket"))
        sort = clean_text(params.get("sort"))
        items: list[dict[str, Any]] = []
        next_token = clean_text(query.cursor)
        offset = 0
        page_index = 0
        while page_index < max_pages and len(items) < max_results:
            limit = min(page_size, max_results - len(items))
            url = semantic_scholar_url(
                base_url,
                query.query,
                year_bucket,
                fields,
                mode,
                sort=sort,
                token=next_token,
                limit=limit,
                offset=offset,
            )
            payload = http_get_json(
                url,
                timeout=int(params.get("timeout", context.timeout)),
                headers=headers,
                attempts=attempts,
                retry_status=retry_status,
                backoff=max(6.0, effective_sleep * 2),
                budget=context.budget,
                source=self.name,
            )
            if not isinstance(payload, dict):
                break
            page_items = payload.get("data") if isinstance(payload.get("data"), list) else []
            remaining = max_results - len(items)
            items.extend(item for item in page_items[:remaining] if isinstance(item, dict))
            response_token = clean_text(payload.get("token"))
            next_offset = payload.get("next")
            source_exhausted = (
                (mode == "bulk" and not response_token)
                or (mode == "relevance" and (not next_offset or len(page_items) < limit))
            )
            if source_exhausted or len(items) >= max_results:
                break
            if mode == "bulk":
                next_token = response_token
            else:
                offset = int(next_offset or (offset + limit))
            page_index += 1
            time.sleep(effective_sleep)
        return items

    def normalize(self, item: dict[str, Any], query: SourceQuery, context: DiscoveryContext) -> dict[str, Any]:
        from ..discovery import ss_item_to_raw

        return ss_item_to_raw(item)

    def run(self, context: DiscoveryContext) -> dict[str, Any]:
        from ..discovery import (
            SEMANTIC_SCHOLAR_API,
            _positive_int,
            as_list,
            default_queries,
            resolve_secret,
            sync_semantic_scholar,
        )

        cfg = context.source_config
        queries = cfg.get("queries") or default_queries(context.topic)
        api_key = resolve_secret(
            cfg,
            value_key="api_key",
            env_key="api_key_env",
            default_env="SEMANTIC_SCHOLAR_API_KEY",
        )
        retry_attempts = int(cfg["retry_attempts"]) if cfg.get("retry_attempts") not in (None, "") else None
        return sync_semantic_scholar(
            context.workspace,
            context.topic,
            context.years,
            queries=[clean_text(q) for q in queries if clean_text(q)],
            max_results=int(cfg.get("max_results", 1000)),
            page_size=int(cfg.get("page_size", 100)),
            api_key=api_key,
            base_url=clean_text(cfg.get("base_url", "")) or SEMANTIC_SCHOLAR_API,
            mode=clean_text(cfg.get("mode", "bulk")) or "bulk",
            year_strategy=clean_text(cfg.get("year_strategy", "range")) or "range",
            sorts=[
                clean_text(sort)
                for sort in as_list(cfg.get("sorts", ["citationCount:desc", "publicationDate:desc"]))
                if clean_text(sort)
            ],
            max_pages=int(cfg.get("max_pages", 1)),
            refresh=context.refresh,
            timeout=int(cfg.get("timeout", context.timeout)),
            sleep_seconds=float(cfg.get("sleep_seconds", 6.0)),
            no_key_sleep_seconds=float(cfg.get("no_key_sleep_seconds", 6.0)),
            retry_attempts=retry_attempts,
            rate_limit_error_limit=int(cfg.get("rate_limit_error_limit", 1)),
            max_kept_per_run=_positive_int(cfg.get("max_kept_per_run")),
            budget=context.budget,
        )

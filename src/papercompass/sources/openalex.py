from __future__ import annotations

import time
from typing import Any

from ..text import as_list, clean_text
from .registry import DiscoveryContext, SourceCapabilities, SourcePreflight, SourceQuery


class OpenAlexSourcePlugin:
    name = "openalex"
    description = "OpenAlex metadata and recall"
    capabilities = SourceCapabilities(
        name="openalex",
        requires_auth=False,
        supports_incremental=True,
        supports_cursor=True,
        supports_since=False,
        default_rate_limit_seconds=0.25,
    )

    def preflight(self, context: DiscoveryContext) -> SourcePreflight:
        from ..discovery import resolve_secret

        cfg = context.source_config
        api_key = resolve_secret(
            cfg,
            value_key="api_key",
            env_key="api_key_env",
            default_env="OPENALEX_API_KEY",
        )
        mailto = resolve_secret(
            cfg,
            value_key="mailto",
            env_key="mailto_env",
            default_env="OPENALEX_EMAIL",
        )
        warnings: list[str] = []
        auth_state = "api_key" if api_key else ("email" if mailto else "anonymous")
        if auth_state == "anonymous":
            warnings.append("openalex_anonymous_access")
        return SourcePreflight(
            source=self.name,
            status="ok",
            auth_state=auth_state,
            warnings=warnings,
            effective_rate_limit_seconds=float(cfg.get("sleep_seconds", self.capabilities.default_rate_limit_seconds)),
        )

    def plan_queries(self, context: DiscoveryContext) -> list[SourceQuery]:
        from ..discovery import (
            OPENALEX_API,
            cache_manifest_key,
            openalex_query_specs,
            openalex_select_fields,
        )

        cfg = context.source_config
        year_bucket = f"{context.years[0]}-{context.years[-1]}" if len(context.years) > 1 else str(context.years[0])
        default_modes = [clean_text(mode) for mode in as_list(cfg.get("modes", ["exact"])) if clean_text(mode)]
        default_sorts = [clean_text(sort) for sort in as_list(cfg.get("sorts", ["relevance"])) if clean_text(sort)]
        default_topic_ids = [clean_text(item) for item in as_list(cfg.get("topic_ids")) if clean_text(item)]
        base_url = clean_text(cfg.get("base_url", "")) or OPENALEX_API
        page_size = int(cfg.get("page_size", 100))
        max_pages = int(cfg.get("max_pages", 2))
        weak_max_pages = int(cfg.get("weak_max_pages", 1))
        planned: list[SourceQuery] = []

        for spec in openalex_query_specs(cfg, context.topic):
            query = clean_text(spec.get("text"))
            if not query:
                continue
            strength = clean_text(spec.get("strength", "strong")).lower() or "strong"
            spec_modes = [
                clean_text(mode)
                for mode in as_list(spec.get("modes") or spec.get("mode") or default_modes)
                if clean_text(mode)
            ]
            spec_sorts = [
                clean_text(sort)
                for sort in as_list(spec.get("sorts") or spec.get("sort") or default_sorts)
                if clean_text(sort)
            ]
            spec_topic_ids = [
                clean_text(item)
                for item in as_list(spec.get("topic_ids") or default_topic_ids)
                if clean_text(item)
            ]
            topic_filter_values = spec_topic_ids or [""]
            pages = int(spec.get("max_pages") or (weak_max_pages if strength == "weak" else max_pages))
            spec_page_size = int(spec.get("page_size") or page_size)
            for mode in spec_modes:
                for sort in spec_sorts:
                    for topic_filter in topic_filter_values:
                        planned.append(SourceQuery(
                            source=self.name,
                            query_key=cache_manifest_key(
                                "openalex",
                                mode,
                                query,
                                year_bucket,
                                sort or "relevance",
                                topic_filter or "no-topic",
                            ),
                            query=query,
                            cursor="*",
                            params={
                                "base_url": base_url,
                                "year_bucket": year_bucket,
                                "mode": mode,
                                "sort": sort,
                                "topic_filter": topic_filter,
                                "strength": strength,
                                "paper_role": clean_text(spec.get("paper_role")),
                                "max_pages": pages,
                                "page_size": spec_page_size,
                                "select": clean_text(spec.get("select")) or openalex_select_fields(),
                                "timeout": int(cfg.get("timeout", context.timeout)),
                                "sleep_seconds": float(cfg.get("sleep_seconds", 0.25)),
                            },
                        ))
        return planned

    def fetch(self, query: SourceQuery, context: DiscoveryContext) -> list[dict[str, Any]]:
        from ..discovery import http_get_json, openalex_url, resolve_secret

        params = dict(query.params)
        api_key = resolve_secret(
            context.source_config,
            value_key="api_key",
            env_key="api_key_env",
            default_env="OPENALEX_API_KEY",
        )
        mailto = resolve_secret(
            context.source_config,
            value_key="mailto",
            env_key="mailto_env",
            default_env="OPENALEX_EMAIL",
        )
        cursor = clean_text(query.cursor or params.get("cursor") or "*") or "*"
        items: list[dict[str, Any]] = []
        max_pages = max(1, int(params.get("max_pages", 1)))
        page_size = min(max(1, int(params.get("page_size", 100))), 100)
        base_url = clean_text(params.get("base_url"))
        for page in range(max_pages):
            filters = [f"publication_year:{params.get('year_bucket')}", "type:article"]
            topic_filter = clean_text(params.get("topic_filter"))
            if topic_filter:
                filters.append(f"primary_topic.id:{topic_filter}")
            request_params: dict[str, Any] = {
                "filter": ",".join(filters),
                "per-page": page_size,
                "cursor": cursor,
                "select": clean_text(params.get("select")),
            }
            mode = clean_text(params.get("mode"))
            if mode == "semantic":
                request_params["search.semantic"] = query.query
            elif mode == "title":
                request_params["filter"] = f"{request_params['filter']},title.search:{query.query}"
            elif mode == "search":
                request_params["search"] = query.query
            else:
                request_params["search.exact"] = query.query
            sort = clean_text(params.get("sort"))
            if sort and sort != "relevance":
                request_params["sort"] = sort
            if api_key:
                request_params["api_key"] = api_key
            if mailto:
                request_params["mailto"] = mailto
            data = http_get_json(
                openalex_url(base_url, request_params),
                timeout=int(params.get("timeout", context.timeout)),
                attempts=2,
                retry_status={429, 500, 502, 503, 504},
                backoff=3.0,
                budget=context.budget,
                source=self.name,
            )
            if not isinstance(data, dict):
                break
            page_items = data.get("results") if isinstance(data.get("results"), list) else []
            items.extend(item for item in page_items if isinstance(item, dict))
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            cursor = clean_text(meta.get("next_cursor"))
            if not cursor or not page_items:
                break
            if page < max_pages - 1:
                time.sleep(float(params.get("sleep_seconds", 0.25)))
        return items

    def normalize(self, item: dict[str, Any], query: SourceQuery, context: DiscoveryContext) -> dict[str, Any]:
        from ..discovery import openalex_item_to_raw

        raw = openalex_item_to_raw(item)
        if clean_text(query.params.get("paper_role")):
            raw["paper_role"] = clean_text(query.params.get("paper_role"))
        return raw

    def run(self, context: DiscoveryContext) -> dict[str, Any]:
        from ..discovery import (
            OPENALEX_API,
            as_list,
            openalex_query_specs,
            resolve_secret,
            sync_openalex,
        )

        cfg = context.source_config
        api_key = resolve_secret(
            cfg,
            value_key="api_key",
            env_key="api_key_env",
            default_env="OPENALEX_API_KEY",
        )
        mailto = resolve_secret(
            cfg,
            value_key="mailto",
            env_key="mailto_env",
            default_env="OPENALEX_EMAIL",
        )
        return sync_openalex(
            context.workspace,
            context.topic,
            context.years,
            queries=openalex_query_specs(cfg, context.topic),
            base_url=clean_text(cfg.get("base_url", "")) or OPENALEX_API,
            api_key=api_key,
            mailto=mailto,
            page_size=int(cfg.get("page_size", 100)),
            max_pages=int(cfg.get("max_pages", 2)),
            weak_max_pages=int(cfg.get("weak_max_pages", 1)),
            modes=[clean_text(mode) for mode in as_list(cfg.get("modes", ["exact"])) if clean_text(mode)],
            sorts=[clean_text(sort) for sort in as_list(cfg.get("sorts", ["relevance"])) if clean_text(sort)],
            topic_ids=[clean_text(item) for item in as_list(cfg.get("topic_ids")) if clean_text(item)],
            refresh=context.refresh,
            timeout=int(cfg.get("timeout", context.timeout)),
            sleep_seconds=float(cfg.get("sleep_seconds", 0.25)),
            budget=context.budget,
        )

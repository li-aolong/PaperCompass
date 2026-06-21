from __future__ import annotations

from typing import Any

from ..text import clean_text
from .registry import DiscoveryContext, SourceCapabilities, SourcePreflight, SourceQuery


class _SimpleSourcePlugin:
    name = ""
    description = ""
    default_rate_limit_seconds = 0.25
    supports_incremental = True

    @property
    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            name=self.name,
            requires_auth=False,
            supports_incremental=self.supports_incremental,
            default_rate_limit_seconds=self.default_rate_limit_seconds,
        )

    def preflight(self, context: DiscoveryContext) -> SourcePreflight:
        return SourcePreflight(
            source=self.name,
            status="ok",
            auth_state="not_required",
            effective_rate_limit_seconds=float(context.source_config.get("sleep_seconds", self.default_rate_limit_seconds)),
        )

    def plan_queries(self, context: DiscoveryContext) -> list[SourceQuery]:
        return []

    def fetch(self, query: SourceQuery, context: DiscoveryContext) -> list[dict[str, Any]]:
        raise NotImplementedError(f"{self.name} has not migrated fetch() yet")

    def normalize(self, item: dict[str, Any], query: SourceQuery, context: DiscoveryContext) -> dict[str, Any]:
        return dict(item)


class PaperlistsSourcePlugin(_SimpleSourcePlugin):
    name = "paperlists"
    description = "Conference/journal paper list baselines"

    def run(self, context: DiscoveryContext) -> dict[str, Any]:
        from ..discovery import resolve_paperlists_venues, sync_paperlists

        venues = resolve_paperlists_venues(
            context.paperlists_venues,
            context.source_config.get("venues"),
            context.topic,
        )
        return sync_paperlists(
            context.workspace,
            context.topic,
            context.years,
            venues=venues,
            refresh=context.refresh,
            timeout=context.timeout,
            budget=context.budget,
        )


class CrossrefSourcePlugin(_SimpleSourcePlugin):
    name = "crossref"
    description = "Crossref publication metadata"

    def run(self, context: DiscoveryContext) -> dict[str, Any]:
        from ..discovery import CROSSREF_API, default_queries, resolve_secret, sync_crossref

        cfg = context.source_config
        queries = cfg.get("queries") or default_queries(context.topic)
        mailto = resolve_secret(
            cfg,
            value_key="mailto",
            env_key="mailto_env",
            default_env="CROSSREF_MAILTO",
        )
        return sync_crossref(
            context.workspace,
            context.topic,
            context.years,
            queries=[clean_text(q) for q in queries if clean_text(q)],
            base_url=clean_text(cfg.get("base_url", "")) or CROSSREF_API,
            max_results=int(cfg.get("max_results", 50)),
            page_size=int(cfg.get("page_size", 25)),
            max_pages=int(cfg.get("max_pages", 1)),
            mailto=mailto,
            refresh=context.refresh,
            timeout=int(cfg.get("timeout", context.timeout)),
            sleep_seconds=float(cfg.get("sleep_seconds", 0.25)),
            budget=context.budget,
        )


class DblpSourcePlugin(_SimpleSourcePlugin):
    name = "dblp"
    description = "DBLP computer-science bibliography"

    def run(self, context: DiscoveryContext) -> dict[str, Any]:
        from ..discovery import DBLP_API, default_queries, sync_dblp

        cfg = context.source_config
        queries = cfg.get("queries") or default_queries(context.topic)
        return sync_dblp(
            context.workspace,
            context.topic,
            context.years,
            queries=[clean_text(q) for q in queries if clean_text(q)],
            base_url=clean_text(cfg.get("base_url", "")) or DBLP_API,
            max_results=int(cfg.get("max_results", 50)),
            page_size=int(cfg.get("page_size", 25)),
            max_pages=int(cfg.get("max_pages", 1)),
            refresh=context.refresh,
            timeout=int(cfg.get("timeout", context.timeout)),
            sleep_seconds=float(cfg.get("sleep_seconds", 0.25)),
            budget=context.budget,
        )


class AclAnthologySourcePlugin(_SimpleSourcePlugin):
    name = "acl_anthology"
    description = "ACL Anthology metadata"

    def run(self, context: DiscoveryContext) -> dict[str, Any]:
        from ..discovery import ACL_ANTHOLOGY_XML, as_list, sync_acl_anthology

        cfg = context.source_config
        venues = [
            clean_text(v).lower()
            for v in as_list(cfg.get("venues") or ["acl", "emnlp", "naacl", "eacl", "coling"])
            if clean_text(v)
        ]
        return sync_acl_anthology(
            context.workspace,
            context.topic,
            context.years,
            venues=venues,
            base_url=clean_text(cfg.get("base_url", "")) or ACL_ANTHOLOGY_XML,
            refresh=context.refresh,
            timeout=int(cfg.get("timeout", context.timeout)),
            sleep_seconds=float(cfg.get("sleep_seconds", 0.25)),
            budget=context.budget,
        )


class EuropePmcSourcePlugin(_SimpleSourcePlugin):
    name = "europepmc"
    description = "Europe PMC biomedical metadata"

    def run(self, context: DiscoveryContext) -> dict[str, Any]:
        from ..discovery import EUROPEPMC_API, default_queries, sync_europepmc

        cfg = context.source_config
        queries = cfg.get("queries") or default_queries(context.topic)
        return sync_europepmc(
            context.workspace,
            context.topic,
            context.years,
            queries=[clean_text(q) for q in queries if clean_text(q)],
            base_url=clean_text(cfg.get("base_url", "")) or EUROPEPMC_API,
            max_results=int(cfg.get("max_results", 50)),
            page_size=int(cfg.get("page_size", 25)),
            max_pages=int(cfg.get("max_pages", 1)),
            refresh=context.refresh,
            timeout=int(cfg.get("timeout", context.timeout)),
            sleep_seconds=float(cfg.get("sleep_seconds", 0.25)),
            budget=context.budget,
        )


class PubMedSourcePlugin(_SimpleSourcePlugin):
    name = "pubmed"
    description = "PubMed biomedical metadata"

    def preflight(self, context: DiscoveryContext) -> SourcePreflight:
        from ..discovery import resolve_secret

        api_key = resolve_secret(
            context.source_config,
            value_key="api_key",
            env_key="api_key_env",
            default_env="NCBI_API_KEY",
        )
        return SourcePreflight(
            source=self.name,
            status="ok",
            auth_state="api_key" if api_key else "anonymous",
            effective_rate_limit_seconds=float(context.source_config.get("sleep_seconds", 0.35)),
        )

    def run(self, context: DiscoveryContext) -> dict[str, Any]:
        from ..discovery import PUBMED_EUTILS_API, default_queries, resolve_secret, sync_pubmed

        cfg = context.source_config
        queries = cfg.get("queries") or default_queries(context.topic)
        api_key = resolve_secret(
            cfg,
            value_key="api_key",
            env_key="api_key_env",
            default_env="NCBI_API_KEY",
        )
        return sync_pubmed(
            context.workspace,
            context.topic,
            context.years,
            queries=[clean_text(q) for q in queries if clean_text(q)],
            base_url=clean_text(cfg.get("base_url", "")) or PUBMED_EUTILS_API,
            max_results=int(cfg.get("max_results", 30)),
            page_size=int(cfg.get("page_size", 20)),
            refresh=context.refresh,
            timeout=int(cfg.get("timeout", context.timeout)),
            sleep_seconds=float(cfg.get("sleep_seconds", 0.35)),
            api_key=api_key,
            budget=context.budget,
        )


class OpenReviewSourcePlugin(_SimpleSourcePlugin):
    name = "openreview"
    description = "OpenReview submissions and decisions"

    def run(self, context: DiscoveryContext) -> dict[str, Any]:
        from ..discovery import OPENREVIEW_API, as_list, sync_openreview

        cfg = context.source_config
        return sync_openreview(
            context.workspace,
            context.topic,
            context.years,
            invitations=[clean_text(item) for item in as_list(cfg.get("invitations")) if clean_text(item)],
            base_url=clean_text(cfg.get("base_url", "")) or OPENREVIEW_API,
            limit=int(cfg.get("limit", 50)),
            max_pages=int(cfg.get("max_pages", 1)),
            refresh=context.refresh,
            timeout=int(cfg.get("timeout", context.timeout)),
            sleep_seconds=float(cfg.get("sleep_seconds", 0.25)),
            budget=context.budget,
        )

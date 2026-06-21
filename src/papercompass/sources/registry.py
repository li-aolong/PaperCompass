from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class SourceDefinition:
    name: str
    description: str = ""
    kind: str = "builtin"
    supports_incremental: bool = False


@dataclass(frozen=True)
class SourceCapabilities:
    name: str
    requires_auth: bool = False
    supports_incremental: bool = False
    supports_cursor: bool = False
    supports_since: bool = False
    default_rate_limit_seconds: float = 0.0


@dataclass(frozen=True)
class SourcePreflight:
    source: str
    status: str
    auth_state: str = "not_required"
    warnings: list[str] = field(default_factory=list)
    effective_rate_limit_seconds: float = 0.0


@dataclass(frozen=True)
class SourceQuery:
    source: str
    query_key: str
    query: str
    params: dict[str, Any] = field(default_factory=dict)
    since: str | None = None
    cursor: str | None = None


@dataclass
class SourceResult:
    source: str
    status: str
    seen: int = 0
    kept: int = 0
    raw_segments: list[str] = field(default_factory=list)
    checkpoint_updates: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DiscoveryContext:
    workspace: Any
    topic: dict[str, Any]
    years: list[int]
    discovery_config: dict[str, Any]
    source_config: dict[str, Any]
    refresh: bool = False
    timeout: int = 35
    budget: Any = None
    paperlists_venues: list[str] | None = None


class SourcePlugin(Protocol):
    name: str
    capabilities: SourceCapabilities

    def preflight(self, context: DiscoveryContext) -> SourcePreflight:
        """Return auth/rate-limit readiness without making network calls."""

    def plan_queries(self, context: DiscoveryContext) -> list[SourceQuery]:
        """Plan source/query-level fetch work for incremental checkpoints."""

    def fetch(self, query: SourceQuery, context: DiscoveryContext) -> Any:
        """Fetch raw source items for one planned query."""

    def normalize(self, item: dict[str, Any], query: SourceQuery, context: DiscoveryContext) -> dict[str, Any]:
        """Normalize one raw source item into PaperCompass candidate shape."""

    def run(self, context: DiscoveryContext) -> dict[str, Any]:
        """Run a discovery source and return a source_result payload."""


class SourceRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, SourceDefinition] = {}
        self._plugins: dict[str, SourcePlugin] = {}

    def register_definition(self, definition: SourceDefinition) -> None:
        name = definition.name.strip().lower()
        if not name:
            raise ValueError("source name cannot be empty")
        if name in self._definitions:
            raise ValueError(f"duplicate source plugin: {name}")
        self._definitions[name] = SourceDefinition(
            name=name,
            description=definition.description,
            kind=definition.kind,
            supports_incremental=definition.supports_incremental,
        )

    def register_plugin(self, plugin: SourcePlugin) -> None:
        name = plugin.capabilities.name.strip().lower()
        if not name:
            raise ValueError("source plugin name cannot be empty")
        if name in self._plugins:
            raise ValueError(f"duplicate source plugin: {name}")
        self._plugins[name] = plugin
        if name not in self._definitions:
            self.register_definition(SourceDefinition(
                name=name,
                description=getattr(plugin, "description", ""),
                supports_incremental=plugin.capabilities.supports_incremental,
            ))

    def names(self) -> list[str]:
        return list(self._definitions.keys())

    def unknown(self, names: list[str]) -> list[str]:
        return [name for name in names if name not in self._definitions]

    def selected(self, names: list[str] | None) -> list[SourceDefinition]:
        if names is None:
            return list(self._definitions.values())
        unknown = self.unknown(names)
        if unknown:
            raise ValueError(f"unknown discovery source(s): {', '.join(unknown)}")
        return [self._definitions[name] for name in names]

    def plugin(self, name: str) -> SourcePlugin | None:
        return self._plugins.get(name.strip().lower())

    def selected_plugins(self, names: list[str] | None) -> list[SourcePlugin]:
        selected_names = self.names() if names is None else names
        unknown = self.unknown(selected_names)
        if unknown:
            raise ValueError(f"unknown discovery source(s): {', '.join(unknown)}")
        return [self._plugins[name] for name in selected_names if name in self._plugins]


BUILTIN_DISCOVERY_SOURCES: list[SourceDefinition] = [
    SourceDefinition("paperlists", "Conference/journal paper list baselines"),
    SourceDefinition("openalex", "OpenAlex metadata and recall"),
    SourceDefinition("crossref", "Crossref publication metadata"),
    SourceDefinition("dblp", "DBLP computer-science bibliography"),
    SourceDefinition("acl_anthology", "ACL Anthology metadata"),
    SourceDefinition("europepmc", "Europe PMC biomedical metadata"),
    SourceDefinition("pubmed", "PubMed biomedical metadata"),
    SourceDefinition("openreview", "OpenReview submissions and decisions"),
    SourceDefinition("semanticscholar", "Semantic Scholar metadata"),
    SourceDefinition("arxiv", "arXiv preprint metadata"),
    SourceDefinition("gemini_search", "Gemini Search assisted recall"),
]


def default_source_registry() -> SourceRegistry:
    registry = SourceRegistry()
    for definition in BUILTIN_DISCOVERY_SOURCES:
        registry.register_definition(definition)
    from .arxiv import ArxivSourcePlugin
    from .builtin import (
        AclAnthologySourcePlugin,
        CrossrefSourcePlugin,
        DblpSourcePlugin,
        EuropePmcSourcePlugin,
        OpenReviewSourcePlugin,
        PaperlistsSourcePlugin,
        PubMedSourcePlugin,
    )
    from .gemini_search import GeminiSearchSourcePlugin
    from .openalex import OpenAlexSourcePlugin
    from .semantic_scholar import SemanticScholarSourcePlugin

    registry.register_plugin(PaperlistsSourcePlugin())
    registry.register_plugin(CrossrefSourcePlugin())
    registry.register_plugin(DblpSourcePlugin())
    registry.register_plugin(AclAnthologySourcePlugin())
    registry.register_plugin(EuropePmcSourcePlugin())
    registry.register_plugin(PubMedSourcePlugin())
    registry.register_plugin(OpenReviewSourcePlugin())
    registry.register_plugin(ArxivSourcePlugin())
    registry.register_plugin(OpenAlexSourcePlugin())
    registry.register_plugin(SemanticScholarSourcePlugin())
    registry.register_plugin(GeminiSearchSourcePlugin())
    return registry

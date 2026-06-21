import pytest

from papercompass.config import init_workspace
from papercompass.discovery import run_discovery
from papercompass.sources.arxiv import ArxivSourcePlugin
from papercompass.sources.openalex import OpenAlexSourcePlugin
from papercompass.sources.semantic_scholar import SemanticScholarSourcePlugin
from papercompass.sources.registry import DiscoveryContext, SourceDefinition, SourceRegistry, default_source_registry


def test_default_source_registry_lists_builtin_discovery_sources() -> None:
    registry = default_source_registry()
    names = registry.names()

    assert names[:5] == ["paperlists", "openalex", "crossref", "dblp", "acl_anthology"]
    assert "arxiv" in names
    assert "gemini_search" in names
    assert registry.plugin("arxiv") is not None
    assert registry.plugin("openalex") is not None
    assert registry.plugin("semanticscholar") is not None
    assert registry.plugin("gemini_search") is not None
    assert all(registry.plugin(name) is not None for name in names)


def test_builtin_source_plugins_expose_protocol_methods() -> None:
    registry = default_source_registry()

    for plugin in registry.selected_plugins(None):
        assert callable(getattr(plugin, "preflight"))
        assert callable(getattr(plugin, "plan_queries"))
        assert callable(getattr(plugin, "fetch"))
        assert callable(getattr(plugin, "normalize"))
        assert callable(getattr(plugin, "run"))


def test_source_registry_rejects_duplicate_names() -> None:
    registry = SourceRegistry()
    registry.register_definition(SourceDefinition("arxiv"))

    with pytest.raises(ValueError, match="duplicate source plugin"):
        registry.register_definition(SourceDefinition("arxiv"))


def test_discovery_rejects_unknown_source_before_network(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")

    with pytest.raises(ValueError, match="unknown discovery source"):
        run_discovery(
            workspace,
            sources=["missing_source"],
            min_year=2022,
            build=False,
            catalog=False,
        )


def test_arxiv_plugin_is_used_by_run_discovery(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "topic.yaml").write_text(
        "topic_id: topic\nname: Topic\nmin_year: 2024\nsearch_hints:\n  - alpha beta\n",
        encoding="utf-8",
    )
    (workspace / "sources.yaml").write_text(
        "discovery:\n"
        "  sources: [arxiv]\n"
        "  arxiv:\n"
        "    queries: ['all:\"alpha beta\"']\n"
        "    max_results: 1\n"
        "    page_size: 1\n"
        "    sleep_seconds: 0\n",
        encoding="utf-8",
    )
    calls = {}

    def fake_fetch(self, query, context):
        calls["query"] = query
        calls["context"] = context
        return [{
            "title": "Alpha Beta",
            "year": 2024,
            "abstract": "alpha beta",
            "arxiv_id": "2401.00001",
        }]

    monkeypatch.setattr(ArxivSourcePlugin, "fetch", fake_fetch)

    result = run_discovery(
        workspace,
        sources=["arxiv"],
        min_year=2024,
        max_year=2024,
        build=False,
        catalog=False,
        max_remote_calls=5,
    )

    assert calls["context"].workspace == workspace
    assert calls["context"].years == [2024]
    assert calls["context"].budget is not None
    assert result["source_results"][0]["source"] == "arxiv"
    assert result["source_results"][0]["runner"] == "structured_plan_fetch_normalize"
    assert result["source_results"][0]["kept"] == 1


def test_arxiv_plugin_plans_fetches_and_normalizes(tmp_path, monkeypatch) -> None:
    plugin = ArxivSourcePlugin()
    context = DiscoveryContext(
        workspace=tmp_path,
        topic={"topic_id": "topic", "min_year": 2024, "arxiv_queries": ['all:"alpha beta"']},
        years=[2024],
        discovery_config={},
        source_config={"max_results": 10, "page_size": 5, "sleep_seconds": 0},
    )
    planned = plugin.plan_queries(context)

    assert len(planned) == 2
    assert planned[0].source == "arxiv"
    assert planned[0].since == "2024-01-01"
    assert "submittedDate:[202401010000 TO 202412312359]" in planned[0].query

    def fake_arxiv_search(query, **kwargs):
        assert query == planned[0].query
        assert kwargs["start"] == 0
        assert kwargs["max_results"] == 5
        return {
            "total": 1,
            "data": [{
                "title": " Alpha Beta ",
                "year": 2024,
                "abstract": " Result ",
                "arxiv_id": "2401.00001",
            }],
        }

    monkeypatch.setattr("papercompass.discovery.arxiv_search", fake_arxiv_search)

    items = plugin.fetch(planned[0], context)
    raw = plugin.normalize(items[0], planned[0], context)

    assert raw["title"] == "Alpha Beta"
    assert raw["venue"] == "arXiv"
    assert raw["url"] == "https://arxiv.org/abs/2401.00001"
    assert raw["pdf_url"] == "https://arxiv.org/pdf/2401.00001.pdf"


def test_openalex_plugin_is_used_by_run_discovery(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "topic.yaml").write_text(
        "topic_id: topic\nname: Topic\nmin_year: 2024\nsearch_hints:\n  - alpha beta\n",
        encoding="utf-8",
    )
    (workspace / "sources.yaml").write_text(
        "discovery:\n"
        "  sources: [openalex]\n"
        "  openalex:\n"
        "    queries:\n"
        "      - text: alpha beta\n"
        "        strength: strong\n",
        encoding="utf-8",
    )
    calls = {}

    def fake_fetch(self, query, context):
        calls["query"] = query
        calls["context"] = context
        return [{
            "id": "https://openalex.org/W1",
            "display_name": "Alpha Beta",
            "publication_year": 2024,
            "abstract_inverted_index": {"Alpha": [0], "beta": [1]},
        }]

    monkeypatch.setattr(OpenAlexSourcePlugin, "fetch", fake_fetch)

    result = run_discovery(
        workspace,
        sources=["openalex"],
        min_year=2024,
        max_year=2024,
        build=False,
        catalog=False,
        max_remote_calls=5,
    )

    assert calls["context"].workspace == workspace
    assert calls["context"].years == [2024]
    assert calls["context"].budget is not None
    assert calls["query"].query == "alpha beta"
    assert result["source_results"][0]["source"] == "openalex"
    assert result["source_results"][0]["runner"] == "structured_plan_fetch_normalize"
    assert "openalex" in result["source_budgets"]


def test_openalex_plugin_plans_fetches_and_normalizes(tmp_path, monkeypatch) -> None:
    plugin = OpenAlexSourcePlugin()
    context = DiscoveryContext(
        workspace=tmp_path,
        topic={"topic_id": "topic", "min_year": 2024},
        years=[2024],
        discovery_config={},
        source_config={
            "queries": [{"text": "alpha beta", "modes": ["title"], "strength": "must_recall"}],
            "max_pages": 1,
            "page_size": 5,
            "sleep_seconds": 0,
            "api_key": "dummy",
            "mailto": "test@example.com",
        },
    )
    planned = plugin.plan_queries(context)

    assert len(planned) == 1
    assert planned[0].source == "openalex"
    assert planned[0].query_key.startswith("openalex/title/alpha-beta/2024")
    assert planned[0].params["strength"] == "must_recall"
    assert "api_key" not in planned[0].params
    assert "mailto" not in planned[0].params

    captured_urls: list[str] = []

    def fake_http_get_json(url, **kwargs):
        captured_urls.append(url)
        return {
            "meta": {"count": 1, "next_cursor": ""},
            "results": [{
                "id": "https://openalex.org/W1",
                "display_name": "Alpha Beta",
                "publication_year": 2024,
                "publication_date": "2024-01-01",
                "cited_by_count": 3,
                "primary_location": {"source": {"display_name": "ACL"}},
                "authorships": [{"author": {"display_name": "Alice"}}],
                "ids": {"arxiv": "2401.00001"},
                "abstract_inverted_index": {"Alpha": [0], "beta": [1]},
            }],
        }

    monkeypatch.setattr("papercompass.discovery.http_get_json", fake_http_get_json)

    items = plugin.fetch(planned[0], context)
    raw = plugin.normalize(items[0], planned[0], context)

    assert captured_urls
    assert "title.search%3Aalpha+beta" in captured_urls[0]
    assert "api_key=dummy" in captured_urls[0]
    assert raw["openalex_id"] == "https://openalex.org/W1"
    assert raw["title"] == "Alpha Beta"
    assert raw["abstract"] == "Alpha beta"
    assert raw["authors"] == "Alice"


def test_semantic_scholar_plugin_is_used_by_run_discovery(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "topic.yaml").write_text(
        "topic_id: topic\nname: Topic\nmin_year: 2024\nsearch_hints:\n  - alpha beta\n",
        encoding="utf-8",
    )
    (workspace / "sources.yaml").write_text(
        "discovery:\n"
        "  sources: [semanticscholar]\n"
        "  semanticscholar:\n"
        "    queries: [alpha beta]\n"
        "    max_results: 10\n"
        "    page_size: 5\n",
        encoding="utf-8",
    )
    calls = {}

    def fake_fetch(self, query, context):
        calls["query"] = query
        calls["context"] = context
        return [{
            "paperId": "S1",
            "title": "Alpha Beta",
            "abstract": "alpha beta",
            "year": 2024,
            "url": "https://example.test/paper",
        }]

    monkeypatch.setattr(SemanticScholarSourcePlugin, "fetch", fake_fetch)

    result = run_discovery(
        workspace,
        sources=["semanticscholar"],
        min_year=2024,
        max_year=2024,
        build=False,
        catalog=False,
        max_remote_calls=5,
    )

    assert calls["context"].workspace == workspace
    assert calls["context"].years == [2024]
    assert calls["context"].budget is not None
    assert calls["query"].query == "alpha beta"
    assert calls["query"].params["max_results"] == 10
    assert calls["query"].params["page_size"] == 5
    assert result["source_results"][0]["source"] == "semanticscholar"
    assert result["source_results"][0]["runner"] == "structured_plan_fetch_normalize"
    assert "semanticscholar" in result["source_budgets"]


def test_semantic_scholar_plugin_plans_fetches_and_normalizes(tmp_path, monkeypatch) -> None:
    plugin = SemanticScholarSourcePlugin()
    context = DiscoveryContext(
        workspace=tmp_path,
        topic={"topic_id": "topic", "min_year": 2024},
        years=[2024],
        discovery_config={},
        source_config={
            "queries": ["alpha beta"],
            "max_results": 5,
            "page_size": 5,
            "max_pages": 1,
            "sleep_seconds": 0,
            "no_key_sleep_seconds": 0,
            "api_key": "dummy",
        },
    )
    planned = plugin.plan_queries(context)

    assert len(planned) == 2
    assert planned[0].source == "semanticscholar"
    assert planned[0].query == "alpha beta"
    assert planned[0].params["year_bucket"] == "2024"

    captured_urls: list[str] = []

    def fake_http_get_json(url, **kwargs):
        captured_urls.append(url)
        assert kwargs["headers"] == {"x-api-key": "dummy"}
        return {
            "data": [{
                "paperId": "S1",
                "title": "Alpha Beta",
                "abstract": "Result",
                "url": "https://example.test/paper",
                "year": 2024,
                "citationCount": 7,
                "authors": [{"name": "Alice"}],
                "externalIds": {"DOI": "10.1/example"},
            }]
        }

    monkeypatch.setattr("papercompass.discovery.http_get_json", fake_http_get_json)

    items = plugin.fetch(planned[0], context)
    raw = plugin.normalize(items[0], planned[0], context)

    assert captured_urls
    assert "paper/search/bulk" in captured_urls[0]
    assert "query=alpha+beta" in captured_urls[0]
    assert raw["semantic_scholar_id"] == "S1"
    assert raw["title"] == "Alpha Beta"
    assert raw["authors"] == "Alice"
    assert raw["doi"] == "10.1/example"

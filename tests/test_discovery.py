import json
import re
import urllib.error

from papercompass.config import init_workspace
from papercompass.discovery import RemoteBudget, assign_tags, cache_manifest_key, candidate_identity, coverage_health, openalex_item_to_raw
from papercompass.discovery import crossref_item_to_raw, dblp_item_to_raw, europepmc_item_to_raw, pubmed_summary_to_raw
from papercompass.discovery import default_discovery_sources, run_discovery, sync_arxiv_discovery, sync_europepmc, sync_openalex, sync_paperlists, sync_semantic_scholar, year_range
from papercompass.discovery import topic_match_decision, _topic_discriminators
from papercompass.discovery import ss_item_to_raw, matches_topic
from papercompass.discovery import resolve_paperlists_venues


def test_year_range_only_fetches_requested_extension() -> None:
    topic = {"min_year": 2023}
    assert year_range(topic, None, 2024) == [2023, 2024]
    assert year_range(topic, 2022, 2024) == [2022, 2023, 2024]


def test_coverage_health_does_not_mark_budget_complete_caps_as_partial() -> None:
    health = coverage_health("success", source_exhausted=False, budget_complete=True)
    assert health["coverage_risk"] == "low"

    health = coverage_health("success", source_exhausted=False, budget_complete=False)
    assert health["coverage_risk"] == "medium"


def test_assign_tags_for_chinese_spelling() -> None:
    topic = {
        "tag_rules": [
            {"tag": "language:zh", "patterns": [r"\bChinese\b"]},
            {"tag": "task:csc", "patterns": [r"\bChinese spelling correction\b"]},
        ]
    }
    tags = assign_tags(
        {
            "title": "A Benchmark for Chinese Spelling Correction",
            "abstract": "Chinese spelling correction with SIGHAN data.",
            "venue": "ACL",
        },
        topic,
        source_name="paperlists",
    )
    assert "language:zh" in tags
    assert "task:csc" in tags
    assert "task:spelling" in tags
    assert "source:paperlists" in tags


def test_manifest_key_and_candidate_identity_are_stable() -> None:
    key = cache_manifest_key("semanticscholar", "grammatical error correction", 2024, 0)
    assert key == "semanticscholar/grammatical-error-correction/2024/0"
    assert candidate_identity({"title": "A Paper", "year": 2024}) == "title:a paper:2024"
    assert candidate_identity({"openalex_id": "https://openalex.org/W123"}) == "openalex_id:https://openalex.org/w123"


def test_remote_budget_child_caps_source_without_losing_global_usage() -> None:
    budget = RemoteBudget(limit=5)
    arxiv_budget = budget.child("arxiv", limit=1)

    arxiv_budget.consume("arxiv", "fake://one")

    assert budget.used == 1
    assert arxiv_budget.used == 1
    try:
        arxiv_budget.consume("arxiv", "fake://two")
    except RuntimeError as exc:
        assert "remote call budget exceeded: arxiv 1/1" in str(exc)
    else:
        raise AssertionError("source budget should stop the second call")


def test_openalex_item_to_raw_extracts_core_metadata() -> None:
    raw = openalex_item_to_raw({
        "id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.18653/v1/test",
        "display_name": "A Grammatical Error Correction Paper",
        "publication_year": 2024,
        "publication_date": "2024-08-01",
        "cited_by_count": 12,
        "primary_location": {
            "pdf_url": "https://example.org/paper.pdf",
            "source": {"display_name": "ACL"},
        },
        "open_access": {"oa_url": "https://example.org/paper.pdf"},
        "authorships": [
            {"author": {"display_name": "Alice"}},
            {"author": {"display_name": "Bob"}},
        ],
        "ids": {"arxiv": "2401.00001"},
        "abstract_inverted_index": {"We": [0], "study": [1], "GEC": [2]},
        "keywords": [{"display_name": "Grammatical Error Correction"}],
        "topics": [{"display_name": "Natural Language Processing Techniques"}],
        "primary_topic": {"display_name": "Natural Language Processing Techniques"},
    })

    assert raw["openalex_id"] == "https://openalex.org/W123"
    assert raw["doi"] == "https://doi.org/10.18653/v1/test"
    assert raw["arxiv_id"] == "2401.00001"
    assert raw["title"] == "A Grammatical Error Correction Paper"
    assert raw["authors"] == "Alice; Bob"
    assert raw["venue"] == "ACL"
    assert raw["abstract"] == "We study GEC"
    assert raw["citation_count"] == 12
    assert raw["keywords"] == "Grammatical Error Correction"


def test_default_discovery_sources_are_broad_without_semantic_scholar() -> None:
    sources = default_discovery_sources({
        "topic_id": "llm-agents",
        "name": "LLM agents",
        "description": "language model agent papers in NLP venues",
    })

    assert "paperlists" in sources
    assert "openalex" in sources
    assert "crossref" in sources
    assert "dblp" in sources
    assert "arxiv" in sources
    assert "acl_anthology" in sources
    assert "semanticscholar" not in sources


def test_new_source_normalizers_extract_stable_ids() -> None:
    crossref = crossref_item_to_raw({
        "DOI": "10.1234/test",
        "title": ["Retrieval Augmented Generation"],
        "container-title": ["Proceedings"],
        "issued": {"date-parts": [[2024]]},
        "author": [{"given": "Alice", "family": "Smith"}],
        "URL": "https://doi.org/10.1234/test",
        "is-referenced-by-count": 7,
    })
    assert crossref["title"] == "Retrieval Augmented Generation"
    assert crossref["doi"] == "10.1234/test"
    assert crossref["authors"] == "Alice Smith"

    dblp = dblp_item_to_raw({
        "info": {
            "title": "A DBLP Paper",
            "year": "2025",
            "venue": "ACL",
            "key": "conf/acl/test",
            "authors": {"author": [{"text": "Alice"}, {"text": "Bob"}]},
        }
    })
    assert dblp["dblp_key"] == "conf/acl/test"
    assert dblp["authors"] == "Alice; Bob"

    epmc = europepmc_item_to_raw({
        "title": "Clinical RAG",
        "authorString": "Alice; Bob",
        "pubYear": "2026",
        "journalTitle": "Journal",
        "doi": "10.1234/clinical",
        "pmid": "12345",
        "pmcid": "PMC12345",
    })
    assert epmc["pmid"] == "12345"
    assert epmc["pmcid"] == "PMC12345"

    pubmed = pubmed_summary_to_raw({
        "uid": "67890",
        "title": "PubMed Paper",
        "pubdate": "2025 Jan",
        "fulljournalname": "Journal",
        "authors": [{"name": "Alice"}],
        "articleids": [{"idtype": "doi", "value": "10.1234/pubmed"}],
    })
    assert pubmed["pmid"] == "67890"
    assert pubmed["doi"] == "10.1234/pubmed"


def test_sync_openalex_honors_query_level_page_size(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    captured_urls: list[str] = []

    def fake_http_get_json(url, **kwargs):
        captured_urls.append(url)
        return {
            "meta": {"count": 1, "next_cursor": ""},
            "results": [
                {
                    "id": "https://openalex.org/W123",
                    "display_name": "Seed Title",
                    "publication_year": 2024,
                    "publication_date": "2024-01-01",
                    "type": "article",
                    "cited_by_count": 0,
                    "primary_location": {"source": {"display_name": "arXiv"}},
                    "open_access": {},
                    "authorships": [],
                    "ids": {},
                    "abstract_inverted_index": {},
                    "keywords": [],
                    "topics": [],
                    "primary_topic": {"display_name": ""},
                }
            ],
        }

    monkeypatch.setattr("papercompass.discovery.http_get_json", fake_http_get_json)

    result = sync_openalex(
        workspace,
        topic={"topic_id": "x", "min_year": 2024},
        years=[2024],
        queries=[
            {
                "text": "Seed Title",
                "strength": "must_recall",
                "modes": ["title"],
                "max_pages": 1,
                "page_size": 5,
            }
        ],
        page_size=100,
        max_pages=1,
        sleep_seconds=0,
    )

    assert result["kept"] == 1
    assert captured_urls
    assert "per-page=5" in captured_urls[0]
    assert "title.search%3ASeed+Title" in captured_urls[0]


def test_sync_paperlists_writes_matching_cached_items(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    cache_path = workspace / ".papercompass" / "cache" / "discovery" / "paperlists" / "acl" / "2024.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps([
        {
            "id": "2024.acl-long.001",
            "title": "A Study of Grammatical Error Correction",
            "abstract": "We study grammatical error correction for learner text.",
            "site": "https://aclanthology.org/2024.acl-long.001/",
            "pdf": "https://aclanthology.org/2024.acl-long.001.pdf",
            "authors": [{"name": "Alice"}],
        },
        {
            "id": "2024.acl-long.002",
            "title": "Feedback for Learner Writing",
            "abstract": "A writing feedback system for learner texts.",
        },
        {
            "id": "2024.acl-long.003",
            "title": "Unrelated Parsing Paper",
            "abstract": "A dependency parsing method.",
        },
    ]), encoding="utf-8")
    coverage_path = workspace / ".papercompass" / "manifests" / "source_coverage.json"
    coverage_path.parent.mkdir(parents=True, exist_ok=True)
    coverage_path.write_text(json.dumps({
        "paperlists/acl/2024": {"complete": True}
    }), encoding="utf-8")

    result = sync_paperlists(
        workspace,
        topic={
            "topic_id": "gec",
            "min_year": 2022,
            "search_hints": ["grammatical error correction", "writing feedback"],
        },
        years=[2024],
        venues=["acl"],
    )

    assert result["seen"] == 3
    assert result["kept"] == 2
    raw_files = list((workspace / ".raw" / "paperlists" / "acl").glob("*.jsonl"))
    assert len(raw_files) == 1
    rows = [json.loads(line) for line in raw_files[0].read_text(encoding="utf-8").splitlines()]
    assert rows[0]["source_type"] == "paperlists"
    assert rows[0]["raw"]["title"] == "A Study of Grammatical Error Correction"
    assert rows[0]["discovery_confidence"] == "weak"
    assert rows[1]["raw"]["title"] == "Feedback for Learner Writing"
    assert rows[1]["discovery_confidence"] == "weak"


def test_sync_paperlists_marks_missing_sparse_year_as_low_risk(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")

    def fake_http_get_json(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://example.invalid/acl2026.json",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("papercompass.discovery.http_get_json", fake_http_get_json)

    result = sync_paperlists(
        workspace,
        topic={"topic_id": "implicit-cot", "search_hints": ["latent reasoning"]},
        years=[2026],
        venues=["acl"],
    )

    assert result["kept"] == 0
    coverage = json.loads((workspace / ".papercompass" / "manifests" / "source_coverage.json").read_text(encoding="utf-8"))
    entry = next(iter(coverage.values()))
    assert entry["execution_status"] == "source_missing"
    assert entry["coverage_risk"] == "low"


def test_v3_filter_uses_explicit_discriminator_terms() -> None:
    topic = {
        "search_hints": ["small language model agent", "Phi-3 agent"],
        "discriminator_terms": ["TinyLlama", "Phi-3", "Octopus v2", "edge LLM"],
    }
    on_topic = {"title": "Octopus v2: On-device language model for super agent",
                "abstract": "We present a small model designed for tool calling."}
    off_topic = {"title": "Improving GPT-4 web agents with new prompt strategies",
                 "abstract": "We show large model agents benefit from chain of thought."}
    assert topic_match_decision(on_topic, topic)["include"] is True
    assert topic_match_decision(off_topic, topic)["include"] is False


def test_v3_filter_merges_explicit_terms_with_auto_recall_anchors() -> None:
    topic = {
        "search_hints": [
            "small language model agent",
            "on-device language model agent",
            "edge LLM function calling",
        ],
        "search_keyword_text": (
            "Small language model agents, compact LLM tool use, "
            "on-device language model agents, edge LLM."
        ),
        "discriminator_terms": ["Octopus v3", "CAMPHOR"],
    }

    discs = _topic_discriminators(topic)
    assert "octopus v3" in discs
    assert "small language model" in discs
    assert "on-device language model" in discs
    assert "edge llm" in discs
    decision = topic_match_decision(
        {
            "title": "A Benchmark for Edge LLM Function Calling",
            "abstract": "We evaluate compact models for API use.",
        },
        topic,
    )
    assert decision["include"] is True
    assert "edge llm" in decision["hits"]


def test_v3_filter_auto_extracts_when_no_explicit_discriminators() -> None:
    topic = {
        "search_hints": ["TinyLlama agent", "compact LLM agent",
                         "on-device language model tool use"],
        "search_keyword_text": "edge LLM, TinyLlama based agent, Phi-3 agent.",
    }
    discs = _topic_discriminators(topic)
    assert "tinyllama" in discs
    assert "phi-3" in discs
    assert "on-device language model" in discs
    on_topic = {"title": "TinyLlama-based agent for tool use",
                "abstract": "TinyLlama is a 1.1B model."}
    off_topic = {"title": "Large language model evaluation",
                 "abstract": "Survey of GPT-4 reasoning benchmarks."}
    assert topic_match_decision(on_topic, topic)["include"] is True
    assert topic_match_decision(off_topic, topic)["include"] is False


def test_v3_filter_uses_term_boundaries_for_short_codenames() -> None:
    topic = {
        "search_hints": ["continuous chain-of-thought"],
        "discriminator_terms": ["CODI"],
    }
    exact = {
        "title": "CODI: Compressing Chain-of-Thought into Continuous Space",
        "abstract": "We study continuous chain-of-thought reasoning.",
    }
    substring_noise = {
        "title": "Contrastive Decoding for Coding Agents",
        "abstract": "We study cognitive offloading and code generation.",
    }

    assert topic_match_decision(exact, topic)["include"] is True
    decision = topic_match_decision(substring_noise, topic)
    assert decision["include"] is False
    assert "codi" not in decision["hits"]


def test_v3_filter_rejects_generic_anchor_only_hits() -> None:
    topic = {
        "search_hints": ["test-time", "implicit chain of thought"],
        "discriminator_terms": ["hidden state"],
    }
    generic_only = {
        "title": "Test-time scaling for language model reasoning",
        "abstract": "We evaluate reasoning with hidden state analysis.",
    }
    specific = {
        "title": "Implicit Chain of Thought in Latent Reasoning",
        "abstract": "A test-time method for hidden state reasoning.",
    }

    generic_decision = topic_match_decision(generic_only, topic)
    assert generic_decision["include"] is False
    assert generic_decision["reason"] == "v3_generic_only_match"
    assert topic_match_decision(specific, topic)["include"] is True


def test_v3_filter_rejects_latent_space_only_but_keeps_latent_reasoning() -> None:
    topic = {
        "search_hints": ["implicit chain-of-thought"],
        "discriminator_terms": ["latent space"],
        "source_filter_terms": ["latent reasoning", "latent space reasoning"],
    }
    latent_space_noise = {
        "title": "Diffusion Models Already Have A Semantic Latent Space",
        "abstract": "We analyze semantic directions in the latent space of image diffusion models.",
    }
    latent_reasoning = {
        "title": "Scaling Latent Reasoning via Looped Language Models",
        "abstract": "A test-time method for latent reasoning in language models.",
    }
    latent_space_reasoning = {
        "title": "Training Large Language Models to Reason in a Continuous Latent Space",
        "abstract": "We study latent space reasoning as an implicit chain-of-thought method.",
    }

    noise_decision = topic_match_decision(latent_space_noise, topic)
    assert noise_decision["include"] is False
    assert noise_decision["reason"] == "v3_no_hint_match"
    assert topic_match_decision(latent_reasoning, topic)["include"] is True
    assert topic_match_decision(latent_space_reasoning, topic)["include"] is True


def test_v3_filter_source_filter_terms_override_keyword_phrase_noise() -> None:
    topic = {
        "search_hints": ["latent reasoning"],
        "search_keyword_text": "token-level reasoning, latent variable reasoning",
        "source_filter_terms": ["latent reasoning"],
    }
    token_level_noise = {
        "title": "Token-Level Alignment for Language Models",
        "abstract": "We study token-level losses and latent variable objectives.",
    }
    latent_reasoning = {
        "title": "Scaling Latent Reasoning via Looped Language Models",
        "abstract": "A method for latent reasoning at test time.",
    }

    discs = _topic_discriminators(topic)
    assert discs == ["latent reasoning"]
    assert topic_match_decision(token_level_noise, topic)["include"] is False
    assert topic_match_decision(latent_reasoning, topic)["include"] is True


def test_v3_filter_keeps_full_representation_phrase_from_keyword_text() -> None:
    topic = {
        "search_hints": [],
        "search_keyword_text": "latent space reasoning",
    }
    latent_space_noise = {
        "title": "Latent Space Navigation for Image Animation",
        "abstract": "A method for controllable image generation in latent space.",
    }
    latent_space_reasoning = {
        "title": "Latent Space Reasoning for Language Models",
        "abstract": "Implicit reasoning without verbal chain-of-thought.",
    }

    assert topic_match_decision(latent_space_noise, topic)["include"] is False
    decision = topic_match_decision(latent_space_reasoning, topic)
    assert decision["include"] is True
    assert "latent space reasoning" in decision["hits"]


def test_v3_filter_no_signal_when_no_hints_no_discriminators() -> None:
    topic = {"min_year": 2020}
    item = {"title": "Anything", "year": 2024}
    decision = topic_match_decision(item, topic)
    assert decision["include"] is True
    assert decision["reason"] == "v3_wide_recall_no_hints"


def test_resolve_paperlists_venues_distinguishes_none_from_empty() -> None:
    topic = {"topic_id": "slm-agents"}
    # CLI override wins; explicit empty list = disabled (do NOT rehydrate).
    assert resolve_paperlists_venues([], None, topic) == []
    # CLI explicit list wins over sources.yaml.
    assert resolve_paperlists_venues(["acl"], ["nips"], topic) == ["acl"]
    # CLI absent + sources.yaml empty = disabled.
    assert resolve_paperlists_venues(None, [], topic) == []
    # CLI absent + sources.yaml absent = default_venues.
    assert "iclr" in resolve_paperlists_venues(None, None, topic)


def test_semantic_scholar_results_apply_topic_filter() -> None:
    # Regression: SS used to skip topic_match_decision (P0 leak — every SS
    # result ended up in pending regardless of topic). The filter has the
    # same shape as paperlists / openalex / arxiv now; this test pins the
    # contract on the matches_topic side rather than re-mocking the full
    # bulk-search HTTP path.
    topic = {
        "discriminator_terms": ["TinyLlama", "Phi-3", "small language model"],
    }
    on_topic = ss_item_to_raw({
        "title": "TinyLlama: An Open-Source Small Language Model",
        "abstract": "We pretrain a small language model with 1.1B parameters.",
        "year": 2024,
    })
    off_topic = ss_item_to_raw({
        "title": "Improving GPT-4 reasoning via prompt engineering",
        "abstract": "We study large model agents and chain-of-thought.",
        "year": 2024,
    })
    assert matches_topic(on_topic, topic) is True
    assert matches_topic(off_topic, topic) is False


def test_arxiv_rate_limit_breaker_stops_remaining_buckets(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    calls = []

    def fake_arxiv_search(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr("papercompass.discovery.arxiv_search", fake_arxiv_search)

    result = sync_arxiv_discovery(
        workspace,
        topic={"topic_id": "slm", "min_year": 2022, "search_hints": ["small language model"]},
        years=[2022, 2023, 2024, 2025],
        queries=['all:"small language model agent"'],
        max_results=10,
        page_size=10,
        sleep_seconds=0,
        rate_limit_error_limit=2,
    )

    assert len(calls) == 2
    assert result["stopped_early"] is True
    assert "breaker" in result["stop_reason"]
    coverage = json.loads((workspace / ".papercompass" / "manifests" / "source_coverage.json").read_text(encoding="utf-8"))
    assert len(coverage) == 2
    assert any(item.get("rate_limit_breaker_triggered") for item in coverage.values())


def test_arxiv_empty_success_does_not_reset_rate_limit_breaker(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    calls = []

    def fake_arxiv_search(query, **kwargs):
        calls.append(query)
        if len(calls) in {1, 3}:
            raise RuntimeError("HTTP Error 429: Too Many Requests")
        return {"total": 0, "data": []}

    monkeypatch.setattr("papercompass.discovery.arxiv_search", fake_arxiv_search)

    result = sync_arxiv_discovery(
        workspace,
        topic={"topic_id": "latent", "min_year": 2022, "search_hints": ["latent reasoning"]},
        years=[2022, 2023, 2024],
        queries=['all:"latent reasoning"'],
        max_results=10,
        page_size=10,
        sleep_seconds=0,
        rate_limit_error_limit=2,
    )

    assert len(calls) == 3
    assert result["stopped_early"] is True
    assert "breaker" in result["stop_reason"]
    coverage = json.loads((workspace / ".papercompass" / "manifests" / "source_coverage.json").read_text(encoding="utf-8"))
    assert len(coverage) == 3
    assert any(item.get("rate_limit_breaker_triggered") for item in coverage.values())


def test_arxiv_budget_exhaustion_stops_remaining_buckets(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    calls = []

    def fake_arxiv_search(*args, **kwargs):
        calls.append((args, kwargs))
        budget = kwargs.get("budget")
        if budget:
            budget.consume("arxiv", "fake://arxiv")
        return {"total": 0, "data": []}

    monkeypatch.setattr("papercompass.discovery.arxiv_search", fake_arxiv_search)

    result = sync_arxiv_discovery(
        workspace,
        topic={"topic_id": "slm", "min_year": 2022, "search_hints": ["small language model"]},
        years=[2022, 2023, 2024, 2025],
        queries=['all:"small language model agent"', 'all:"compact llm"'],
        max_results=10,
        page_size=10,
        sleep_seconds=0,
        budget=RemoteBudget(limit=1),
    )

    assert len(calls) == 2
    assert result["stopped_early"] is True
    assert "remote call budget exceeded" in result["stop_reason"]
    coverage = json.loads((workspace / ".papercompass" / "manifests" / "source_coverage.json").read_text(encoding="utf-8"))
    assert len(coverage) == 2
    statuses = {item["execution_status"] for item in coverage.values()}
    assert statuses == {"success", "budget_exhausted"}
    budget_entry = [item for item in coverage.values() if item["execution_status"] == "budget_exhausted"][0]
    assert budget_entry["coverage_risk"] == "medium"


def test_run_discovery_auto_floors_legacy_arxiv_budget(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "topic.yaml").write_text(
        "topic_id: topic\nmin_year: 2024\nsource_filter_terms:\n  - latent reasoning\n",
        encoding="utf-8",
    )
    queries = [f'all:"latent reasoning {idx}"' for idx in range(9)]
    (workspace / "sources.yaml").write_text(
        "discovery:\n"
        "  sources: [arxiv]\n"
        "  max_remote_calls: 200\n"
        "  arxiv:\n"
        "    max_remote_calls: 24\n"
        "    max_results: 35\n"
        "    page_size: 35\n"
        "    recent_year_count: 3\n"
        "    sleep_seconds: 0\n"
        "    queries:\n"
        + "".join(f"      - {query!r}\n" for query in queries),
        encoding="utf-8",
    )
    calls = []

    def fake_arxiv_search(query, **kwargs):
        calls.append(query)
        budget = kwargs.get("budget")
        if budget:
            budget.consume("arxiv", "fake://arxiv")
        return {"total": 0, "data": []}

    monkeypatch.setattr("papercompass.discovery.arxiv_search", fake_arxiv_search)

    result = run_discovery(
        workspace,
        min_year=2024,
        max_year=2026,
        sources=["arxiv"],
        build=False,
    )

    assert len(calls) == 27
    assert result["source_results"][0]["stopped_early"] is False
    assert result["source_budgets"]["arxiv"]["used"] == 27
    assert result["source_budgets"]["arxiv"]["limit"] > 24


def test_run_discovery_respects_fixed_arxiv_budget(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "topic.yaml").write_text(
        "topic_id: topic\nmin_year: 2024\nsource_filter_terms:\n  - latent reasoning\n",
        encoding="utf-8",
    )
    queries = [f'all:"latent reasoning {idx}"' for idx in range(9)]
    (workspace / "sources.yaml").write_text(
        "discovery:\n"
        "  sources: [arxiv]\n"
        "  max_remote_calls: 200\n"
        "  arxiv:\n"
        "    budget_policy: fixed\n"
        "    max_remote_calls: 24\n"
        "    max_results: 35\n"
        "    page_size: 35\n"
        "    recent_year_count: 3\n"
        "    sleep_seconds: 0\n"
        "    queries:\n"
        + "".join(f"      - {query!r}\n" for query in queries),
        encoding="utf-8",
    )
    calls = []

    def fake_arxiv_search(query, **kwargs):
        calls.append(query)
        budget = kwargs.get("budget")
        if budget:
            budget.consume("arxiv", "fake://arxiv")
        return {"total": 0, "data": []}

    monkeypatch.setattr("papercompass.discovery.arxiv_search", fake_arxiv_search)

    result = run_discovery(
        workspace,
        min_year=2024,
        max_year=2026,
        sources=["arxiv"],
        build=False,
    )

    assert len(calls) == 25
    assert result["source_results"][0]["stopped_early"] is True
    assert "remote call budget exceeded" in result["source_results"][0]["stop_reason"]
    assert result["source_budgets"]["arxiv"]["limit"] == 24


def test_arxiv_transient_failures_are_retry_risk_not_hard_source_risk(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")

    def fake_arxiv_search(*args, **kwargs):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr("papercompass.discovery.arxiv_search", fake_arxiv_search)

    result = sync_arxiv_discovery(
        workspace,
        topic={"topic_id": "latent", "min_year": 2024, "search_hints": ["latent reasoning"]},
        years=[2024],
        queries=['all:"latent reasoning"'],
        max_results=10,
        page_size=10,
        sleep_seconds=0,
        rate_limit_error_limit=1,
    )

    assert result["stopped_early"] is True
    coverage = json.loads((workspace / ".papercompass" / "manifests" / "source_coverage.json").read_text(encoding="utf-8"))
    entry = next(iter(coverage.values()))
    assert entry["execution_status"] == "failed"
    assert entry["coverage_risk"] == "medium"
    assert entry["transient_error"] is True
    assert entry["retry_recommended"] is True


def test_arxiv_recent_first_fetches_newer_years_first(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    calls = []

    def fake_arxiv_search(query, **kwargs):
        calls.append(query)
        return {"total": 0, "data": []}

    monkeypatch.setattr("papercompass.discovery.arxiv_search", fake_arxiv_search)

    result = sync_arxiv_discovery(
        workspace,
        topic={"topic_id": "latent", "min_year": 2022, "search_hints": ["latent reasoning"]},
        years=[2022, 2023, 2024],
        queries=['all:"latent reasoning"'],
        max_results=10,
        page_size=10,
        sleep_seconds=0,
        recent_first=True,
    )

    assert result["stopped_early"] is False
    fetched_years = [re.search(r"submittedDate:\[(\d{4})", query).group(1) for query in calls]
    assert fetched_years == ["2024", "2023", "2022"]


def test_arxiv_recent_year_count_caps_year_fanout(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    calls = []

    def fake_arxiv_search(query, **kwargs):
        calls.append((query, kwargs))
        return {"total": 0, "data": []}

    monkeypatch.setattr("papercompass.discovery.arxiv_search", fake_arxiv_search)

    result = sync_arxiv_discovery(
        workspace,
        topic={"topic_id": "latent", "min_year": 2022, "search_hints": ["latent reasoning"]},
        years=[2022, 2023, 2024, 2025],
        queries=['all:"latent reasoning"'],
        max_results=10,
        page_size=10,
        sleep_seconds=0,
        recent_first=True,
        recent_year_count=2,
        retry_attempts=1,
    )

    assert result["stopped_early"] is False
    fetched_years = [re.search(r"submittedDate:\[(\d{4})", query).group(1) for query, _ in calls]
    assert fetched_years == ["2025", "2024"]
    assert all(kwargs["retry_attempts"] == 1 for _, kwargs in calls)


def test_semantic_scholar_rate_limit_is_recoverable_and_breaks_remaining(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    calls = []

    def fake_http_get_json(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr("papercompass.discovery.http_get_json", fake_http_get_json)

    result = sync_semantic_scholar(
        workspace,
        topic={"topic_id": "slm", "min_year": 2022, "search_hints": ["small language model"]},
        years=[2022, 2023, 2024],
        queries=["small language model agent"],
        max_results=10,
        page_size=10,
        sleep_seconds=0,
        no_key_sleep_seconds=0,
        rate_limit_error_limit=1,
    )

    assert len(calls) == 1
    assert result["stopped_early"] is True
    coverage = json.loads((workspace / ".papercompass" / "manifests" / "source_coverage.json").read_text(encoding="utf-8"))
    assert len(coverage) == 1
    entry = next(iter(coverage.values()))
    assert entry["execution_status"] == "rate_limited"
    assert entry["coverage_risk"] == "low"
    assert entry["optional_source"] is True
    assert entry["optional_reason"] == "semanticscholar_no_api_key"
    assert entry["source_exhausted"] is None
    assert entry["rate_limit_breaker_triggered"] is True


def test_semantic_scholar_with_key_keeps_rate_limit_as_partial_risk(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")

    def fake_http_get_json(*args, **kwargs):
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr("papercompass.discovery.http_get_json", fake_http_get_json)

    sync_semantic_scholar(
        workspace,
        topic={"topic_id": "slm", "min_year": 2022, "search_hints": ["small language model"]},
        years=[2024],
        queries=["small language model agent"],
        api_key="test-key",
        max_results=10,
        page_size=10,
        sleep_seconds=0,
        no_key_sleep_seconds=0,
        rate_limit_error_limit=1,
    )

    coverage = json.loads((workspace / ".papercompass" / "manifests" / "source_coverage.json").read_text(encoding="utf-8"))
    entry = next(iter(coverage.values()))
    assert entry["execution_status"] == "rate_limited"
    assert entry["coverage_risk"] == "medium"
    assert entry["optional_source"] is False
    assert entry["authenticated"] is True


def test_semantic_scholar_no_key_403_is_optional_low_risk(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")

    def fake_http_get_json(*args, **kwargs):
        raise RuntimeError("HTTP Error 403: Forbidden")

    monkeypatch.setattr("papercompass.discovery.http_get_json", fake_http_get_json)

    sync_semantic_scholar(
        workspace,
        topic={"topic_id": "slm", "min_year": 2022, "search_hints": ["small language model"]},
        years=[2024],
        queries=["small language model agent"],
        max_results=10,
        page_size=10,
        sleep_seconds=0,
        no_key_sleep_seconds=0,
        rate_limit_error_limit=1,
    )

    coverage = json.loads((workspace / ".papercompass" / "manifests" / "source_coverage.json").read_text(encoding="utf-8"))
    entry = next(iter(coverage.values()))
    assert entry["execution_status"] == "failed"
    assert entry["coverage_risk"] == "low"
    assert entry["optional_source"] is True
    assert entry["optional_reason"] == "semanticscholar_no_api_key"
    assert entry["authenticated"] is False


def test_semantic_scholar_with_key_403_records_auth_problem(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")

    def fake_http_get_json(*args, **kwargs):
        raise RuntimeError("HTTP Error 403: Forbidden")

    monkeypatch.setattr("papercompass.discovery.http_get_json", fake_http_get_json)

    sync_semantic_scholar(
        workspace,
        topic={"topic_id": "slm", "min_year": 2022, "search_hints": ["small language model"]},
        years=[2024],
        queries=["small language model agent"],
        api_key="bad-key",
        max_results=10,
        page_size=10,
        sleep_seconds=0,
        no_key_sleep_seconds=0,
        rate_limit_error_limit=1,
    )

    coverage = json.loads((workspace / ".papercompass" / "manifests" / "source_coverage.json").read_text(encoding="utf-8"))
    entry = next(iter(coverage.values()))
    assert entry["execution_status"] == "failed"
    assert entry["coverage_risk"] == "high"
    assert entry["optional_source"] is False
    assert entry["authenticated"] is True
    assert entry["auth_status"] == "api_key_configured"
    assert entry["auth_problem"] == "semanticscholar_api_key_forbidden"
    assert "SEMANTIC_SCHOLAR_API_KEY" in entry["auth_hint"]


def test_semantic_scholar_caps_kept_candidates_per_run(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")

    def fake_http_get_json(*args, **kwargs):
        return {
            "data": [
                {
                    "paperId": f"p{idx}",
                    "title": f"Latent reasoning paper {idx}",
                    "abstract": "Latent reasoning in language models.",
                    "year": 2024,
                    "citationCount": idx,
                    "url": f"https://example.test/{idx}",
                }
                for idx in range(30)
            ]
        }

    monkeypatch.setattr("papercompass.discovery.http_get_json", fake_http_get_json)

    result = sync_semantic_scholar(
        workspace,
        topic={"topic_id": "latent", "min_year": 2022, "search_hints": ["latent reasoning"]},
        years=[2024],
        queries=["latent reasoning"],
        max_results=30,
        page_size=30,
        sorts=["citationCount:desc"],
        sleep_seconds=0,
        no_key_sleep_seconds=0,
        max_kept_per_run=7,
    )

    assert result["kept"] == 7
    coverage = json.loads((workspace / ".papercompass" / "manifests" / "source_coverage.json").read_text(encoding="utf-8"))
    entry = next(iter(coverage.values()))
    assert entry["kept_before_cap"] == 30
    assert entry["kept_count"] == 7
    assert entry["max_kept_per_run"] == 7
    raw_rows = [
        json.loads(line)
        for line in (workspace / entry["raw_output"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(raw_rows) == 7
    assert raw_rows[0]["raw"]["citation_count"] == 29
    assert raw_rows[-1]["raw"]["citation_count"] == 23


def test_europepmc_uses_next_cursor_for_followup_pages(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    requested_urls: list[str] = []

    def fake_http_get_json(url, *args, **kwargs):
        requested_urls.append(url)
        if "cursorMark=%2A" in url:
            return {
                "nextCursorMark": "NEXT",
                "resultList": {
                    "result": [
                        {
                            "title": "Machine learning in medicine",
                            "pubYear": "2024",
                            "pmid": "1",
                            "journalTitle": "Test",
                            "abstractText": "machine learning",
                        }
                    ]
                },
            }
        return {
            "nextCursorMark": "END",
            "resultList": {
                "result": [
                    {
                        "title": "Deep learning in medicine",
                        "pubYear": "2024",
                        "pmid": "2",
                        "journalTitle": "Test",
                        "abstractText": "machine learning",
                    }
                ]
            },
        }

    monkeypatch.setattr("papercompass.discovery.http_get_json", fake_http_get_json)

    result = sync_europepmc(
        workspace,
        topic={"topic_id": "bio", "min_year": 2024, "search_hints": ["machine learning"]},
        years=[2024],
        queries=["machine learning"],
        page_size=1,
        max_pages=2,
        sleep_seconds=0,
    )

    assert result["runs"] == 2
    assert result["kept"] == 2
    assert "cursorMark=%2A" in requested_urls[0]
    assert "cursorMark=NEXT" in requested_urls[1]

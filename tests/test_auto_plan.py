"""v3 plan render tests.

render_plan now consumes search_hints / search_keyword_text / judge_examples
from the brain output (no more strong_terms / named_methods / negative_terms).
"""

from __future__ import annotations

from pathlib import Path

from papercompass.auto.plan import (
    arxiv_queries_for_terms,
    canonical_short_terms,
    openalex_queries_for_terms,
    recall_terms_for_plan,
    render_plan,
    write_plan,
)
from papercompass.source_budget import planned_arxiv_remote_calls, recommended_arxiv_remote_calls


def _sample_plan() -> dict:
    return {
        "topic_id": "Speculative Decoding!",
        "name": "Speculative decoding for LLMs",
        "description": "Inference acceleration for LLMs.",
        "min_year": 2023,
        "search_hints": [
            "speculative decoding",
            "speculative sampling",
            "draft model verification",
            "Medusa decoding",
            "EAGLE speculative",
            "tree attention decoding",
        ],
        "search_keyword_text": (
            "Speculative decoding, speculative sampling, draft-and-verify, "
            "draft model speculative inference, EAGLE, Medusa, tree-of-drafts, "
            "lossless LLM acceleration."
        ),
        "judge_examples": {
            "in_scope": [
                {"title": "Fast inference from transformers via speculative decoding", "reason": "core paper"},
                {"title": "Medusa: simple LLM inference acceleration", "reason": "named anchor"},
                {"title": "EAGLE-2: speculative sampling on draft model", "reason": "named anchor"},
            ],
            "out_of_scope": [
                {"title": "Speculative execution branch prediction", "reason": "CPU architecture, not decoding"},
                {"title": "Medusa medical imaging", "reason": "name collision"},
                {"title": "GPT-4 web agent", "reason": "agent paper, not decoding"},
            ],
        },
        "seed_papers": [
            {"title": "Fast Inference from Transformers via Speculative Decoding", "year": 2023, "arxiv_id": "2211.17192", "why_seed": "core"},
            {"title": "Accelerating LLM Decoding with Speculative Sampling", "year": 2023, "arxiv_id": "2302.01318", "why_seed": "core"},
            {"title": "Medusa: Simple LLM Inference Acceleration", "year": 2024, "arxiv_id": "2401.10774", "why_seed": "anchor"},
        ],
    }


def test_render_plan_writes_v3_topic_yaml():
    topic, sources, seeds = render_plan(_sample_plan(), "Speculative decoding")
    assert topic["topic_id"] == "speculative-decoding"
    assert topic["direction_raw"] == "Speculative decoding"
    assert "search_hints" in topic
    assert len(topic["search_hints"]) >= 6
    assert "speculative decoding" in topic["search_hints"]
    assert topic["search_keyword_text"]
    assert "judge_examples" in topic
    assert len(topic["judge_examples"]["in_scope"]) >= 3
    assert len(topic["judge_examples"]["out_of_scope"]) >= 3
    # Removed keyword-rule fields must stay absent from v3 topic.yaml.
    for removed_field in (
        "strong_keywords", "negative_patterns", "named_patterns",
        "strict_patterns", "title_focus_patterns", "weak_keywords",
        "strong_keywords_semantics",
    ):
        assert removed_field not in topic, f"{removed_field} must not appear in v3 topic.yaml"


def test_render_plan_persists_original_query_verbatim():
    original = "帮我调研一下 2023 年之后的 speculative decoding，排除 CPU 分支预测。"
    topic, _, _ = render_plan(
        _sample_plan(),
        "Speculative decoding after 2023",
        original_query=original,
    )

    assert topic["direction_raw"] == "Speculative decoding after 2023"
    assert topic["original_query"] == original


def test_render_plan_infers_publication_scope_from_direction():
    topic, _, _ = render_plan(
        _sample_plan(),
        "2023年及以后的中文语法错误纠正顶会论文和较新的 arXiv 论文",
    )

    scope = topic["publication_scope"]
    assert scope["strict"] is True
    assert scope["include_preprints"] is True
    assert "ACL" in scope["preferred_venues"]
    assert "EMNLP" in scope["preferred_venues"]
    assert scope["venue_profile"] == "nlp_top_ai"
    assert "AAAI" in scope["preferred_venues"]


def test_render_plan_drops_unverified_brain_seeds():
    _, _, seeds = render_plan(_sample_plan(), "Speculative decoding")
    assert seeds == []


def test_render_plan_sources_use_search_hints():
    _, sources, _ = render_plan(_sample_plan(), "Speculative decoding")
    arxiv_q = sources["sources"]["arxiv"]["queries"]
    assert any("speculative decoding" in q.lower() for q in arxiv_q)
    assert any("all:speculative" in q.lower() and "all:decoding" in q.lower() for q in arxiv_q)
    oa_q = sources["discovery"]["openalex"]["queries"]
    oa_texts = [q["text"] if isinstance(q, dict) else q for q in oa_q]
    assert any("speculative decoding" in t.lower() for t in oa_texts)
    assert all((q.get("modes") == ["search"]) for q in oa_q if isinstance(q, dict) and q.get("strength") != "must_recall")
    assert "semanticscholar" not in sources["discovery"]["sources"]
    assert "crossref" in sources["discovery"]["sources"]
    assert "dblp" in sources["discovery"]["sources"]
    ss_q = sources["discovery"]["semanticscholar"]["queries"]
    assert any("speculative decoding" in q.lower() for q in ss_q)
    assert all('"' not in q for q in ss_q)


def test_render_plan_prioritizes_required_seed_title_queries():
    plan = _sample_plan()
    plan["seed_papers"][2]["paper_role"] = "background_anchor"
    for seed in plan["seed_papers"]:
        seed["verified"] = True
        seed["evidence"] = {
            "source": "openalex",
            "query": seed["title"],
            "source_item_id": f"https://openalex.org/{seed['arxiv_id']}",
            "source_url": f"https://example.org/{seed['arxiv_id']}",
            "match_type": "openalex_title_match",
        }
    topic, sources, seeds = render_plan(plan, "Speculative decoding")

    assert topic["topic_id"] == "speculative-decoding"
    assert seeds[2]["paper_role"] == "background_anchor"
    oa_q = sources["discovery"]["openalex"]["queries"]
    assert oa_q[0]["text"] == "Fast Inference from Transformers via Speculative Decoding"
    assert oa_q[0]["strength"] == "must_recall"
    assert oa_q[0]["modes"] == ["title"]
    assert oa_q[0]["page_size"] == 5
    assert oa_q[0]["paper_role"] == "core_method"
    assert all("search" not in (q.get("modes") or []) for q in oa_q if isinstance(q, dict) and q.get("strength") == "must_recall")
    assert any(q["paper_role"] == "background_anchor" for q in oa_q if isinstance(q, dict))


def test_render_plan_uses_discriminator_terms_as_bounded_secondary_queries():
    plan = _sample_plan() | {
        "discriminator_terms": ["function-calling agent", "MobileLLM"],
    }
    _, sources, _ = render_plan(plan, "Speculative decoding")
    oa_q = sources["discovery"]["openalex"]["queries"]
    oa_texts = [q["text"] if isinstance(q, dict) else q for q in oa_q]

    assert "function-calling agent" in oa_texts
    assert oa_texts.index("function-calling agent") > oa_texts.index("tree attention decoding")


def test_render_plan_handles_empty_inputs():
    topic, sources, seeds = render_plan({"topic_id": "x"}, "anything")
    assert topic["topic_id"] == "x"
    assert topic["search_hints"] == []
    assert topic["judge_examples"]["in_scope"] == []
    assert sources["discovery"]["sources"] == ["paperlists", "openalex", "crossref", "dblp", "arxiv"]
    assert sources["discovery"]["arxiv"]["budget_policy"] == "auto_floor"
    assert sources["discovery"]["arxiv"]["max_remote_calls"] == 24
    assert sources["discovery"]["arxiv"]["recent_year_count"] == 3
    assert sources["discovery"]["arxiv"]["retry_attempts"] == 1
    assert sources["discovery"]["arxiv"]["rate_limit_error_limit"] == 2
    assert sources["discovery"]["semanticscholar"]["max_results"] == 300
    assert sources["discovery"]["semanticscholar"]["max_kept_per_run"] == 25
    assert seeds == []


def test_render_plan_keyword_text_falls_back_to_hints_join():
    plan = {
        "topic_id": "x",
        "search_hints": ["alpha", "beta", "gamma"],
        "seed_papers": [{"title": str(i)} for i in range(3)],
    }
    topic, _, _ = render_plan(plan, "test")
    assert "alpha" in topic["search_keyword_text"]
    assert "beta" in topic["search_keyword_text"]


def test_recall_terms_expand_over_specific_hints_deterministically():
    terms = recall_terms_for_plan(
        [
            "implicit chain-of-thought reasoning",
            "latent reasoning language model",
            "compressed reasoning LLM",
        ],
        "pause tokens, coconut chain-of-continuous-thought, soft chain-of-thought",
        [],
    )
    assert "chain thought" in terms
    assert "latent reasoning" in terms
    assert "pause tokens" in terms
    assert "compressed chain thought" in terms
    queries = arxiv_queries_for_terms(terms)
    assert "all:implicit AND all:chain AND all:thought AND all:reasoning" in queries
    assert 'all:"latent reasoning language model"' in queries
    assert "all:compressed AND all:chain AND all:thought" in queries


def test_render_plan_persists_source_filter_terms_for_discovery_alignment():
    plan = _sample_plan() | {
        "search_hints": [
            "implicit chain-of-thought reasoning",
            "latent reasoning language model",
        ],
        "search_keyword_text": "latent space reasoning, chain-of-continuous-thought",
    }

    topic, sources, _ = render_plan(plan, "Implicit CoT and latent reasoning")
    oa_texts = [
        q["text"] if isinstance(q, dict) else q
        for q in sources["discovery"]["openalex"]["queries"]
    ]

    assert "latent reasoning" in topic["source_filter_terms"]
    assert "latent space reasoning" in topic["source_filter_terms"]
    assert "latent reasoning" in oa_texts


def test_latent_agent_cross_axis_terms_are_added_generically():
    plan = {
        "topic_id": "implicit-cot-agents",
        "name": "Implicit CoT for agents",
        "description": "Latent internal reasoning for LLM agents.",
        "min_year": 2025,
        "search_hints": [
            "implicit chain-of-thought reasoning",
            "latent reasoning agent",
            "hidden state planning",
            "continuous thought agent",
            "internal monologue agent",
            "reasoning without token output",
        ],
        "search_keyword_text": (
            "Implicit chain-of-thought, latent reasoning, tool use, web agents, "
            "embodied agents, action selection, agent memory."
        ),
        "discriminator_terms": [
            "latent reasoning",
            "implicit chain-of-thought",
            "latent state planning",
        ],
        "judge_examples": {"in_scope": [], "out_of_scope": []},
    }

    topic, sources, _ = render_plan(plan, "Implicit chain-of-thought and latent reasoning for LLM agents")
    oa_texts = [
        q["text"] if isinstance(q, dict) else q
        for q in sources["discovery"]["openalex"]["queries"]
    ]
    arxiv_queries = sources["discovery"]["arxiv"]["queries"]

    assert "latent communication" in topic["source_filter_terms"]
    assert "kv cache communication" in topic["source_filter_terms"]
    assert "latent communication" in oa_texts
    assert any("latent" in query.lower() and "communication" in query.lower() for query in arxiv_queries)


def test_out_of_scope_agent_mention_does_not_inject_multi_agent_bridge_terms():
    # "agent" appears ONLY inside the out-of-scope clause; axis detection must
    # not treat this in-context-learning topic as an agent topic and inject
    # unrelated multi-agent bridge queries (regression for the ICL build).
    direction = (
        "In-context learning in large language models: how LLMs learn from "
        "demonstrations without parameter updates, including implicit gradient "
        "descent and task vectors. Out-of-scope: in-context reinforcement "
        "learning or agent online adaptation."
    )
    terms = recall_terms_for_plan(
        ["in-context learning demonstrations", "task vectors function vectors"],
        "in-context learning, few-shot demonstrations, induction heads.",
        ["in-context learning", "task vector"],
        direction=direction,
    )
    joined = " ".join(terms).lower()
    assert "latent communication" not in joined
    assert "multi agent" not in joined
    assert "kv cache communication" not in joined


def test_generic_meta_terms_are_not_standalone_recall_anchors():
    # "scaling laws" / "bayesian inference" must not become bare recall anchors
    # (they flooded the ICL build), while topic-specific terms survive.
    terms = recall_terms_for_plan(
        [
            "scaling laws emergence of in-context learning ability",
            "in-context learning demonstration selection",
        ],
        "In-context learning, scaling laws, Bayesian inference interpretation of in-context learning.",
        ["in-context learning", "demonstration selection"],
        direction="In-context learning in large language models.",
    )
    low = [t.lower() for t in terms]
    assert "scaling laws" not in low
    assert "bayesian inference" not in low
    assert any("in-context learning" in t or "in context learning" in t for t in low)


def test_query_generation_prioritizes_core_recall_terms_before_cap():
    terms = [f"generic method family {idx}" for idx in range(20)] + [
        "latent reasoning language model",
        "faithfulness internal reasoning",
    ]

    arxiv_queries = arxiv_queries_for_terms(terms, cap=3)
    assert "all:latent AND all:reasoning" in arxiv_queries[0]
    assert any("faithfulness" in query.lower() for query in arxiv_queries)

    oa_queries = openalex_queries_for_terms(terms, cap=2)
    assert oa_queries[0]["text"] == "latent reasoning language model"
    assert oa_queries[1]["text"] == "faithfulness internal reasoning"


def test_recall_terms_do_not_turn_seed_titles_into_remote_queries():
    terms = recall_terms_for_plan(
        ["latent reasoning language model"],
        "",
        [],
        [
            "Plan-and-Solve Prompting: Improving Zero-Shot Chain-of-Thought Reasoning by Large Language Models",
        ],
    )
    assert all("plan" not in t.lower() and "solve" not in t.lower() for t in terms)


def test_recall_terms_add_stable_aliases_without_keyword_ngram_noise():
    terms = recall_terms_for_plan(
        ["faithfulness of chain-of-thought", "soft token reasoning"],
        "Implicit chain-of-thought and latent reasoning in large language models involve hidden-state reasoning.",
        [],
    )
    assert "unfaithful chain thought" in terms
    assert "softcot" in terms
    assert "involve hidden" not in terms


def test_render_plan_keeps_soft_token_hint_reachable_under_source_caps():
    plan = {
        "topic_id": "implicit-cot",
        "search_hints": [
            "implicit chain-of-thought",
            "latent reasoning",
            "hidden chain-of-thought",
            "continuous chain-of-thought",
            "soft token reasoning",
            "compressed reasoning",
            "internalized reasoning",
            "recurrent reasoning",
            "faithfulness of chain-of-thought",
            "Coconut chain-of-thought",
        ],
        "search_keyword_text": (
            "Implicit chain-of-thought reasoning, latent reasoning, "
            "soft-token reasoning, compressed internal reasoning."
        ),
    }

    _, sources, _ = render_plan(plan, "Implicit CoT")
    query_blob = "\n".join(
        [str(q) for q in sources["sources"]["arxiv"]["queries"]]
        + [q["text"] for q in sources["discovery"]["openalex"]["queries"] if q.get("strength") != "must_recall"]
        + list(sources["discovery"]["semanticscholar"]["queries"])
    ).lower()

    assert "soft token reasoning" in query_blob


def test_render_plan_keeps_soft_token_hint_reachable_with_hidden_alias_pressure():
    plan = {
        "topic_id": "implicit-cot",
        "search_hints": [
            "implicit chain of thought reasoning",
            "latent reasoning language model",
            "hidden state reasoning",
            "continuous thought LLM",
            "soft token reasoning",
            "compressed reasoning",
            "internalized reasoning",
            "recurrent test time reasoning",
            "chain of thought faithfulness",
            "mechanistic interpretability chain of thought",
        ],
        "search_keyword_text": (
            "Implicit chain-of-thought reasoning, latent reasoning in LLMs, "
            "hidden-state reasoning, continuous-space reasoning, soft-token "
            "reasoning, compressed reasoning."
        ),
    }

    _, sources, _ = render_plan(plan, "Implicit CoT")
    oa_texts = [
        q["text"] if isinstance(q, dict) else q
        for q in sources["discovery"]["openalex"]["queries"]
    ]
    ss_texts = list(sources["discovery"]["semanticscholar"]["queries"])

    assert "soft token reasoning" in oa_texts
    assert "soft token reasoning" in ss_texts


def test_render_plan_protects_all_search_hints_before_alias_expansion():
    hints = [
        "implicit chain of thought",
        "latent reasoning",
        "hidden state reasoning",
        "continuous reasoning",
        "soft token reasoning",
        "compressed reasoning",
        "internalized reasoning",
        "recurrent reasoning",
        "CoT faithfulness",
        "reasoning without CoT",
        "internal reasoning chain",
    ]
    plan = {
        "topic_id": "implicit-cot",
        "search_hints": hints,
        "search_keyword_text": "latent reasoning, hidden-state reasoning, soft-token reasoning",
    }

    topic, sources, _ = render_plan(plan, "Implicit CoT")
    oa_texts = [
        q["text"] if isinstance(q, dict) else q
        for q in sources["discovery"]["openalex"]["queries"]
    ]
    ss_texts = list(sources["discovery"]["semanticscholar"]["queries"])

    for hint in hints:
        cleaned = hint.replace("CoT", "cot")
        assert cleaned.lower() in {term.lower() for term in topic["source_filter_terms"]}
        assert cleaned.lower() in {term.lower() for term in oa_texts}
        assert cleaned.lower() in {term.lower() for term in ss_texts}


def test_render_plan_arxiv_query_count_matches_remote_budget():
    _, sources, _ = render_plan(_sample_plan(), "Speculative decoding")
    arxiv_cfg = sources["discovery"]["arxiv"]
    planned = planned_arxiv_remote_calls(
        len(arxiv_cfg["queries"]),
        recent_year_count=arxiv_cfg["recent_year_count"],
        max_results=arxiv_cfg["max_results"],
        page_size=arxiv_cfg["page_size"],
    )
    assert planned <= arxiv_cfg["max_remote_calls"]
    assert arxiv_cfg["max_remote_calls"] == recommended_arxiv_remote_calls(
        len(arxiv_cfg["queries"]),
        recent_year_count=arxiv_cfg["recent_year_count"],
        max_results=arxiv_cfg["max_results"],
        page_size=arxiv_cfg["page_size"],
    )


def test_write_plan_persists_yaml_and_seeds(tmp_path: Path):
    plan = _sample_plan()
    plan["seed_papers"][0]["verified"] = True
    plan["seed_papers"][0]["required"] = True
    plan["seed_papers"][0]["paper_role"] = "background_anchor"
    plan["seed_papers"][0]["evidence"] = {
        "source": "openalex",
        "query": plan["seed_papers"][0]["title"],
        "source_item_id": "https://openalex.org/W1",
        "source_url": "https://example.org/W1",
        "match_type": "openalex_title_search",
    }
    topic, sources, seeds = render_plan(plan, "Speculative decoding")
    ws = tmp_path / "ws"
    (ws / ".papercompass" / "plans").mkdir(parents=True)
    write_plan(ws, topic, sources, seeds)
    assert (ws / "topic.yaml").exists()
    assert (ws / "sources.yaml").exists()
    anchors_text = (ws / ".papercompass" / "plans" / "anchors.jsonl").read_text(encoding="utf-8")
    legacy_text = (ws / ".papercompass" / "plans" / "seed_papers.jsonl").read_text(encoding="utf-8")
    assert "Speculative Decoding" in anchors_text
    assert anchors_text == legacy_text


def test_canonical_short_terms_extracts_2word_core_for_long_phrases():
    """canonical_short_terms helper retained — used by callers that want
    canonical 2-word cores from long brain phrases."""
    out = canonical_short_terms([
        "speculative decoding large language models",
        "draft and verify",
        "tree-based speculative decoding",
    ])
    assert "speculative decoding" in out
    assert "draft and" not in out  # stopword filter active

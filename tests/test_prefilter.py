from papercompass.auto.prefilter import PrefilterDecision, PrefilterPipeline, PrefilterPolicy, prefilter_review_indices, summarize_prefilter


def test_prefilter_scores_topic_hits_and_negative_signals() -> None:
    topic = {
        "min_year": 2022,
        "search_hints": ["speculative decoding"],
        "discriminator_terms": ["draft verifier"],
        "search_keyword_text": "LLM assisted generation draft model verification",
        "judge_examples": {
            "out_of_scope": [
                {"title": "CPU speculative execution", "reason": "hardware branch prediction"}
            ]
        },
    }
    prefilter = PrefilterPipeline(topic)

    strong = prefilter.evaluate({
        "title": "Speculative Decoding with Draft Verifier Models",
        "year": 2024,
        "abstract": "LLM assisted generation uses a draft model and verifier.",
        "sources": ["openalex", "arxiv"],
        "max_citation": 42,
    })
    weak_negative = prefilter.evaluate({
        "title": "CPU Branch Prediction for Speculative Execution",
        "year": 2024,
        "abstract": "Hardware branch prediction and speculative execution.",
        "sources": ["dblp"],
    })

    assert strong.action in {"strong", "review"}
    assert strong.score > weak_negative.score
    assert "speculative decoding" in strong.topic_hits
    assert weak_negative.negative_hits
    assert "negative_signal_without_title_hit" in weak_negative.reasons


def test_prefilter_summary_counts_actions_and_reasons() -> None:
    topic = {"search_hints": ["alpha beta"], "search_keyword_text": "alpha beta"}
    prefilter = PrefilterPipeline(topic)
    decisions = [
        prefilter.evaluate({"title": "Alpha Beta Paper", "abstract": "alpha beta", "year": 2024}),
        prefilter.evaluate({"title": "Unrelated", "abstract": "", "year": 2024}),
    ]

    summary = summarize_prefilter(decisions)

    assert summary["candidate_count"] == 2
    assert summary["topic_hit_count"] == 1
    assert summary["top_reasons"]["low_deterministic_relevance"] == 1


def test_prefilter_partition_filters_low_candidates_and_keeps_protected() -> None:
    topic = {
        "min_year": 2022,
        "search_hints": ["alpha beta"],
        "search_keyword_text": "alpha beta methods",
    }
    candidates = [
        {"candidate_key": "seed", "title": "Old protected", "year": 2020, "abstract": ""},
        {"candidate_key": "good", "title": "Alpha Beta Methods", "year": 2024, "abstract": "alpha beta methods"},
        {"candidate_key": "bad", "title": "Unrelated", "year": 2024, "abstract": "no matching signal"},
    ]
    prefilter = PrefilterPipeline(topic)

    decisions, review_indices = prefilter.partition(candidates, protected_keys={"seed"})
    summary = summarize_prefilter(decisions, review_indices=review_indices)

    assert decisions[0].action == "protected"
    assert decisions[2].action == "reject"
    assert 0 in review_indices
    assert 2 not in review_indices
    assert summary["llm_review_count"] < summary["candidate_count"]


def test_prefilter_downgrades_missing_abstract_strong_candidate() -> None:
    topic = {
        "search_hints": ["speculative decoding"],
        "discriminator_terms": ["draft verifier"],
        "search_keyword_text": "speculative decoding draft verifier",
    }
    prefilter = PrefilterPipeline(topic)
    decision = prefilter.evaluate({
        "title": "Speculative Decoding with Draft Verifier",
        "year": 2025,
        "abstract": "",
        "sources": ["openalex", "arxiv", "semanticscholar"],
        "citation_count": 120,
    })

    assert "missing_abstract" in decision.reasons
    assert decision.action != "strong"
    assert "strong_downgraded_missing_abstract" in decision.reasons


def test_prefilter_generic_tokens_do_not_count_as_phrase_hits() -> None:
    topic = {"search_hints": ["model"], "search_keyword_text": "model"}
    prefilter = PrefilterPipeline(topic)

    decision = prefilter.evaluate({
        "title": "A Better Model",
        "year": 2024,
        "abstract": "This model is unrelated.",
    })

    assert decision.topic_hits == []
    assert "no_topic_signal" in decision.reasons
    assert decision.features["generic_token_count"] >= 1


def test_prefilter_strong_audit_uses_score_strata() -> None:
    decisions = [
        PrefilterDecision(
            action="strong",
            score=float(100 - idx),
            reasons=[],
            topic_hits=[],
            negative_hits=[],
            candidate_key=f"c{idx}",
        )
        for idx in range(9)
    ]

    selected = prefilter_review_indices(
        decisions,
        PrefilterPolicy(
            strong_audit_min=3,
            strong_audit_rate=0.34,
        ),
    )

    assert len(selected) == 4
    assert any(index >= 6 for index in selected)

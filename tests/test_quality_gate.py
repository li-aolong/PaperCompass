from papercompass.auto.quality_gate import diagnose_review_queue


def test_review_queue_gate_stops_generic_dominated_large_queue() -> None:
    pending = [
        {
            "title": f"Paper {idx}",
            "topic_signals": {"topic_signal_hits": ["test-time"]},
            "sources": ["paperlists"],
        }
        for idx in range(1200)
    ]

    diagnosis = diagnose_review_queue(pending, batch_size=25, max_batches=20)

    assert diagnosis["status"] == "needs_rule_repair"
    assert diagnosis["should_stop"] is True
    assert "weak_queue_too_large" in diagnosis["reason_codes"]
    assert "generic_anchor_dominates" in diagnosis["reason_codes"]


def test_review_queue_gate_distinguishes_budget_only_overflow() -> None:
    pending = [
        {
            "title": f"Paper {idx}",
            "topic_signals": {"topic_signal_hits": ["implicit chain of thought"]},
            "sources": ["arxiv"],
        }
        for idx in range(525)
    ]

    diagnosis = diagnose_review_queue(pending, batch_size=25, max_batches=20)

    assert diagnosis["status"] == "partial_due_to_budget"
    assert diagnosis["reason_codes"] == ["weak_batches_over_budget"]
    assert diagnosis["uncovered_count"] == 25


def test_review_queue_gate_treats_latent_space_as_generic_anchor() -> None:
    pending = [
        {
            "title": f"Latent diffusion paper {idx}",
            "topic_signals": {"topic_signal_hits": ["latent space"]},
            "sources": ["paperlists"],
        }
        for idx in range(120)
    ]

    diagnosis = diagnose_review_queue(pending, batch_size=25, max_batches=20)

    assert diagnosis["status"] == "needs_rule_repair"
    assert "generic_anchor_dominates" in diagnosis["reason_codes"]
    assert diagnosis["generic_only_count"] == 120

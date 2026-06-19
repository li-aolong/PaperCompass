import json

from papercompass.candidate_review import applied_decisions_path
from papercompass.config import init_workspace
from papercompass.qa import build_quality_report, query_coverage_report, refresh_final_summary_from_qa


def _seed_evidence(title: str) -> dict:
    return {
        "source": "openalex",
        "query": title,
        "source_item_id": f"https://openalex.org/{title}",
        "source_url": f"https://example.org/{title}",
        "match_type": "openalex_title_match",
    }


def test_quality_report_detects_raw_pollution_and_catalog_mismatch(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    raw_path = workspace / ".raw" / "arxiv" / "query" / "paper.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps({
        "source_name": "arxiv",
        "source_type": "arxiv",
        "raw": {
            "title": "A Paper",
            "year": 2024,
            "tags": ["confidence:weak", "review:weak_topic_signal"],
        },
    }) + "\n", encoding="utf-8")
    (workspace / "data" / "papers.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")

    report = build_quality_report(workspace)

    assert report["raw_pollution"]["polluted_row_count"] == 1
    assert "raw_derived_tag_pollution" in report["warnings"]
    assert report["catalog"]["count_matches"] is False
    assert "catalog_count_mismatch" in report["critical"]


def test_quality_report_separates_medium_and_hard_source_coverage(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "data" / "papers.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    coverage_path = workspace / ".papercompass" / "manifests" / "source_coverage.json"
    coverage_path.parent.mkdir(parents=True, exist_ok=True)
    coverage_path.write_text(json.dumps({
        "paperlists/acl/2026": {
            "source": "paperlists",
            "execution_status": "source_missing",
            "coverage_risk": "medium",
            "source_exhausted": None,
            "errors": ["HTTP 404: Not Found"],
        },
        "openalex/query": {
            "source": "openalex",
            "execution_status": "cache_hit",
            "coverage_risk": "medium",
            "source_exhausted": False,
            "budget_complete": False,
            "errors": [],
        },
    }), encoding="utf-8")

    report = build_quality_report(workspace)

    assert report["source_coverage"]["problem_count"] == 0
    assert report["source_coverage"]["medium_problem_count"] == 2
    assert "source_coverage_partial" in report["warnings"]
    assert "source_coverage_has_risk" not in report["warnings"]

    coverage_path.write_text(json.dumps({
        "openalex/capped-query": {
            "source": "openalex",
            "execution_status": "cache_hit",
            "coverage_risk": "low",
            "source_exhausted": False,
            "budget_complete": True,
            "errors": [],
        },
    }), encoding="utf-8")
    report = build_quality_report(workspace)

    assert report["source_coverage"]["medium_problem_count"] == 0
    assert "source_coverage_partial" not in report["warnings"]

    coverage_path.write_text(json.dumps({
        "paperlists/acl/2026": {
            "source": "paperlists",
            "execution_status": "source_missing",
            "coverage_risk": "low",
            "source_exhausted": None,
            "budget_complete": True,
            "errors": ["HTTP 404: Not Found"],
        },
    }), encoding="utf-8")
    report = build_quality_report(workspace)

    assert report["source_coverage"]["medium_problem_count"] == 0
    assert "source_coverage_partial" not in report["warnings"]

    coverage_path.write_text(json.dumps({
        "semanticscholar/rate-limit": {
            "source": "semanticscholar",
            "execution_status": "rate_limited",
            "coverage_risk": "medium",
            "source_exhausted": None,
            "errors": ["HTTP Error 429"],
        }
    }), encoding="utf-8")
    report = build_quality_report(workspace)

    assert report["source_coverage"]["problem_count"] == 0
    assert report["source_coverage"]["medium_problem_count"] == 1
    assert "source_coverage_partial" in report["warnings"]
    assert "source_coverage_has_risk" not in report["warnings"]

    coverage_path.write_text(json.dumps({
        "semanticscholar/no-key-403": {
            "source": "semanticscholar",
            "optional_source": True,
            "optional_reason": "semanticscholar_no_api_key",
            "execution_status": "failed",
            "coverage_risk": "high",
            "source_exhausted": None,
            "budget_complete": False,
            "errors": ["HTTP Error 403: Forbidden"],
        }
    }), encoding="utf-8")
    report = build_quality_report(workspace)

    assert report["source_coverage"]["problem_count"] == 0
    assert report["source_coverage"]["medium_problem_count"] == 0
    assert report["source_coverage"]["optional_problem_count"] == 1
    assert "source_coverage_partial" not in report["warnings"]
    assert "source_coverage_has_risk" not in report["warnings"]

    coverage_path.write_text(json.dumps({
        "arxiv/rate-limit": {
            "source": "arxiv",
            "execution_status": "failed",
            "coverage_risk": "high",
            "source_exhausted": False,
            "errors": ["HTTP Error 429"],
        }
    }), encoding="utf-8")
    report = build_quality_report(workspace)

    assert report["source_coverage"]["problem_count"] == 1
    assert "source_coverage_has_risk" in report["warnings"]


def test_quality_report_refreshes_stale_coverage_manifest(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    papers = [{"title": "A", "year": 2024}, {"title": "B", "year": 2025}]
    (workspace / "data" / "papers.json").write_text(json.dumps(papers), encoding="utf-8")
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "catalog").mkdir(exist_ok=True)
    (workspace / "catalog" / "manifest.json").write_text(
        json.dumps({"paper_count": 2}), encoding="utf-8"
    )
    coverage_report_path = workspace / ".papercompass" / "manifests" / "coverage_report.json"
    coverage_report_path.parent.mkdir(parents=True, exist_ok=True)
    coverage_report_path.write_text(json.dumps({"paper_count": 1}), encoding="utf-8")

    report = build_quality_report(workspace)
    refreshed = json.loads(coverage_report_path.read_text(encoding="utf-8"))

    assert refreshed["paper_count"] == 2
    assert report["coverage_manifest"]["refresh_status"] == "refreshed"
    assert report["coverage_manifest"]["count_matches"] is True
    assert "coverage_report_count_mismatch" not in report["critical"]


def test_quality_report_flags_semantic_scholar_auth_problem(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "data" / "papers.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "catalog").mkdir(exist_ok=True)
    (workspace / "catalog" / "manifest.json").write_text(
        json.dumps({"paper_count": 0}), encoding="utf-8"
    )
    coverage_path = workspace / ".papercompass" / "manifests" / "source_coverage.json"
    coverage_path.parent.mkdir(parents=True, exist_ok=True)
    coverage_path.write_text(json.dumps({
        "semanticscholar/key-403": {
            "source": "semanticscholar",
            "execution_status": "failed",
            "coverage_risk": "high",
            "authenticated": True,
            "auth_status": "api_key_configured",
            "auth_problem": "semanticscholar_api_key_forbidden",
            "auth_hint": "check key",
            "errors": ["HTTP Error 403: Forbidden"],
        }
    }), encoding="utf-8")

    report = build_quality_report(workspace)

    assert report["source_coverage"]["auth_problem_count"] == 1
    assert report["source_coverage"]["auth_problem_examples"][0]["auth_problem"] == "semanticscholar_api_key_forbidden"
    assert "source_auth_failed_optional" in report["warnings"]


def test_quality_report_infers_old_semantic_scholar_403_auth_problem(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "data" / "papers.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "catalog").mkdir(exist_ok=True)
    (workspace / "catalog" / "manifest.json").write_text(
        json.dumps({"paper_count": 0}), encoding="utf-8"
    )
    coverage_path = workspace / ".papercompass" / "manifests" / "source_coverage.json"
    coverage_path.parent.mkdir(parents=True, exist_ok=True)
    coverage_path.write_text(json.dumps({
        "semanticscholar/old-key-403": {
            "source": "semanticscholar",
            "execution_status": "failed",
            "coverage_risk": "high",
            "authenticated": True,
            "errors": ["HTTP Error 403: Forbidden"],
        }
    }), encoding="utf-8")

    report = build_quality_report(workspace)

    assert report["source_coverage"]["auth_problem_count"] == 1
    assert report["source_coverage"]["auth_problem_examples"][0]["auth_problem"] == "semanticscholar_api_key_forbidden"
    assert "source_auth_failed_optional" in report["warnings"]


def test_quality_report_flags_publication_scope_violations(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "topic.yaml").write_text(
        """
topic_id: topic
publication_scope:
  policy: preferred_venues_or_preprints
  strict: true
  preferred_venues:
    - ACL
  include_preprints: true
search_hints:
  - chinese grammatical error correction
""".strip(),
        encoding="utf-8",
    )
    papers = [
        {"title": "ACL Paper", "year": 2025, "venue": "ACL"},
        {"title": "arXiv Paper", "year": 2026, "venue": "arXiv", "ids": {"arxiv": "2601.1"}},
        {"title": "Journal Paper", "year": 2024, "venue": "Journal of NLP"},
    ]
    (workspace / "data" / "papers.json").write_text(json.dumps(papers), encoding="utf-8")
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "catalog").mkdir(exist_ok=True)
    (workspace / "catalog" / "manifest.json").write_text(
        json.dumps({"paper_count": 3}), encoding="utf-8"
    )

    report = build_quality_report(workspace)

    assert report["publication_scope"]["violation_count"] == 1
    assert report["publication_scope"]["violation_by_venue"] == [["Journal of NLP", 1]]
    assert report["publication_scope"]["violation_examples"][0]["title"] == "Journal Paper"
    violations_path = workspace / report["publication_scope"]["violations_path"]
    assert violations_path.exists()
    rows = [json.loads(line) for line in violations_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["title"] == "Journal Paper"
    assert "publication_scope_violations" in report["warnings"]


def test_quality_report_flags_underpowered_recall_pool(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    papers = [{"title": f"Paper {i}", "year": 2024} for i in range(10)]
    (workspace / "data" / "papers.json").write_text(json.dumps(papers), encoding="utf-8")
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "catalog").mkdir(exist_ok=True)
    (workspace / "catalog" / "manifest.json").write_text(
        json.dumps({"paper_count": 10}), encoding="utf-8"
    )
    raw_path = workspace / ".raw" / "manual" / "small.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        "\n".join(
            json.dumps({"raw": {"title": f"Raw {i}", "year": 2024}})
            for i in range(20)
        )
        + "\n",
        encoding="utf-8",
    )
    reviews = workspace / ".papercompass" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    queue = reviews / "weak_candidates_20260101_000000.json"
    queue_items = [
        {"candidate_key": f"k{i}", "title": f"Candidate {i}", "year": 2024}
        for i in range(20)
    ]
    queue.write_text(
        json.dumps({"run_id": "20260101_000000", "review_candidates": queue_items}),
        encoding="utf-8",
    )
    decisions = reviews / "review_decisions_20260101_000000.jsonl"
    decisions.write_text(
        "\n".join(
            json.dumps({
                "candidate_key": f"k{i}",
                "title": f"Candidate {i}",
                "year": 2024,
                "decision": "accept" if i < 10 else "reject",
                "action": "add_to_main" if i < 10 else "keep_pending",
                "reason": "test",
            })
            for i in range(20)
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_quality_report(workspace)

    assert report["recall_pool"]["underpowered"] is True
    assert report["recall_pool"]["review_queue_count"] == 20
    assert report["recall_pool"]["raw_candidate_count"] == 20
    assert "recall_pool_underpowered" in report["warnings"]


def test_quality_report_flags_no_brain_verdict_violations(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "data" / "papers.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "catalog").mkdir(exist_ok=True)
    (workspace / "catalog" / "manifest.json").write_text(
        json.dumps({"paper_count": 0}), encoding="utf-8"
    )
    reviews = workspace / ".papercompass" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    (reviews / "score_decisions_20260101_000000.jsonl").write_text(
        "\n".join([
            json.dumps({
                "candidate_key": "a",
                "title": "A",
                "verdict": "boundary",
                "brain_score": None,
                "policy": "fused",
                "score": 55,
            }),
            json.dumps({
                "candidate_key": "b",
                "title": "B",
                "verdict": "out_of_scope",
                "brain_score": None,
                "policy": "no_brain_conservative_drop",
                "score": 80,
            }),
        ])
        + "\n",
        encoding="utf-8",
    )

    report = build_quality_report(workspace)

    assert report["score_decisions"]["no_brain_verdict_violation_count"] == 1
    assert "no_brain_boundary_scores" in report["warnings"]


def test_quality_report_flags_role_placement_violations(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "data" / "papers.json").write_text(
        json.dumps([
            {"title": "Core Paper", "year": 2024, "paper_role": "core_method"},
            {"title": "Negative In Main", "year": 2024, "paper_role": "boundary_negative"},
        ]),
        encoding="utf-8",
    )
    (workspace / "data" / "anchor_papers.json").write_text(
        json.dumps([{"title": "Core In Anchors", "year": 2024, "paper_role": "core_method"}]),
        encoding="utf-8",
    )
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "catalog").mkdir(exist_ok=True)
    (workspace / "catalog" / "manifest.json").write_text(
        json.dumps({"paper_count": 2}), encoding="utf-8"
    )

    report = build_quality_report(workspace)

    assert report["role_placement"]["main_invalid_count"] == 1
    assert report["role_placement"]["anchor_invalid_count"] == 1
    assert "main_role_placement_violation" in report["critical"]
    assert "anchor_role_placement_violation" in report["warnings"]


def test_quality_report_flags_unresolved_pending_and_negative_citations(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "data" / "papers.json").write_text(
        json.dumps([{"title": "A", "year": 2024, "citation_count": -1}]),
        encoding="utf-8",
    )
    (workspace / "data" / "pending_review_candidates.json").write_text(
        json.dumps([{"title": "Pending", "year": 2024}]),
        encoding="utf-8",
    )
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "catalog").mkdir(exist_ok=True)
    (workspace / "catalog" / "manifest.json").write_text(
        json.dumps({"paper_count": 1}), encoding="utf-8"
    )

    report = build_quality_report(workspace)

    assert report["metadata"]["gaps"]["negative_citation"] == 1
    assert "pending_review_unresolved" in report["warnings"]
    assert "metadata_negative_citations" in report["warnings"]


def test_quality_report_flags_stale_applied_review_decisions(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "data" / "papers.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "anchor_papers.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "catalog").mkdir(exist_ok=True)
    (workspace / "catalog" / "manifest.json").write_text(
        json.dumps({"paper_count": 0}), encoding="utf-8"
    )
    applied = applied_decisions_path(workspace)
    applied.parent.mkdir(parents=True, exist_ok=True)
    applied.write_text(
        json.dumps({
            "candidate_key": "title:a:2024",
            "title": "A",
            "year": 2024,
            "decision": "accept",
            "decision_context_hash": "old-context",
        })
        + "\n",
        encoding="utf-8",
    )

    report = build_quality_report(workspace)

    applied_report = report["applied_review_decisions"]
    assert applied_report["status"] == "stale_context"
    assert applied_report["decision_count"] == 1
    assert applied_report["matching_context_count"] == 0
    assert applied_report["stale_context_count"] == 1
    assert "applied_review_decisions_stale" in report["warnings"]


def test_refresh_final_summary_from_qa_updates_counts_and_portable_paths(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    summary_path = workspace / ".papercompass" / "auto" / "final_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps({
            "workspace": str(workspace),
            "status": "passed_authoritative",
            "deliverable_status": "passed_authoritative",
            "safe_for_default_llm_retrieval": True,
            "counts": {"papers": 9, "anchors": 0, "pending": 0, "rejected": 0},
            "quality": {"qa_status": "passed", "critical": [], "warnings": []},
            "artifacts": {
                "state": str(workspace / ".papercompass" / "auto" / "state.json"),
                "main_library": str(workspace / "data" / "papers.jsonl"),
            },
        }),
        encoding="utf-8",
    )
    report = {
        "status": "warning",
        "critical": [],
        "warnings": ["pending_review_unresolved"],
        "counts": {"papers": 1, "anchors": 2, "pending": 3, "rejected": 4},
        "manifest": ".papercompass/manifests/quality_gates_test.json",
        "markdown": ".papercompass/reports/final_audit_test.md",
    }

    result = refresh_final_summary_from_qa(workspace, report)
    refreshed = json.loads(summary_path.read_text(encoding="utf-8"))

    assert result["refreshed"] is True
    assert refreshed["counts"] == {"papers": 1, "anchors": 2, "pending": 3, "rejected": 4}
    assert refreshed["qa_status"] == "warning"
    assert refreshed["deliverable_status"] == "usable_with_caveats"
    assert refreshed["safe_for_default_llm_retrieval"] is False
    assert refreshed["artifacts"]["main_library"] == "data/papers.jsonl"
    assert str(workspace) not in json.dumps(refreshed)


def test_quality_report_splits_core_and_anchor_seed_missing(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "data" / "papers.json").write_text(
        json.dumps([{"title": "Core Paper", "year": 2024, "paper_role": "core_method"}]),
        encoding="utf-8",
    )
    (workspace / "data" / "anchor_papers.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    seeds = workspace / ".papercompass" / "plans" / "seed_papers.jsonl"
    seeds.parent.mkdir(parents=True, exist_ok=True)
    seeds.write_text(
        "\n".join([
            json.dumps({"title": "Core Paper", "paper_role": "core_method", "required": True, "verified": True, "evidence": _seed_evidence("Core Paper")}),
            json.dumps({"title": "Missing Anchor", "paper_role": "background_anchor", "required": True, "verified": True, "evidence": _seed_evidence("Missing Anchor")}),
            json.dumps({"title": "Contrast Paper", "paper_role": "boundary_negative", "required": False}),
        ])
        + "\n",
        encoding="utf-8",
    )
    (workspace / "catalog").mkdir(exist_ok=True)
    (workspace / "catalog" / "manifest.json").write_text(
        json.dumps({"paper_count": 1}), encoding="utf-8"
    )

    report = build_quality_report(workspace)

    seed = report["seed_coverage"]
    assert seed["missing_count"] == 1
    assert seed["core_missing_count"] == 0
    assert seed["anchor_missing_count"] == 1
    assert "anchor_seed_coverage_missing" in report["warnings"]
    assert "core_seed_coverage_missing" not in report["warnings"]


def test_quality_report_flags_required_seed_without_source_evidence(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "data" / "papers.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "anchor_papers.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    seeds = workspace / ".papercompass" / "plans" / "seed_papers.jsonl"
    seeds.parent.mkdir(parents=True, exist_ok=True)
    seeds.write_text(
        json.dumps({"title": "Model Hallucinated Seed", "paper_role": "core_method", "required": True})
        + "\n",
        encoding="utf-8",
    )
    (workspace / "catalog").mkdir(exist_ok=True)
    (workspace / "catalog" / "manifest.json").write_text(
        json.dumps({"paper_count": 0}), encoding="utf-8"
    )

    report = build_quality_report(workspace)

    assert "seed_without_source_evidence" in report["critical"]
    assert report["seed_coverage"]["required_without_evidence_count"] == 1


def test_quality_report_flags_seed_role_misplacement_after_scoring(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "data" / "papers.json").write_text(
        json.dumps([{"title": "Anchor In Main", "year": 2024, "paper_role": "background_anchor"}]),
        encoding="utf-8",
    )
    (workspace / "data" / "anchor_papers.json").write_text(
        json.dumps([{"title": "Core In Anchors", "year": 2024, "paper_role": "core_method"}]),
        encoding="utf-8",
    )
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    seeds = workspace / ".papercompass" / "plans" / "seed_papers.jsonl"
    seeds.parent.mkdir(parents=True, exist_ok=True)
    seeds.write_text(
        "\n".join([
            json.dumps({"title": "Core In Anchors", "paper_role": "core_method", "required": True, "verified": True, "evidence": _seed_evidence("Core In Anchors")}),
            json.dumps({"title": "Anchor In Main", "paper_role": "background_anchor", "required": True, "verified": True, "evidence": _seed_evidence("Anchor In Main")}),
        ])
        + "\n",
        encoding="utf-8",
    )
    reviews = workspace / ".papercompass" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    (reviews / "score_decisions_20260101_000000.jsonl").write_text(
        json.dumps({
            "candidate_key": "title:any",
            "title": "Any",
            "verdict": "out_of_scope",
            "brain_score": 10,
            "score": 10,
        })
        + "\n",
        encoding="utf-8",
    )
    (workspace / "catalog").mkdir(exist_ok=True)
    (workspace / "catalog" / "manifest.json").write_text(
        json.dumps({"paper_count": 1}), encoding="utf-8"
    )

    report = build_quality_report(workspace)

    seed = report["seed_coverage"]
    assert seed["missing_count"] == 0
    assert seed["core_misplaced_count"] == 1
    assert seed["anchor_misplaced_count"] == 1
    assert "core_seed_misplaced" in report["warnings"]
    assert "anchor_seed_misplaced" in report["warnings"]


def test_quality_report_defers_seed_role_misplacement_before_scoring(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "data" / "papers.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "anchor_papers.json").write_text(
        json.dumps([{"title": "Core Still Pending Elsewhere", "year": 2024, "paper_role": "core_method"}]),
        encoding="utf-8",
    )
    (workspace / "data" / "pending_review_candidates.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    seeds = workspace / ".papercompass" / "plans" / "seed_papers.jsonl"
    seeds.parent.mkdir(parents=True, exist_ok=True)
    seeds.write_text(
        json.dumps({"title": "Core Still Pending Elsewhere", "paper_role": "core_method", "required": True, "verified": True, "evidence": _seed_evidence("Core Still Pending Elsewhere")})
        + "\n",
        encoding="utf-8",
    )
    (workspace / "catalog").mkdir(exist_ok=True)
    (workspace / "catalog" / "manifest.json").write_text(
        json.dumps({"paper_count": 0}), encoding="utf-8"
    )

    report = build_quality_report(workspace)

    assert report["seed_coverage"]["core_misplaced_count"] == 1
    assert "core_seed_misplaced" not in report["warnings"]


def test_query_coverage_checks_search_hints_not_discriminators(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "topic.yaml").write_text(
        """
topic_id: topic
search_hints:
  - small language model agent
discriminator_terms:
  - MobileLLM
  - function-calling agent
""".strip(),
        encoding="utf-8",
    )
    (workspace / "sources.yaml").write_text(
        """
discovery:
  sources:
    - openalex
  openalex:
    queries:
      - text: small language model agent
        strength: strong
""".strip(),
        encoding="utf-8",
    )

    report = query_coverage_report(workspace)

    assert report["term_count"] == 1
    assert report["uncovered_count"] == 0


def test_query_coverage_accepts_cot_normalized_queries(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "topic.yaml").write_text(
        """
topic_id: topic
search_hints:
  - implicit chain-of-thought reasoning
  - mechanism of chain-of-thought
""".strip(),
        encoding="utf-8",
    )
    (workspace / "sources.yaml").write_text(
        """
discovery:
  sources:
    - openalex
    - arxiv
  openalex:
    queries:
      - text: implicit chain thought reasoning
        strength: strong
  arxiv:
    queries:
      - all:mechanism AND all:chain AND all:thought
""".strip(),
        encoding="utf-8",
    )

    report = query_coverage_report(workspace)

    assert report["term_count"] == 2
    assert report["uncovered_count"] == 0

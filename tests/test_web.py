import json

from papercompass.web import make_build_status, score_paper


def test_short_multiword_query_requires_all_terms() -> None:
    paper = {
        "title": "RoLegalGEC: Legal Domain Grammatical Error Detection and Correction Dataset for Romanian",
        "abstract": "A grammatical error correction dataset for Romanian legal text.",
        "keyword_hits": ["grammatical error correction"],
        "authors": "",
        "venue": "arXiv",
    }
    assert score_paper(paper, "chinese grammatical") == 0


def test_short_multiword_query_keeps_exact_topic_match() -> None:
    paper = {
        "title": "Chinese Grammatical Error Correction: A Survey",
        "abstract": "",
        "keyword_hits": ["Chinese grammatical error correction"],
        "authors": "",
        "venue": "ACL",
    }
    assert score_paper(paper, "chinese grammatical") > 0


def test_make_build_status_reads_auto_summary(tmp_path) -> None:
    ws = tmp_path / "ws"
    auto = ws / ".papercompass" / "auto"
    auto.mkdir(parents=True)
    (ws / ".papercompass" / "manifests").mkdir(parents=True)
    (ws / "topic.yaml").write_text("topic_id: test\n", encoding="utf-8")
    qa_path = ws / ".papercompass" / "manifests" / "quality_gates_20260101_000000.json"
    qa_path.write_text(json.dumps({"status": "warning", "warnings": ["source_coverage_partial"]}), encoding="utf-8")
    (auto / "final_summary.json").write_text(
        json.dumps({
            "deliverable_status": "partial_due_to_budget",
            "safe_for_default_llm_retrieval": False,
            "direction": "Test direction",
            "counts": {"papers": 3, "pending": 0, "rejected": 2},
            "artifacts": {"qa_manifest": str(qa_path)},
        }),
        encoding="utf-8",
    )
    (auto / "state.json").write_text(
        json.dumps({
            "direction": "Test direction",
            "brain": "codex",
            "stages": {
                "plan_direction": {"status": "completed"},
                "score_papers": {"status": "completed", "batches": 1},
            },
        }),
        encoding="utf-8",
    )

    status = make_build_status(ws)

    assert status["has_summary"] is True
    assert status["summary"]["deliverable_status"] == "partial_due_to_budget"
    assert status["qa"]["warnings"] == ["source_coverage_partial"]
    assert [stage["name"] for stage in status["stages"]] == ["plan_direction", "score_papers"]


def test_make_build_status_uses_latest_quality_counts_when_summary_is_stale(tmp_path) -> None:
    ws = tmp_path / "ws"
    auto = ws / ".papercompass" / "auto"
    auto.mkdir(parents=True)
    manifests = ws / ".papercompass" / "manifests"
    manifests.mkdir(parents=True)
    (ws / "topic.yaml").write_text("topic_id: test\n", encoding="utf-8")
    qa_path = manifests / "quality_gates_20260101_000000.json"
    qa_path.write_text(
        json.dumps({
            "status": "warning",
            "warnings": ["catalog_count_mismatch"],
            "critical": [],
            "counts": {"papers": 1, "anchors": 0, "pending": 2, "rejected": 3},
        }),
        encoding="utf-8",
    )
    (auto / "final_summary.json").write_text(
        json.dumps({
            "deliverable_status": "passed_authoritative",
            "safe_for_default_llm_retrieval": True,
            "counts": {"papers": 9, "anchors": 0, "pending": 0, "rejected": 0},
            "artifacts": {"qa_manifest": str(qa_path)},
        }),
        encoding="utf-8",
    )

    status = make_build_status(ws)

    assert status["summary_stale"] is True
    assert status["summary"]["counts"]["papers"] == 1
    assert status["summary"]["counts"]["pending"] == 2
    assert status["summary"]["safe_for_default_llm_retrieval"] is False

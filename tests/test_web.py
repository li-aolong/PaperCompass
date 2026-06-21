import json
from pathlib import Path

from papercompass.web import (
    make_build_status,
    make_filters,
    paper_to_result,
    score_paper,
    search_papers,
)


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


def _write_web_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    data = workspace / "data"
    catalog = workspace / "catalog" / "index"
    data.mkdir(parents=True)
    catalog.mkdir(parents=True)
    papers = [
        {
            "paper_key": "core",
            "title": "Core decoding paper",
            "year": 2025,
            "venue": "ICLR",
            "paper_role": "core_method",
            "decision": {"reason": "fusion_accept"},
            "tags": ["review:accepted", "speculative decoding"],
            "keyword_hits": ["speculative decoding"],
            "sources": ["openalex"],
        },
        {
            "paper_key": "anchor",
            "title": "Background anchor paper",
            "year": 2024,
            "venue": "arXiv",
            "paper_role": "background_anchor",
            "decision": {"reason": "required_seed_anchor"},
            "tags": ["review:anchor"],
            "keyword_hits": ["speculative sampling"],
            "sources": ["arxiv"],
        },
    ]
    (data / "papers.json").write_text(json.dumps(papers), encoding="utf-8")
    (catalog / "by_year.json").write_text(
        json.dumps({"2025": ["core"], "2024": ["anchor"]}),
        encoding="utf-8",
    )
    return workspace


def test_paper_to_result_exposes_role_badge_fields() -> None:
    result = paper_to_result(
        {
            "paper_key": "p1",
            "title": "Paper",
            "paper_role": "core_method",
            "decision": {"reason": "fusion_accept"},
        }
    )

    assert result["paper_role"] == "core_method"
    assert result["role_label"] == "Core"
    assert result["decision_label"] == "fusion_accept"


def test_web_filters_and_search_support_paper_role(tmp_path: Path) -> None:
    workspace = _write_web_workspace(tmp_path)

    filters = make_filters(workspace)
    roles = {item["value"]: item for item in filters["roles"]}
    assert roles["core_method"]["label"] == "Core"
    assert roles["background_anchor"]["label"] == "Anchor"

    result = search_papers(
        workspace,
        {"role": ["core_method"], "limit": ["10"], "offset": ["0"]},
    )
    assert result["total"] == 1
    assert result["results"][0]["paper_key"] == "core"
    assert result["results"][0]["role_label"] == "Core"

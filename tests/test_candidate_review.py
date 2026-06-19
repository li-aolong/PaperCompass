import json

from papercompass.build import build_workspace
from papercompass.candidate_review import applied_decisions_path
from papercompass.candidate_review import apply_review_decisions
from papercompass.candidate_review import build_weak_candidate_review, validate_review_decisions
from papercompass.candidate_review import workspace_decision_context_hash
from papercompass.config import init_workspace
from papercompass.text import iter_jsonl, read_json


def _seed_review_workspace(tmp_path):
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "topic.yaml").write_text(
        """
topic_id: topic
min_year: 2022
strict_patterns: []
soft_patterns:
  - "\\\\bwriting feedback\\\\b"
negative_patterns: []
title_focus_patterns: []
""".strip(),
        encoding="utf-8",
    )
    raw_path = workspace / ".raw" / "agent_search" / "weak.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        '{"source_name":"agent","source_type":"agent_search","raw":{"title":"Feedback for Learner Writing","year":2024,"abstract":"writing feedback"}}\n',
        encoding="utf-8",
    )
    build_workspace(workspace)
    review = build_weak_candidate_review(workspace)
    queue_path = workspace / ".papercompass" / "reviews" / f"weak_candidates_{review['run_id']}.json"
    queue = read_json(queue_path)
    candidate = queue["review_candidates"][0]
    decisions_path = workspace / ".papercompass" / "reviews" / "decisions.jsonl"
    decisions_path.write_text(json.dumps({
        "candidate_key": candidate["candidate_key"],
        "title": candidate["title"],
        "year": candidate["year"],
        "decision": "accept",
        "reason": "属于方向边界内",
        "action": "accept_to_main",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    return workspace, queue_path, decisions_path


def test_build_weak_candidate_review_uses_pending_queue(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    data_dir = workspace / "data"
    (data_dir / "papers.json").write_text(json.dumps([
        {
            "paper_key": "paper-strong",
            "title": "Strong Included Paper",
            "year": 2024,
            "decision": {"included": True, "confidence": "strong", "reason": "strong_topic_signal"},
        },
    ]), encoding="utf-8")
    (data_dir / "pending_review_candidates.json").write_text(json.dumps([
        {
            "title": "Weak Pending Paper",
            "year": 2024,
            "venue": "ACL",
            "abstract": "A weak but plausible candidate.",
            "decision": {"included": False, "confidence": "weak", "needs_review": True, "reason": "weak_topic_signal"},
            "tags": ["confidence:weak", "review:weak_topic_signal"],
            "source_records": [{"query": "acl2024", "raw_path": ".raw/paperlists/acl/2024.jsonl"}],
        },
    ]), encoding="utf-8")
    (data_dir / "rejected_candidates.json").write_text("[]", encoding="utf-8")

    result = build_weak_candidate_review(workspace)

    assert result["pending_weak_count"] == 1
    assert result["review_candidate_count"] == 1
    payload = read_json(workspace / ".papercompass" / "reviews" / f"weak_candidates_{result['run_id']}.json")
    assert payload["pending_weak"][0]["title"] == "Weak Pending Paper"
    assert payload["review_candidates"][0]["title"] == "Weak Pending Paper"
    md = (workspace / ".papercompass" / "reviews" / f"weak_candidates_{result['run_id']}.md").read_text(encoding="utf-8")
    assert "所有弱候选都先进入统一复核队列" in md


def test_review_decisions_can_be_validated_and_applied(tmp_path) -> None:
    workspace, queue_path, decisions_path = _seed_review_workspace(tmp_path)

    validation = validate_review_decisions(queue_path, decisions_path)
    assert validation["valid"] is True
    assert validation["decision_counts"]["accept"] == 1
    apply_review_decisions(workspace, decisions_path, queue_path=queue_path)
    build_workspace(workspace)
    papers = read_json(workspace / "data" / "papers.json")
    pending = read_json(workspace / "data" / "pending_review_candidates.json")

    assert [paper["title"] for paper in papers] == ["Feedback for Learner Writing"]
    assert pending == []
    applied = list(iter_jsonl(applied_decisions_path(workspace)))
    assert applied[-1]["queue_hash"] == validation["queue_hash"]
    assert applied[-1]["decision_context_hash"] == workspace_decision_context_hash(workspace)
    assert applied[-1]["scoring_policy"] == "papercompass.review_policy.v1"
    evidence = papers[0]["topic_relevance_evidence"]
    assert evidence["review_decision"] == "accept"
    assert evidence["queue_hash"] == validation["queue_hash"]


def test_anchor_decision_routes_to_anchor_library(tmp_path) -> None:
    workspace, queue_path, decisions_path = _seed_review_workspace(tmp_path)
    row = next(iter_jsonl(decisions_path))
    row["decision"] = "anchor"
    row["action"] = "add_to_anchor"
    row["paper_role"] = "background_anchor"
    decisions_path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    validation = validate_review_decisions(queue_path, decisions_path)
    assert validation["valid"] is True
    assert validation["decision_counts"]["anchor"] == 1
    apply_review_decisions(workspace, decisions_path, queue_path=queue_path)
    build_workspace(workspace)

    papers = read_json(workspace / "data" / "papers.json")
    anchors = read_json(workspace / "data" / "anchor_papers.json")
    pending = read_json(workspace / "data" / "pending_review_candidates.json")
    assert papers == []
    assert [paper["title"] for paper in anchors] == ["Feedback for Learner Writing"]
    assert anchors[0]["paper_role"] == "background_anchor"
    assert pending == []


def test_stale_review_decision_is_ignored_after_topic_change(tmp_path) -> None:
    workspace, queue_path, decisions_path = _seed_review_workspace(tmp_path)
    apply_review_decisions(workspace, decisions_path, queue_path=queue_path)
    build_workspace(workspace)
    assert read_json(workspace / "data" / "papers.json")[0]["title"] == "Feedback for Learner Writing"

    (workspace / "topic.yaml").write_text(
        (workspace / "topic.yaml").read_text(encoding="utf-8")
        + "\nsearch_hints:\n  - changed topic\n",
        encoding="utf-8",
    )
    build_workspace(workspace)
    papers = read_json(workspace / "data" / "papers.json")
    pending = read_json(workspace / "data" / "pending_review_candidates.json")

    assert papers == []
    assert [item["title"] for item in pending] == ["Feedback for Learner Writing"]


# test_strong_risk_candidates_are_queued_without_blocking_main_library
# was a v2 keyword-rule integration test (strict_patterns → strong; cross-modal
# pattern → audit queue). v3 normalize doesn't put non-manual papers into
# main on keyword match; that decision moves to the fusion stage. Removed
# because the v3 contract this test described no longer exists.

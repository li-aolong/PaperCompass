import json
import re
from pathlib import Path

from papercompass.candidate_review import applied_decisions_path, workspace_decision_context_hash
from papercompass.roles import ANCHOR_ROLES, MAIN_LIBRARY_ROLES
from papercompass.text import iter_jsonl


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_ROOT = REPO_ROOT / "examples"
ABSOLUTE_PATH_RE = re.compile(r"(/Users/|/home/|/private/|[A-Za-z]:\\\\)")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def example_workspaces() -> list[Path]:
    return sorted(path for path in EXAMPLES_ROOT.iterdir() if (path / "topic.yaml").exists())


def test_examples_keep_library_roles_in_the_right_files() -> None:
    workspaces = example_workspaces()
    assert workspaces
    for workspace in workspaces:
        papers = read_json(workspace / "data" / "papers.json")
        anchors = read_json(workspace / "data" / "anchor_papers.json")

        main_roles = {paper.get("paper_role") for paper in papers}
        anchor_roles = {paper.get("paper_role") for paper in anchors}

        assert main_roles <= MAIN_LIBRARY_ROLES, workspace
        assert anchor_roles <= ANCHOR_ROLES, workspace


def test_examples_have_synced_summary_and_catalog_counts() -> None:
    for workspace in example_workspaces():
        papers = read_json(workspace / "data" / "papers.json")
        anchors = read_json(workspace / "data" / "anchor_papers.json")
        pending = read_json(workspace / "data" / "pending_review_candidates.json")
        rejected = read_json(workspace / "data" / "rejected_candidates.json")
        summary = read_json(workspace / ".papercompass" / "auto" / "final_summary.json")
        manifest = read_json(workspace / "catalog" / "manifest.json")

        expected_counts = {
            "papers": len(papers),
            "anchors": len(anchors),
            "pending": len(pending),
            "rejected": len(rejected),
        }

        assert summary["counts"] == expected_counts, workspace
        assert summary["qa_status"] == "passed", workspace
        assert summary["safe_for_default_llm_retrieval"] is True, workspace
        assert manifest["paper_count"] == len(papers), workspace


def test_examples_do_not_leak_machine_absolute_paths() -> None:
    checked_any = False
    for workspace in example_workspaces():
        for path in (
            workspace / ".papercompass" / "auto" / "final_summary.json",
            workspace / "catalog" / "manifest.json",
            workspace / "catalog" / "README.md",
        ):
            if not path.exists():
                continue
            checked_any = True
            assert not ABSOLUTE_PATH_RE.search(path.read_text(encoding="utf-8")), path
    assert checked_any


def test_examples_applied_review_decisions_match_current_context() -> None:
    for workspace in example_workspaces():
        path = applied_decisions_path(workspace)
        if not path.exists():
            continue
        current_hash = workspace_decision_context_hash(workspace)
        stale = [
            row.get("candidate_key") or row.get("title")
            for row in iter_jsonl(path)
            if isinstance(row, dict) and row.get("decision_context_hash") != current_hash
        ]

        assert stale == [], workspace

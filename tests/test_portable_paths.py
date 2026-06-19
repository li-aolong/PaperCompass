from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from papercompass.auto.orchestrator import _write_queue_gate_summary
from papercompass.auto.state import AutoState
from papercompass.build import add_manual_paper, build_workspace
from papercompass.catalog import build_catalog
from papercompass.config import load_topic_config, workspace_relative_path, write_yaml
from papercompass.qa import build_quality_report


def _assert_no_workspace_absolute_path(path: Path, workspace: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert str(workspace) not in text
    assert str(workspace.resolve()) not in text


def test_generated_workspace_artifacts_use_portable_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "portable-topic--2022plus"
    workspace.mkdir()
    write_yaml(
        workspace / "topic.yaml",
        {
            "topic_id": "portable-topic",
            "name": "Portable topic",
            "min_year": 2022,
            "search_hints": ["portable topic"],
            "search_keyword_text": "portable topic",
            "discriminator_terms": ["portable topic"],
            "judge_examples": {"in_scope": [], "out_of_scope": []},
        },
    )
    write_yaml(workspace / "sources.yaml", {"sources": {}})

    add_manual_paper(
        workspace,
        {
            "title": "Portable Topic Paper",
            "year": 2024,
            "abstract": "A test paper for portable workspace artifacts.",
            "authors": "A. Researcher",
            "url": "https://example.org/paper",
        },
    )
    build_result = build_workspace(workspace)
    catalog_result = build_catalog(workspace)
    qa_report = build_quality_report(workspace)

    assert build_result["workspace"] == workspace.name
    assert build_result["log"].startswith(".papercompass/logs/")
    assert catalog_result["catalog"] == "catalog"
    assert catalog_result["manifest"] == "catalog/manifest.json"
    assert qa_report["manifest"].startswith(".papercompass/manifests/")
    assert qa_report["markdown"].startswith(".papercompass/reports/")

    state = AutoState(workspace)
    state.begin_stage("path_portability_check", manifest=str(workspace / ".papercompass" / "x.json"))
    state.end_stage("path_portability_check", status="completed", output=str(workspace / "data" / "papers.jsonl"))
    topic = load_topic_config(workspace)
    _write_queue_gate_summary(
        workspace=workspace,
        direction="portable topic",
        brain_plugin=SimpleNamespace(name="stub-brain"),
        second_brain_plugin=None,
        topic=topic,
        state=state,
        qa_report=qa_report,
        queue_diagnosis={
            "status": "partial_due_to_budget",
            "reason_codes": ["weak_batches_over_budget"],
            "needed_batches": 2,
            "effective_batches": 1,
            "uncovered_count": 1,
        },
        embedding_available=True,
        environment_warnings=[],
        weak_batch_size=25,
        weak_max_batches=1,
        boundary_max_batches=None,
        seed_total=0,
    )

    summary = json.loads(
        (workspace / ".papercompass" / "auto" / "final_summary.json").read_text(encoding="utf-8")
    )
    assert summary["workspace"] == workspace.name
    assert summary["artifacts"]["state"] == ".papercompass/auto/state.json"
    assert summary["artifacts"]["main_library"] == "data/papers.jsonl"
    assert summary["artifacts"]["catalog_manifest"] == "catalog/manifest.json"

    generated = [
        workspace / ".papercompass" / "manifests" / "latest.json",
        workspace / ".papercompass" / "auto" / "state.json",
        workspace / ".papercompass" / "auto" / "final_summary.json",
        workspace / "catalog" / "manifest.json",
        workspace / "catalog" / "README.md",
        workspace / qa_report["manifest"],
        workspace / qa_report["markdown"],
    ]
    for path in generated:
        _assert_no_workspace_absolute_path(path, workspace)


def test_workspace_relative_path_handles_synced_foreign_root(tmp_path: Path) -> None:
    workspace = tmp_path / "portable-topic--2022plus"
    old_wsl_path = (
        "/home/yhli/my-research/PaperCompass/workspaces/"
        "portable-topic--2022plus/.papercompass/auto/final_summary.json"
    )

    assert (
        workspace_relative_path(workspace, old_wsl_path)
        == ".papercompass/auto/final_summary.json"
    )

import json

from papercompass.build import build_workspace
from papercompass.config import init_workspace
from papercompass.text import read_json


def _write_imported_candidates(workspace, rows):
    raw_path = workspace / ".raw" / "imported" / "papers.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        "\n".join(
            json.dumps({
                "source_name": "fixture",
                "source_type": "imported_paper",
                "raw": row,
            }, ensure_ascii=False)
            for row in rows
        )
        + "\n",
        encoding="utf-8",
    )


def test_build_scope_gate_rejects_out_of_scope_main_papers(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "topic.yaml").write_text(
        """
topic_id: topic
min_year: 2023
publication_scope:
  policy: preferred_venues_or_preprints
  strict: true
  strict_venue_list: true
  preferred_venues:
    - ACL
  include_preprints: true
""".strip(),
        encoding="utf-8",
    )
    _write_imported_candidates(workspace, [
        {"title": "ACL Paper", "year": 2024, "venue": "ACL"},
        {"title": "arXiv Paper", "year": 2025, "arxiv_id": "2501.00001"},
        {"title": "Journal Paper", "year": 2024, "venue": "Journal of NLP"},
    ])

    result = build_workspace(workspace)

    papers = read_json(workspace / "data" / "papers.json")
    rejected = read_json(workspace / "data" / "rejected_candidates.json")
    assert {paper["title"] for paper in papers} == {"ACL Paper", "arXiv Paper"}
    journal = next(item for item in rejected if item["title"] == "Journal Paper")
    assert journal["decision"]["reason"] == "publication_scope_violation"
    assert journal["publication_scope_gate"]["reason"] == "outside_publication_scope"
    assert result["publication_scope_rejected_count"] == 1
    assert result["publication_scope_gate"]["rejected_count"] == 1


def test_build_scope_gate_allows_manual_override(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "topic.yaml").write_text(
        """
topic_id: topic
min_year: 2023
publication_scope:
  policy: preferred_venues_or_preprints
  strict: true
  strict_venue_list: true
  preferred_venues:
    - ACL
  include_preprints: false
""".strip(),
        encoding="utf-8",
    )
    _write_imported_candidates(workspace, [
        {"title": "Journal Paper", "year": 2024, "venue": "Journal of NLP"},
    ])
    overrides = workspace / "overrides" / "manual.jsonl"
    overrides.parent.mkdir(parents=True, exist_ok=True)
    overrides.write_text(
        json.dumps({
            "title": "Journal Paper",
            "year": 2024,
            "publication_scope_override": True,
            "system_tags": ["publication_scope_override"],
        }, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    result = build_workspace(workspace)

    papers = read_json(workspace / "data" / "papers.json")
    rejected = read_json(workspace / "data" / "rejected_candidates.json")
    assert [paper["title"] for paper in papers] == ["Journal Paper"]
    assert papers[0]["publication_scope_gate"]["status"] == "override_allowed"
    assert rejected == []
    assert result["publication_scope_gate"]["override_count"] == 1


def test_build_scope_gate_does_not_treat_generic_manual_patch_as_scope_override(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "topic.yaml").write_text(
        """
topic_id: topic
min_year: 2023
publication_scope:
  policy: preferred_venues_or_preprints
  strict: true
  strict_venue_list: true
  preferred_venues:
    - ACL
  include_preprints: false
""".strip(),
        encoding="utf-8",
    )
    _write_imported_candidates(workspace, [
        {"title": "Journal Paper", "year": 2024, "venue": "Journal of NLP"},
    ])
    overrides = workspace / "overrides" / "manual.jsonl"
    overrides.parent.mkdir(parents=True, exist_ok=True)
    overrides.write_text(
        json.dumps({
            "title": "Journal Paper",
            "year": 2024,
            "authors": "Corrected Author",
        }, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    result = build_workspace(workspace)

    papers = read_json(workspace / "data" / "papers.json")
    rejected = read_json(workspace / "data" / "rejected_candidates.json")
    assert papers == []
    assert [item["title"] for item in rejected] == ["Journal Paper"]
    assert result["publication_scope_gate"]["override_count"] == 0


def test_build_scope_gate_keeps_background_anchor_out_of_main_gate(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "topic.yaml").write_text(
        """
topic_id: topic
min_year: 2023
publication_scope:
  policy: preferred_venues_or_preprints
  strict: true
  strict_venue_list: true
  preferred_venues:
    - ACL
  include_preprints: false
""".strip(),
        encoding="utf-8",
    )
    _write_imported_candidates(workspace, [
        {
            "title": "Classic Journal Anchor",
            "year": 2024,
            "venue": "Journal of NLP",
            "paper_role": "background_anchor",
        },
    ])

    build_workspace(workspace)

    papers = read_json(workspace / "data" / "papers.json")
    anchors = read_json(workspace / "data" / "anchor_papers.json")
    rejected = read_json(workspace / "data" / "rejected_candidates.json")
    assert papers == []
    assert [paper["title"] for paper in anchors] == ["Classic Journal Anchor"]
    assert rejected == []


def test_build_excludes_negative_roles_from_main_library(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    _write_imported_candidates(workspace, [
        {"title": "Core Paper", "year": 2024, "paper_role": "core_method"},
        {"title": "Boundary Contrast", "year": 2024, "paper_role": "boundary_negative"},
        {"title": "Off Topic Contrast", "year": 2024, "paper_role": "out_of_scope"},
    ])

    build_workspace(workspace)

    papers = read_json(workspace / "data" / "papers.json")
    rejected = read_json(workspace / "data" / "rejected_candidates.json")
    assert [paper["title"] for paper in papers] == ["Core Paper"]
    assert {item["title"] for item in rejected} == {"Boundary Contrast", "Off Topic Contrast"}
    assert {item["decision"]["reason"] for item in rejected} == {"negative_role_excluded"}

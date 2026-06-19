from __future__ import annotations

import json
from pathlib import Path

from papercompass.auto.audit import seed_recall


def _seed_workspace(tmp_path: Path, *, main_titles: list[str], pending_titles: list[str], rejected_titles: list[str], seeds: list[dict]) -> Path:
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    (ws / ".papercompass" / "plans").mkdir(parents=True)
    (ws / ".raw").mkdir(parents=True)
    (ws / "data" / "papers.json").write_text(
        json.dumps([{"title": t, "year": 2024} for t in main_titles])
    )
    (ws / "data" / "pending_review_candidates.json").write_text(
        json.dumps([{"title": t, "year": 2024} for t in pending_titles])
    )
    (ws / "data" / "rejected_candidates.json").write_text(
        json.dumps([{"title": t, "year": 2024} for t in rejected_titles])
    )
    seeds_path = ws / ".papercompass" / "plans" / "anchors.jsonl"
    seeds_path.write_text("\n".join(json.dumps(s) for s in seeds) + "\n")
    return ws


def test_seed_recall_classifies_by_location(tmp_path: Path):
    seeds = [
        {"title": "Fast Inference via Speculative Decoding", "year": 2022, "arxiv_id": "2211.17192"},
        {"title": "Medusa: Simple LLM Inference Acceleration", "year": 2024},
        {"title": "EAGLE: Speculative Sampling Requires Rethinking", "year": 2024},
        {"title": "Quiet-STaR: Language Models Can Teach Themselves", "year": 2024},
    ]
    ws = _seed_workspace(
        tmp_path,
        main_titles=["Fast Inference via Speculative Decoding", "Medusa: Simple LLM Inference Acceleration"],
        pending_titles=["EAGLE: Speculative Sampling Requires Rethinking"],
        rejected_titles=[],
        seeds=seeds,
    )
    out = seed_recall(ws)
    assert out["total"] == 4
    assert out["in_main"] == 2
    assert out["in_pending"] == 1
    assert out["in_rejected"] == 0
    assert out["missing_count"] == 1
    assert out["missing"][0]["title"].startswith("Quiet-STaR")
    assert out["recall_main"] == 0.5
    assert out["recall_anywhere"] == 0.75


def test_precision_sample_with_stub_brain(tmp_path: Path):
    from papercompass.auto.audit import precision_sample
    from papercompass.plugins import BrainPlugin, BrainResponse

    class StubBrain(BrainPlugin):
        name = "stub"
        display = "stub"

        @classmethod
        def is_available(cls) -> bool:
            return True

        def ask(self, prompt, *, schema=None, context_files=None, timeout=600, **kwargs):
            judgements = [
                {"candidate_key": f"p{i}", "title": f"Paper {i}", "verdict": "in_scope", "reason": "ok"}
                for i in range(3)
            ]
            payload = {"judgements": judgements}
            return BrainResponse(text=json.dumps(payload), parsed=payload, plugin="stub")

    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "papers.json").write_text(
        json.dumps([{"title": f"Paper {i}", "year": 2024, "paper_key": f"p{i}"} for i in range(3)])
    )
    (ws / "topic.yaml").write_text("topic_id: t\nname: t\ndescription: d\nmin_year: 2024\n")
    out = precision_sample(ws, brain=StubBrain(), sample_size=3)
    assert out["status"] == "judged"
    assert out["sample_size"] == 3
    assert out["counts"]["in_scope"] == 3
    assert out["precision_in_scope"] == 1.0


def test_seed_recall_handles_no_seeds(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "papers.json").write_text("[]")
    out = seed_recall(ws)
    assert out["status"] == "no_seeds"
    assert out["total"] == 0


def _ws_with_state(tmp_path: Path, build_brain: str) -> Path:
    ws = tmp_path / "ws"
    (ws / ".papercompass" / "auto").mkdir(parents=True)
    (ws / ".papercompass" / "auto" / "state.json").write_text(
        json.dumps({"brain": build_brain}), encoding="utf-8"
    )
    return ws


def test_select_audit_brain_explicit_wins(tmp_path: Path):
    from papercompass.cli import select_audit_brain
    ws = _ws_with_state(tmp_path, "deepseek")
    pref, mode, note, build_brain = select_audit_brain(
        workspace=ws, requested_brain="codex",
        same_brain=False,
    )
    assert pref == "codex"
    assert mode == "explicit_brain"
    assert build_brain == "deepseek"


def test_select_audit_brain_same_brain_flag(tmp_path: Path):
    from papercompass.cli import select_audit_brain
    ws = _ws_with_state(tmp_path, "deepseek")
    pref, mode, _note, _build = select_audit_brain(
        workspace=ws, requested_brain=None,
        same_brain=True,
    )
    assert pref == "deepseek"
    assert mode == "same_brain_explicit"


def test_select_audit_brain_requires_explicit_brain(tmp_path: Path):
    from papercompass.cli import select_audit_brain
    ws = _ws_with_state(tmp_path, "deepseek")
    pref, mode, note, build_brain = select_audit_brain(
        workspace=ws, requested_brain=None,
        same_brain=False,
    )
    assert pref is None
    assert mode == "missing_audit_brain"
    assert "does not choose a default audit agent" in note
    assert build_brain == "deepseek"


def test_select_audit_brain_same_brain_requires_build_state(tmp_path: Path):
    from papercompass.cli import select_audit_brain
    ws = tmp_path / "ws"
    ws.mkdir()
    pref, mode, note, _build = select_audit_brain(
        workspace=ws, requested_brain=None,
        same_brain=True,
    )
    assert pref is None
    assert mode == "missing_audit_brain"
    assert "--same-brain requested" in note


def test_select_audit_brain_handles_missing_state(tmp_path: Path):
    """Workspace without state.json (build never finished, or fresh dir)."""
    from papercompass.cli import select_audit_brain
    ws = tmp_path / "ws"
    ws.mkdir()
    pref, mode, _note, build_brain = select_audit_brain(
        workspace=ws, requested_brain=None,
        same_brain=False,
    )
    assert pref is None
    assert mode == "missing_audit_brain"
    assert build_brain == ""

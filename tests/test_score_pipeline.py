"""Integration test for the v3 score → resolve_boundary pipeline.

Avoids real brain calls by feeding stage_score_papers a stub brain that
returns canned 0-100 scores. Verifies:
  - score_papers writes a decisions file with verdicts
  - apply_review_decisions promotes in_scope candidates to main
  - resolve_boundary forces boundary papers to in/out using stub brain +
    metadata fallback
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from papercompass.auto.state import AutoState
from papercompass.plugins import BrainPlugin, BrainResponse


def _score_row(candidate_key: str, score: int) -> dict:
    confidence = "high" if score >= 75 else "medium" if score >= 35 else "low"
    return {
        "candidate_key": candidate_key,
        "score": score,
        "paper_role": "core_method",
        "confidence": confidence,
        "inclusion_evidence": ["stub inclusion"] if score >= 35 else [],
        "exclusion_evidence": ["stub exclusion"] if score < 35 else [],
        "missing_information": [],
        "reason": "stub",
    }


class CannedScoreBrain(BrainPlugin):
    """Returns 0-100 scores from a fixed dict {candidate_key: score}."""

    name = "stub-score"
    display = "stub score brain"

    def __init__(self, scores_by_key: dict[str, int]) -> None:
        self.scores_by_key = scores_by_key
        self.calls: list[str] = []

    @classmethod
    def is_available(cls):
        return True

    def ask(self, prompt, *, schema=None, **kwargs):
        self.calls.append(prompt)
        # Parse candidate_keys out of the prompt by string match (lazy but
        # avoids JSON-prompt parsing).
        keys_in_prompt = []
        for line in prompt.splitlines():
            if line.startswith("- candidate_key:"):
                k = line.split(":", 1)[1].strip()
                keys_in_prompt.append(k)
        rows = []
        for k in keys_in_prompt:
            score = int(self.scores_by_key.get(k, 50))
            rows.append(_score_row(k, score))
        payload = {"scores": rows}
        text = json.dumps(payload)
        return BrainResponse(
            text=text, parsed=payload, raw_stdout=text, raw_stderr="",
            plugin=self.name, duration_seconds=0.0, extra={},
        )


class OmitFirstScoreBrain(CannedScoreBrain):
    """Drops one key on the first call so stage retry logic is exercised."""

    def __init__(self, scores_by_key: dict[str, int]) -> None:
        super().__init__(scores_by_key)
        self.omitted_once = False

    def ask(self, prompt, *, schema=None, **kwargs):
        self.calls.append(prompt)
        keys_in_prompt = []
        for line in prompt.splitlines():
            if line.startswith("- candidate_key:"):
                keys_in_prompt.append(line.split(":", 1)[1].strip())
        if not self.omitted_once and keys_in_prompt:
            keys_in_prompt = keys_in_prompt[1:]
            self.omitted_once = True
        rows = [
            _score_row(k, int(self.scores_by_key.get(k, 50)))
            for k in keys_in_prompt
        ]
        payload = {"scores": rows}
        text = json.dumps(payload)
        return BrainResponse(
            text=text, parsed=payload, raw_stdout=text, raw_stderr="",
            plugin=self.name, duration_seconds=0.0, extra={},
        )


def _setup_workspace(tmp_path: Path) -> Path:
    """Build a minimal workspace with topic.yaml + pending queue + raw papers
    so build_weak_candidate_review can run."""
    from papercompass.config import init_workspace
    ws = tmp_path / "ws"
    init_workspace(ws, "stub-topic")
    (ws / "topic.yaml").write_text(
        "topic_id: stub-topic\n"
        "name: Stub topic\n"
        "min_year: 2022\n"
        "direction_raw: Stub direction\n"
        "search_hints:\n"
        "  - alpha\n"
        "  - beta\n"
        "search_keyword_text: alpha beta gamma research methods\n"
        "judge_examples:\n"
        "  in_scope:\n"
        "    - title: anchor1\n"
        "      reason: core\n"
        "  out_of_scope:\n"
        "    - title: out1\n"
        "      reason: off\n",
        encoding="utf-8",
    )
    raw = ws / ".raw" / "test" / "2024.jsonl"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "\n".join([
            json.dumps({"source_name": "test", "source_type": "test",
                        "raw": {"title": "Alpha paper one", "year": 2024,
                                "abstract": "alpha methods", "arxiv_id": "2401.00001"}}),
            json.dumps({"source_name": "test", "source_type": "test",
                        "raw": {"title": "Beta paper two", "year": 2024,
                                "abstract": "beta methods", "arxiv_id": "2401.00002"}}),
            json.dumps({"source_name": "test", "source_type": "test",
                        "raw": {"title": "Gamma paper three", "year": 2024,
                                "abstract": "gamma methods", "arxiv_id": "2401.00003"}}),
        ]),
        encoding="utf-8",
    )
    return ws


def test_score_papers_classifies_by_fused_score(tmp_path: Path):
    from papercompass.auto.stages import stage_score_papers
    from papercompass.build import build_workspace
    from papercompass.text import read_json

    ws = _setup_workspace(tmp_path)
    build_workspace(ws)  # writes pending_review_candidates.json

    pending = read_json(ws / "data" / "pending_review_candidates.json", [])
    assert isinstance(pending, list) and len(pending) >= 3
    keys = [p["candidate_key"] for p in pending]
    # Map: first → high score (in_scope), second → mid (boundary),
    # third → low (out_of_scope)
    scores = {keys[0]: 90, keys[1]: 55, keys[2]: 10}

    state = AutoState(ws)
    result = stage_score_papers(
        ws,
        brain=CannedScoreBrain(scores),
        state=state,
        batch_size=10,
    )
    assert result["status"] == "completed"
    assert result["prefilter"]["candidate_count"] >= 3
    prefilter_path = Path(result["prefilter_decisions"])
    assert prefilter_path.name == "prefilter_decisions.jsonl"
    prefilter_rows = [
        json.loads(line)
        for line in prefilter_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(prefilter_rows) == result["prefilter"]["candidate_count"]
    assert all("sent_to_llm" in row for row in prefilter_rows)
    counts = result["counts"]
    # Each verdict should appear at least once given our stub scores
    assert counts.get("in_scope", 0) >= 1
    assert counts.get("out_of_scope", 0) >= 1
    score_rows = [
        json.loads(line)
        for line in Path(result["decisions"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(float(row["brain_confidence"]) >= 0.8 for row in score_rows)
    assert any(row["inclusion_evidence"] == ["stub inclusion"] for row in score_rows)
    assert all("prefilter_score" in row for row in score_rows)
    assert any(row["prefilter_topic_hits"] for row in score_rows)
    review_path = Path(result["decisions"]).with_name(
        Path(result["decisions"]).stem.replace("score_decisions_", "review_decisions_")
        + ".jsonl"
    )
    review_rows = [
        json.loads(line)
        for line in review_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(float(row["confidence"]) >= 0.8 for row in review_rows)
    assert any(row["inclusion_evidence"] == ["stub inclusion"] for row in review_rows)


def test_required_manual_seed_bypasses_min_year_and_routes_to_anchor(tmp_path: Path):
    from papercompass.build import build_workspace
    from papercompass.config import init_workspace
    from papercompass.text import read_json

    ws = tmp_path / "ws"
    init_workspace(ws, "stub-topic")
    (ws / "topic.yaml").write_text(
        "topic_id: stub-topic\n"
        "name: Stub topic\n"
        "min_year: 2022\n",
        encoding="utf-8",
    )
    raw_path = ws / ".raw" / "manual" / "seed.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        json.dumps({
            "source_name": "auto_seed_repair",
            "source_type": "manual",
            "raw": {
                "title": "Required Old Anchor",
                "year": 2021,
                "arxiv_id": "2110.00001",
                "paper_role": "background_anchor",
                "required": True,
            },
        })
        + "\n",
        encoding="utf-8",
    )

    build_workspace(ws)

    anchors = read_json(ws / "data" / "anchor_papers.json", [])
    rejected = read_json(ws / "data" / "rejected_candidates.json", [])
    assert "Required Old Anchor" in {p["title"] for p in anchors}
    assert "Required Old Anchor" not in {p["title"] for p in rejected}


def test_score_papers_required_seed_roles_override_low_scores(tmp_path: Path):
    from papercompass.auto.stages import stage_score_papers
    from papercompass.build import build_workspace
    from papercompass.text import read_json

    ws = _setup_workspace(tmp_path)
    seeds_path = ws / ".papercompass" / "plans" / "seed_papers.jsonl"
    seeds_path.parent.mkdir(parents=True, exist_ok=True)
    seeds_path.write_text(
        "\n".join([
            json.dumps({
                "title": "Beta paper two",
                "year": 2024,
                "arxiv_id": "2401.00002",
                "paper_role": "background_anchor",
                "required": True,
            }),
            json.dumps({
                "title": "Gamma paper three",
                "year": 2024,
                "arxiv_id": "2401.00003",
                "paper_role": "core_method",
                "required": True,
            }),
        ])
        + "\n",
        encoding="utf-8",
    )
    build_workspace(ws)

    pending = read_json(ws / "data" / "pending_review_candidates.json", [])
    keys = [p["candidate_key"] for p in pending]
    scores = {key: 5 for key in keys}

    state = AutoState(ws)
    stage_score_papers(
        ws,
        brain=CannedScoreBrain(scores),
        state=state,
        batch_size=10,
    )
    build_workspace(ws)

    papers = read_json(ws / "data" / "papers.json", [])
    anchors = read_json(ws / "data" / "anchor_papers.json", [])
    rejected = read_json(ws / "data" / "rejected_candidates.json", [])
    assert "Gamma paper three" in {p["title"] for p in papers}
    assert "Beta paper two" in {p["title"] for p in anchors}
    assert "Beta paper two" not in {p["title"] for p in rejected}


def test_resolve_boundary_preserves_required_seed_routes(tmp_path: Path):
    from papercompass.auto.stages import stage_resolve_boundary, stage_score_papers
    from papercompass.build import build_workspace
    from papercompass.text import iter_jsonl, read_json

    ws = _setup_workspace(tmp_path)
    seeds_path = ws / ".papercompass" / "plans" / "seed_papers.jsonl"
    seeds_path.parent.mkdir(parents=True, exist_ok=True)
    seeds_path.write_text(
        "\n".join([
            json.dumps({
                "title": "Beta paper two",
                "year": 2024,
                "arxiv_id": "2401.00002",
                "paper_role": "background_anchor",
                "required": True,
            }),
            json.dumps({
                "title": "Gamma paper three",
                "year": 2024,
                "arxiv_id": "2401.00003",
                "paper_role": "core_method",
                "required": True,
            }),
        ])
        + "\n",
        encoding="utf-8",
    )
    build_workspace(ws)
    pending = read_json(ws / "data" / "pending_review_candidates.json", [])
    keys = [p["candidate_key"] for p in pending]

    state = AutoState(ws)
    stage_score_papers(
        ws,
        brain=CannedScoreBrain({key: 45 for key in keys}),
        state=state,
        batch_size=10,
    )
    build_workspace(ws)
    stage_resolve_boundary(
        ws,
        brain=CannedScoreBrain({key: 5 for key in keys}),
        state=state,
        batch_size=10,
    )
    build_workspace(ws)

    resolved = sorted((ws / ".papercompass" / "reviews").glob("review_decisions_resolved_*.jsonl"))[-1]
    decisions = {
        row["title"]: row
        for row in iter_jsonl(resolved)
        if isinstance(row, dict) and row.get("title") in {"Beta paper two", "Gamma paper three"}
    }
    assert decisions["Beta paper two"]["decision"] == "anchor"
    assert decisions["Beta paper two"]["action"] == "required_seed_anchor"
    assert decisions["Gamma paper three"]["decision"] == "accept"
    assert decisions["Gamma paper three"]["action"] == "required_seed_main"

    papers = read_json(ws / "data" / "papers.json", [])
    anchors = read_json(ws / "data" / "anchor_papers.json", [])
    rejected = read_json(ws / "data" / "rejected_candidates.json", [])
    assert "Gamma paper three" in {p["title"] for p in papers}
    assert "Beta paper two" in {p["title"] for p in anchors}
    assert not ({"Beta paper two", "Gamma paper three"} & {p["title"] for p in rejected})


def test_score_papers_retries_missing_batch_scores(tmp_path: Path):
    from papercompass.auto.stages import stage_score_papers
    from papercompass.build import build_workspace
    from papercompass.text import read_json

    ws = _setup_workspace(tmp_path)
    build_workspace(ws)
    pending = read_json(ws / "data" / "pending_review_candidates.json", [])
    keys = [p["candidate_key"] for p in pending]
    brain = OmitFirstScoreBrain({k: 90 for k in keys})

    state = AutoState(ws)
    result = stage_score_papers(
        ws,
        brain=brain,
        state=state,
        batch_size=10,
    )

    assert result["status"] == "completed"
    assert result["brain_missing_scores"] == 0
    assert len(brain.calls) == 2


class FailAfterNBatchesBrain(BrainPlugin):
    """Score normally for the first N batches, then raise. Used to simulate
    a mid-stage codex / opencode kill so the resume-from-partial-cache path
    can be tested."""

    name = "stub-fail-after"
    display = "stub brain that fails after N batches"

    def __init__(self, scores_by_key: dict[str, int], fail_after: int) -> None:
        self.scores_by_key = scores_by_key
        self.fail_after = fail_after
        self.batches = 0

    @classmethod
    def is_available(cls):
        return True

    def ask(self, prompt, *, schema=None, **kwargs):
        self.batches += 1
        if self.batches > self.fail_after:
            raise RuntimeError("simulated mid-stage kill")
        keys_in_prompt = []
        for line in prompt.splitlines():
            if line.startswith("- candidate_key:"):
                k = line.split(":", 1)[1].strip()
                keys_in_prompt.append(k)
        rows = [
            {"candidate_key": k, "score": int(self.scores_by_key.get(k, 50)), "reason": "stub"}
            for k in keys_in_prompt
        ]
        payload = {"scores": rows}
        text = json.dumps(payload)
        return BrainResponse(
            text=text, parsed=payload, raw_stdout=text, raw_stderr="",
            plugin=self.name, duration_seconds=0.0, extra={},
        )


def test_score_papers_resumes_from_partial_cache(tmp_path: Path):
    """When a stage dies mid-run (codex / opencode SIGKILL after batch N),
    a re-run must pick up from the partial cache instead of re-paying brain
    cost on the already-scored batches."""
    from papercompass.auto.stages import stage_score_papers
    from papercompass.build import build_workspace
    from papercompass.text import read_json

    ws = _setup_workspace(tmp_path)
    # Add more papers so we have multiple batches.
    raw = ws / ".raw" / "test" / "2024.jsonl"
    rows = raw.read_text(encoding="utf-8").splitlines()
    for i in range(3, 12):
        rows.append(json.dumps({
            "source_name": "test", "source_type": "test",
            "raw": {"title": f"Alpha paper {i}", "year": 2024,
                    "abstract": "alpha methods deep dive",
                    "arxiv_id": f"2401.{i:05d}"},
        }))
    raw.write_text("\n".join(rows), encoding="utf-8")

    build_workspace(ws)
    pending = read_json(ws / "data" / "pending_review_candidates.json", [])
    keys = [p["candidate_key"] for p in pending]
    scores = {k: 90 for k in keys}

    # Run 1: brain dies after 1 batch (=2 keys) — stage propagates the error.
    flaky = FailAfterNBatchesBrain(scores, fail_after=1)
    state = AutoState(ws)
    with pytest.raises(RuntimeError, match="simulated mid-stage kill"):
        stage_score_papers(ws, brain=flaky, state=state, batch_size=2,
                           max_batches=10)
    partial_path = ws / ".papercompass" / "auto" / "score_papers_partial.jsonl"
    assert partial_path.exists(), "stage_score_papers should have written a partial cache"
    cached = [
        json.loads(line)
        for line in partial_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(cached) >= 2, f"first batch's keys should be cached, got {cached}"
    assert all(row.get("cache_key") for row in cached)
    v2_cache = ws / ".papercompass" / "cache" / "review" / "brain_scores.v2.jsonl"
    assert v2_cache.exists()

    # Run 2: a fresh AutoState (state.stage_done would otherwise short-circuit).
    state2 = AutoState(ws)
    state2.set("stages", {})
    healthy = FailAfterNBatchesBrain(scores, fail_after=100)
    result = stage_score_papers(ws, brain=healthy, state=state2, batch_size=2,
                                max_batches=10)
    assert result["status"] == "completed"
    # Healthy brain must have done strictly fewer batches than total — the
    # cached batch was skipped (we don't re-pay for it).
    n_keys = len(pending)
    total_batches = (n_keys + 1) // 2
    assert healthy.batches
    assert healthy.batches < total_batches, (
        f"resume should skip cached batches; brain was called "
        f"{healthy.batches} times for {n_keys} keys / batch_size=2 "
        f"(total batches={total_batches})"
    )
    # Partial cache cleaned up after a clean completion.
    assert not partial_path.exists(), "partial cache should be cleaned after a clean run"


def test_resolve_boundary_promotes_or_rejects_via_metadata(tmp_path: Path):
    """When a boundary paper has strong metadata anchors but mid brain
    score, resolve_boundary's metadata fallback should still place it
    decisively."""
    from papercompass.auto.stages import (
        stage_resolve_boundary,
        stage_score_papers,
    )
    from papercompass.build import build_workspace
    from papercompass.text import read_json

    ws = _setup_workspace(tmp_path)
    build_workspace(ws)
    pending = read_json(ws / "data" / "pending_review_candidates.json", [])
    keys = [p["candidate_key"] for p in pending]
    # Force everything to boundary (45) so resolve_boundary has work to do
    scores = {k: 45 for k in keys}

    state = AutoState(ws)
    stage_score_papers(
        ws, brain=CannedScoreBrain(scores), state=state, batch_size=10
    )
    # Second-pass brain returns clear in/out for each
    second_scores = {keys[0]: 80, keys[1]: 30, keys[2]: 50}
    result = stage_resolve_boundary(
        ws,
        brain=CannedScoreBrain(second_scores),
        state=state,
        batch_size=10,
    )
    assert result["status"] == "completed"
    final = result["final_counts"]
    # Every boundary must end up in either in_scope or out_of_scope
    assert sum(final.values()) == result["boundary_total"]
    assert "in_scope" in final
    assert "out_of_scope" in final
    reflections = ws / ".papercompass" / "cache" / "review" / "reflections.v1.jsonl"
    assert reflections.exists()
    assert "initial_review" in reflections.read_text(encoding="utf-8")
    row = json.loads(reflections.read_text(encoding="utf-8").splitlines()[0])
    assert row["cache_key"]
    assert row["prompt_version"] == "papercompass.reflection.v1"
    assert row["schema_version"] == "papercompass.reflection_schema.v1"
    assert row["policy_version"] == "papercompass.boundary_reflection_policy.v1"


def test_resolve_boundary_skips_reflection_for_clear_first_brain(tmp_path: Path):
    from papercompass.auto.stages import (
        stage_resolve_boundary,
        stage_score_papers,
    )
    from papercompass.build import build_workspace
    from papercompass.text import read_json

    ws = _setup_workspace(tmp_path)
    build_workspace(ws)
    pending = read_json(ws / "data" / "pending_review_candidates.json", [])
    keys = [p["candidate_key"] for p in pending]

    state = AutoState(ws)
    stage_score_papers(
        ws,
        brain=CannedScoreBrain({key: 65 for key in keys}),
        state=state,
        batch_size=10,
    )
    second_brain = CannedScoreBrain({key: 5 for key in keys})
    result = stage_resolve_boundary(
        ws,
        brain=second_brain,
        state=state,
        batch_size=10,
    )

    assert result["status"] == "completed"
    assert result["boundary_total"] >= 1
    assert result["reflection_count"] == 0
    assert result["direct_resolved_count"] == result["boundary_total"]
    assert second_brain.calls == []
    assert result["final_counts"]["in_scope"] == result["boundary_total"]
    reflections = ws / ".papercompass" / "cache" / "review" / "reflections.v1.jsonl"
    assert not reflections.exists()


def test_claude_plugin_detects_not_logged_in():
    """ClaudePlugin should treat the JSON-wrapper is_error=true response
    (e.g. 'Not logged in') as BrainInvocationError, not silent success."""
    from papercompass.plugins.brain import (
        BrainInvocationError,
        BrainResponse,
        ClaudePlugin,
    )
    import subprocess

    # Build a fake completed subprocess that simulates `claude -p ...` when
    # the user isn't logged in: returncode 0, JSON wrapper with is_error true.
    fake_stdout = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "Not logged in · Please run /login",
        "duration_ms": 36,
    })

    class FakeProc:
        returncode = 0
        stdout = fake_stdout
        stderr = ""

    plugin = ClaudePlugin()
    original_is_available = ClaudePlugin.is_available
    ClaudePlugin.is_available = classmethod(lambda cls: True)
    original_run = subprocess.run
    subprocess.run = lambda *a, **kw: FakeProc()  # type: ignore[assignment]
    try:
        with pytest.raises(BrainInvocationError, match="not logged in|Not logged in|is_error"):
            plugin._ask_once("test prompt", schema={"type": "object"})
    finally:
        subprocess.run = original_run
        ClaudePlugin.is_available = original_is_available

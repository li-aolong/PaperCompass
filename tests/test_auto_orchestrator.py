"""Integration smoke tests for the auto-build orchestrator that exercise the
deterministic glue without making any brain CLI call. Ensures:

- the public stage functions are importable and their signatures stay stable
- AutoState correctly checkpoints stage start / end and survives reload
- a "dry" auto-build flow with a mock brain plugin produces the expected
  topic.yaml / sources.yaml / state structure
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from papercompass.auto.state import AutoState
from papercompass.plugins import BrainPlugin, BrainResponse


class StubBrain(BrainPlugin):
    """In-memory brain that returns a canned plan when asked. Used to drive
    the orchestrator end-to-end without spending any real CLI tokens."""

    name = "stub"
    display = "stub-brain (test fixture)"
    canned: dict | None = None

    def __init__(self, canned: dict | None = None) -> None:
        self.canned = canned or {}

    @classmethod
    def is_available(cls) -> bool:  # pragma: no cover - trivial
        return True

    def ask(self, prompt: str, *, schema=None, context_files=None, timeout=600, **kwargs):
        return BrainResponse(
            text=json.dumps(self.canned),
            parsed=self.canned,
            raw_stdout=json.dumps(self.canned),
            raw_stderr="",
            plugin=self.name,
            duration_seconds=0.0,
            extra={"returncode": 0},
        )


def test_state_checkpoint_round_trip(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / ".papercompass").mkdir(parents=True)
    s = AutoState(ws)
    s.begin_stage("plan_direction", direction="X")
    s.end_stage("plan_direction", topic_id="x", strong_count=10)
    # reload from disk
    s2 = AutoState(ws)
    assert s2.stage_status("plan_direction") == "completed"
    assert s2.stage_done("plan_direction") is True
    assert s2.data["stages"]["plan_direction"]["topic_id"] == "x"


def test_stage_plan_direction_writes_yaml(tmp_path: Path, monkeypatch):
    from papercompass.auto.stages import stage_plan_direction

    monkeypatch.setenv("PAPERCOMPASS_SKIP_SEED_SEARCH", "1")
    monkeypatch.setenv("PAPERCOMPASS_SKIP_SEED_VERIFY", "1")
    ws = tmp_path / "ws"
    (ws / ".papercompass").mkdir(parents=True)
    canned = {
        "topic_id": "stub-topic",
        "name": "Stub topic",
        "description": "for tests",
        "min_year": 2024,
        "search_hints": [
            "alpha decoding",
            "beta sampling",
            "gamma extension",
            "stub research method",
            "alpha-beta acceleration",
            "draft and verify alpha",
        ],
        "search_keyword_text": (
            "Alpha decoding, beta sampling, draft-and-verify alpha-beta methods, "
            "alpha-beta lossless inference acceleration, gamma stage extensions, "
            "stub research methods for inference, alpha decoding tree, beta sampling "
            "draft model verification."
        ),
        "judge_examples": {
            "in_scope": [
                {"title": "Alpha decoding paper", "reason": "core method"},
                {"title": "Beta sampling overview", "reason": "core method"},
                {"title": "Draft model alpha-beta", "reason": "core extension"},
            ],
            "out_of_scope": [
                {"title": "Off-topic 1", "reason": "wrong field"},
                {"title": "Off-topic 2", "reason": "different topic"},
                {"title": "Off-topic 3", "reason": "unrelated"},
            ],
        },
        "seed_papers": [
            {"title": "An alpha decoding paper", "year": 2024, "arxiv_id": "2401.00001", "doi": "", "why_seed": "core", "paper_role": "core_method", "required": True},
            {"title": "Beta sampling overview", "year": 2024, "arxiv_id": "2401.00002", "doi": "", "why_seed": "core", "paper_role": "core_method", "required": True},
            {"title": "Stub anchor paper", "year": 2024, "arxiv_id": "2401.00003", "doi": "", "why_seed": "anchor", "paper_role": "background_anchor", "required": True},
        ],
    }
    state = AutoState(ws)
    result = stage_plan_direction(
        ws,
        "alpha + beta methods",
        min_year=2024,
        brain=StubBrain(canned),
        state=state,
    )
    assert result["status"] == "completed"
    assert (ws / "topic.yaml").exists()
    assert (ws / "sources.yaml").exists()
    seeds_path = ws / ".papercompass" / "plans" / "seed_papers.jsonl"
    assert seeds_path.exists()
    # checkpoint persists
    assert state.stage_done("plan_direction")
    # idempotent: a second call short-circuits to "cached"
    result2 = stage_plan_direction(
        ws,
        "alpha + beta methods",
        min_year=2024,
        brain=StubBrain(canned),
        state=state,
    )
    assert result2["status"] == "cached"


def test_stage_plan_direction_can_pin_topic_id_to_workspace_contract(tmp_path: Path, monkeypatch):
    from papercompass.auto.stages import stage_plan_direction
    from papercompass.config import load_topic_config

    monkeypatch.setenv("PAPERCOMPASS_SKIP_SEED_SEARCH", "1")
    monkeypatch.setenv("PAPERCOMPASS_SKIP_SEED_VERIFY", "1")
    ws = tmp_path / "implicit-chain-of-thought--2022plus"
    (ws / ".papercompass").mkdir(parents=True)
    canned = {
        "topic_id": "brain-picked-name",
        "name": "Implicit CoT",
        "description": "for tests",
        "min_year": 2022,
        "search_hints": [
            "implicit chain of thought",
            "latent reasoning",
            "chain of continuous thought",
        ],
        "search_keyword_text": "implicit chain-of-thought latent reasoning",
        "judge_examples": {
            "in_scope": [
                {"title": "Implicit CoT", "reason": "core"},
                {"title": "Latent reasoning", "reason": "core"},
                {"title": "Continuous thought", "reason": "core"},
            ],
            "out_of_scope": [
                {"title": "Prompt engineering", "reason": "too broad"},
                {"title": "Agent workflow", "reason": "off topic"},
                {"title": "Robot planning", "reason": "off topic"},
            ],
        },
        "seed_papers": [
            {"title": "Implicit CoT", "year": 2024, "why_seed": "core", "paper_role": "core_method", "required": True},
            {"title": "Latent reasoning", "year": 2024, "why_seed": "core", "paper_role": "core_method", "required": True},
            {"title": "Continuous thought", "year": 2024, "why_seed": "core", "paper_role": "core_method", "required": True},
        ],
    }
    state = AutoState(ws)

    result = stage_plan_direction(
        ws,
        "implicit chain-of-thought",
        min_year=2022,
        brain=StubBrain(canned),
        state=state,
        topic_id_override="implicit-chain-of-thought",
    )

    assert result["topic_id"] == "implicit-chain-of-thought"
    assert load_topic_config(ws)["topic_id"] == "implicit-chain-of-thought"


def test_seed_repair_adds_source_backed_direct_id_seed_without_brain_call(tmp_path: Path):
    from papercompass.auto.stages import stage_seed_repair
    from papercompass.config import init_workspace

    class RaisingBrain(BrainPlugin):
        name = "raise"
        display = "raise"

        @classmethod
        def is_available(cls) -> bool:
            return True

        def ask(self, *args, **kwargs):
            raise AssertionError("deterministic seed repair should not call brain")

    ws = tmp_path / "ws"
    init_workspace(ws, "seed-topic")
    (ws / "topic.yaml").write_text(
        "topic_id: seed-topic\nsearch_hints:\n  - small language model\n",
        encoding="utf-8",
    )
    (ws / "sources.yaml").write_text(
        "discovery:\n  arxiv:\n    queries: []\n",
        encoding="utf-8",
    )
    state = AutoState(ws)

    result = stage_seed_repair(
        ws,
        [
            {
                "seed_index": 0,
                "title": "TinyLlama: An Open-Source Small Language Model",
                "year": 2024,
                "arxiv_id": "2301.03872",
                "why_seed": "anchor",
                "verified": True,
                "required": True,
                "evidence": {
                    "source": "arxiv",
                    "query": "id:2301.03872",
                    "source_item_id": "2301.03872",
                    "source_url": "https://arxiv.org/abs/2301.03872",
                    "match_type": "arxiv_id_exact",
                },
            }
        ],
        brain=RaisingBrain(),
        state=state,
    )

    assert result["added_papers"] == 1
    assert result["deterministic_added"] == 1
    raw_files = list((ws / ".raw" / "manual").glob("*auto_seed_repair_batch.jsonl"))
    assert len(raw_files) == 1
    row = json.loads(raw_files[0].read_text(encoding="utf-8").strip())
    assert row["source_type"] == "manual"
    assert row["source_item_id"] == "2301.03872"
    assert row["raw"]["arxiv_id"] == "2301.03872"
    assert row["raw"]["year"] == 2024


def test_seed_repair_does_not_ask_brain_for_extra_queries(tmp_path: Path):
    from papercompass.auto.stages import stage_seed_repair
    from papercompass.config import init_workspace, load_sources_config

    ws = tmp_path / "ws"
    init_workspace(ws, "seed-topic")
    (ws / "topic.yaml").write_text(
        "topic_id: seed-topic\nsearch_hints:\n  - latent reasoning\n",
        encoding="utf-8",
    )
    base_queries = [f'all:"latent reasoning {idx}"' for idx in range(8)]
    (ws / "sources.yaml").write_text(
        "discovery:\n"
        "  arxiv:\n"
        "    budget_policy: auto_floor\n"
        "    max_remote_calls: 24\n"
        "    max_results: 35\n"
        "    page_size: 35\n"
        "    recent_year_count: 3\n"
        "    queries:\n"
        + "".join(f"      - {query!r}\n" for query in base_queries),
        encoding="utf-8",
    )
    state = AutoState(ws)
    brain = StubBrain({
        "actions": [
            {
                "seed_index": 0,
                "title": "Missing latent reasoning paper",
                "decision": "extra_arxiv_query",
                "query_text": 'all:"missing latent reasoning"',
                "reason": "test repair query",
            }
        ]
    })

    result = stage_seed_repair(
        ws,
        [{"seed_index": 0, "title": "Missing latent reasoning paper", "year": 2025}],
        brain=brain,
        state=state,
    )

    arxiv_cfg = load_sources_config(ws)["discovery"]["arxiv"]
    assert result["added_papers"] == 0
    assert len(result["skipped_without_direct_id"]) == 1
    assert 'all:"missing latent reasoning"' not in arxiv_cfg["queries"]
    assert arxiv_cfg["max_remote_calls"] == 24


def test_brain_plugin_registry_lists_known_names():
    from papercompass.plugins.brain import _REGISTRY  # noqa: PLC2701 — testing registry shape

    names = set(_REGISTRY)
    assert {"codex", "gemini", "claude"} <= names


def test_select_brain_requires_explicit_or_caller(monkeypatch):
    from papercompass.plugins.brain import BrainUnavailable, select_brain

    monkeypatch.delenv("PAPERCOMPASS_BRAIN", raising=False)
    monkeypatch.delenv("PAPERCOMPASS_CALLER_AGENT", raising=False)
    with pytest.raises(BrainUnavailable, match="does not choose a default agent"):
        select_brain()


def test_select_brain_uses_caller_without_registry_fallback(monkeypatch):
    import papercompass.plugins.brain as brain_mod
    from papercompass.plugins.brain import BrainUnavailable, BrainPlugin, select_brain

    class FakeUnavailableCodex(BrainPlugin):
        name = "codex"
        display = "codex"

        @classmethod
        def is_available(cls) -> bool:
            return False

    class FakeAvailableClaude(BrainPlugin):
        name = "claude"
        display = "claude"

        @classmethod
        def is_available(cls) -> bool:
            return True

    monkeypatch.setattr(
        brain_mod,
        "_REGISTRY",
        {"codex": FakeUnavailableCodex, "claude": FakeAvailableClaude},
    )
    monkeypatch.delenv("PAPERCOMPASS_BRAIN", raising=False)
    monkeypatch.setenv("PAPERCOMPASS_CALLER_AGENT", "codex")
    with pytest.raises(BrainUnavailable, match="codex.*not available"):
        select_brain()


def test_brain_plugin_retries_once_on_transient_error():
    """ask() should retry _ask_once on BrainInvocationError up to `retries`
    extra attempts, then propagate the last exception."""
    from papercompass.plugins import BrainPlugin, BrainResponse
    from papercompass.plugins.brain import BrainInvocationError

    class FlakyBrain(BrainPlugin):
        name = "flaky"
        display = "flaky"
        attempts = 0

        @classmethod
        def is_available(cls) -> bool:
            return True

        def _ask_once(self, prompt, **kwargs):
            type(self).attempts += 1
            if type(self).attempts < 2:
                raise BrainInvocationError("simulated 5xx")
            return BrainResponse(text="ok", parsed={"ok": True}, plugin="flaky")

    FlakyBrain.attempts = 0
    resp = FlakyBrain().ask("hi", retries=1)
    assert FlakyBrain.attempts == 2
    assert resp.parsed == {"ok": True}

    # Now with retries=0 the same flaky plugin must fail because attempt 1 raises.
    FlakyBrain.attempts = 0
    import pytest as _pytest
    with _pytest.raises(BrainInvocationError):
        FlakyBrain().ask("hi", retries=0)


# ─── workspace fingerprint ────────────────────────────────────────────────


def test_fingerprint_writes_on_first_run(tmp_path):
    from papercompass.auto.orchestrator import (
        _check_workspace_fingerprint,
        _fingerprint_path,
        _workspace_fingerprint,
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    fp = _workspace_fingerprint("topic A", 2022, ["arxiv"])
    _check_workspace_fingerprint(ws, fp, fresh=False)
    written = json.loads(_fingerprint_path(ws).read_text(encoding="utf-8"))
    assert written == fp


def test_fingerprint_match_passes(tmp_path):
    from papercompass.auto.orchestrator import (
        _check_workspace_fingerprint,
        _workspace_fingerprint,
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    fp = _workspace_fingerprint("topic A", 2022, ["arxiv"])
    _check_workspace_fingerprint(ws, fp, fresh=False)
    # Re-run with same inputs is fine.
    _check_workspace_fingerprint(ws, fp, fresh=False)


def test_fingerprint_mismatch_aborts(tmp_path):
    from papercompass.auto.orchestrator import (
        _check_workspace_fingerprint,
        _workspace_fingerprint,
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    _check_workspace_fingerprint(
        ws, _workspace_fingerprint("topic A", 2022, ["arxiv"]), fresh=False
    )
    with pytest.raises(RuntimeError, match="previously built with a different"):
        _check_workspace_fingerprint(
            ws, _workspace_fingerprint("topic B", 2022, ["arxiv"]), fresh=False
        )


def test_fingerprint_fresh_wipes_and_rewrites(tmp_path):
    from papercompass.auto.orchestrator import (
        _check_workspace_fingerprint,
        _fingerprint_path,
        _workspace_fingerprint,
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data").mkdir()
    (ws / "data" / "papers.json").write_text("[]", encoding="utf-8")
    (ws / "topic.yaml").write_text("topic_id: stale\n", encoding="utf-8")
    new_fp = _workspace_fingerprint("topic NEW", 2024, ["openalex"])
    _check_workspace_fingerprint(ws, new_fp, fresh=True)
    assert not (ws / "data" / "papers.json").exists()
    assert not (ws / "topic.yaml").exists()
    assert json.loads(_fingerprint_path(ws).read_text(encoding="utf-8")) == new_fp


def test_fingerprint_untracked_artifacts_require_fresh(tmp_path):
    from papercompass.auto.orchestrator import (
        _check_workspace_fingerprint,
        _workspace_fingerprint,
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data").mkdir()
    (ws / "data" / "papers.json").write_text("[]", encoding="utf-8")
    fp = _workspace_fingerprint("topic A", 2022, ["arxiv"])
    with pytest.raises(RuntimeError, match="workspace_fingerprint"):
        _check_workspace_fingerprint(ws, fp, fresh=False)
    # --fresh resolves the untracked-artifact case.
    _check_workspace_fingerprint(ws, fp, fresh=True)


def test_delivery_assessment_blocks_truncated_handoff():
    from papercompass.auto.orchestrator import _delivery_assessment

    delivery = _delivery_assessment(
        {"status": "passed", "critical": [], "warnings": []},
        [{"stage": "score_papers", "uncovered": 3}],
    )

    assert delivery["deliverable_status"] == "partial_due_to_budget"
    assert delivery["safe_for_default_llm_retrieval"] is False
    assert delivery["exit_code"] == 4
    assert "truncation:score_papers:3" in delivery["hard_reasons"]


def test_delivery_assessment_blocks_missing_brain_scores():
    from papercompass.auto.orchestrator import _delivery_assessment

    delivery = _delivery_assessment(
        {"status": "passed", "critical": [], "warnings": []},
        [],
        [],
        [{"stage": "score_papers", "missing": 1}],
    )

    assert delivery["deliverable_status"] == "needs_brain_score_repair"
    assert delivery["safe_for_default_llm_retrieval"] is False
    assert delivery["exit_code"] == 4
    assert "brain_missing_scores:score_papers:1" in delivery["hard_reasons"]


def test_delivery_assessment_blocks_hard_warnings():
    from papercompass.auto.orchestrator import _delivery_assessment

    delivery = _delivery_assessment(
        {
            "status": "warning",
            "critical": [],
            "warnings": ["score_papers_has_deferred", "recall_pool_underpowered"],
        },
        [],
    )

    assert delivery["deliverable_status"] == "partial_due_to_budget"
    assert delivery["exit_code"] == 4
    assert delivery["hard_warnings"] == [
        "score_papers_has_deferred",
        "recall_pool_underpowered",
    ]


def test_delivery_assessment_blocks_hard_environment_warnings():
    from papercompass.auto.orchestrator import _delivery_assessment

    delivery = _delivery_assessment(
        {"status": "warning", "critical": [], "warnings": []},
        [],
        ["embedding_missing_with_capped_brain_budget"],
    )

    assert delivery["deliverable_status"] == "partial_due_to_budget"
    assert delivery["exit_code"] == 4
    assert delivery["hard_environment_warnings"] == [
        "embedding_missing_with_capped_brain_budget"
    ]
    assert "environment:embedding_missing_with_capped_brain_budget" in delivery["hard_reasons"]


def test_delivery_assessment_blocks_missing_required_embedding():
    from papercompass.auto.orchestrator import _delivery_assessment

    delivery = _delivery_assessment(
        {"status": "passed", "critical": [], "warnings": []},
        [],
        ["embedding_channel_disabled", "embedding_required_missing"],
    )

    assert delivery["deliverable_status"] == "missing_required_embedding"
    assert delivery["safe_for_default_llm_retrieval"] is False
    assert delivery["exit_code"] == 4
    assert delivery["hard_environment_warnings"] == ["embedding_required_missing"]
    assert "environment:embedding_required_missing" in delivery["hard_reasons"]


def test_delivery_assessment_allows_explicit_no_embedding_caveat():
    from papercompass.auto.orchestrator import _delivery_assessment

    delivery = _delivery_assessment(
        {"status": "passed", "critical": [], "warnings": []},
        [],
        ["embedding_channel_disabled"],
    )

    assert delivery["deliverable_status"] == "passed_authoritative"
    assert delivery["safe_for_default_llm_retrieval"] is True
    assert delivery["hard_environment_warnings"] == []


def test_delivery_assessment_allows_clean_pass():
    from papercompass.auto.orchestrator import _delivery_assessment

    delivery = _delivery_assessment(
        {"status": "passed", "critical": [], "warnings": []},
        [],
    )

    assert delivery["deliverable_status"] == "passed_authoritative"
    assert delivery["safe_for_default_llm_retrieval"] is True
    assert delivery["exit_code"] == 0


def test_delivery_assessment_blocks_partial_source_coverage():
    from papercompass.auto.orchestrator import _delivery_assessment

    delivery = _delivery_assessment(
        {"status": "warning", "critical": [], "warnings": ["source_coverage_partial"]},
        [],
    )

    assert delivery["deliverable_status"] == "needs_source_retry"
    assert delivery["safe_for_default_llm_retrieval"] is False
    assert delivery["exit_code"] == 4


def test_delivery_assessment_prioritizes_seed_or_query_repair_over_source_retry_and_missing_score():
    from papercompass.auto.orchestrator import _delivery_assessment

    delivery = _delivery_assessment(
        {
            "status": "warning",
            "critical": [],
            "warnings": ["source_coverage_has_risk", "query_terms_uncovered"],
        },
        [],
        [],
        [{"stage": "score_papers", "missing": 1}],
    )

    assert delivery["deliverable_status"] == "needs_seed_or_query_repair"
    assert delivery["safe_for_default_llm_retrieval"] is False
    assert "warning:query_terms_uncovered" in delivery["hard_reasons"]
    assert "warning:source_coverage_has_risk" in delivery["hard_reasons"]
    assert "brain_missing_scores:score_papers:1" in delivery["hard_reasons"]


def test_delivery_assessment_allows_non_hard_warnings_as_caveated():
    from papercompass.auto.orchestrator import _delivery_assessment

    delivery = _delivery_assessment(
        {"status": "warning", "critical": [], "warnings": ["candidate_duplicate_entities"]},
        [],
    )

    assert delivery["deliverable_status"] == "usable_with_caveats"
    assert delivery["safe_for_default_llm_retrieval"] is False
    assert delivery["exit_code"] == 0

"""Unit tests for brain_score: per-paper 0-100 scoring with anchors."""

from __future__ import annotations

import json
from typing import Any

from papercompass.auto.brain_score import score_candidate_batch
from papercompass.plugins import BrainPlugin, BrainResponse


class StubBrain(BrainPlugin):
    name = "stub"
    display = "stub"

    def __init__(self, canned: Any) -> None:
        self.canned = canned
        self.last_prompt: str | None = None

    @classmethod
    def is_available(cls):
        return True

    def ask(self, prompt, *, schema=None, **kwargs):
        self.last_prompt = prompt
        if isinstance(self.canned, Exception):
            raise self.canned
        text = json.dumps(self.canned)
        return BrainResponse(
            text=text, parsed=self.canned, raw_stdout=text, raw_stderr="",
            plugin=self.name, duration_seconds=0.0, extra={},
        )


def _candidates() -> list[dict]:
    return [
        {"candidate_key": "k1", "title": "Paper One", "year": 2024, "abstract": "abs1"},
        {"candidate_key": "k2", "title": "Paper Two", "year": 2024, "abstract": "abs2"},
    ]


def _examples() -> dict:
    return {
        "in_scope": [{"title": "Anchor In", "reason": "core"}],
        "out_of_scope": [{"title": "Anchor Out", "reason": "off topic"}],
    }


def test_score_returns_per_candidate():
    canned = {
        "scores": [
            {"candidate_key": "k1", "score": 85, "reason": "matches anchor"},
            {"candidate_key": "k2", "score": 30, "reason": "off topic"},
        ]
    }
    brain = StubBrain(canned)
    result = score_candidate_batch(
        brain, _candidates(),
        direction_raw="My direction", judge_examples=_examples(),
    )
    assert result["error"] is None
    assert len(result["scores"]) == 2
    assert {r["candidate_key"]: r["score"] for r in result["scores"]} == {"k1": 85, "k2": 30}


def test_score_filters_hallucinated_keys():
    canned = {
        "scores": [
            {"candidate_key": "k1", "score": 80, "reason": "ok"},
            {"candidate_key": "x", "score": 50, "reason": "hallucinated"},
        ]
    }
    result = score_candidate_batch(
        StubBrain(canned), _candidates(),
        direction_raw="d", judge_examples=_examples(),
    )
    keys = [r["candidate_key"] for r in result["scores"]]
    assert keys == ["k1"]


def test_score_clamps_to_0_100():
    canned = {"scores": [{"candidate_key": "k1", "score": 150, "reason": "x"}]}
    result = score_candidate_batch(
        StubBrain(canned), _candidates()[:1],
        direction_raw="d", judge_examples=_examples(),
    )
    assert result["scores"][0]["score"] == 100


def test_score_returns_error_on_brain_failure():
    from papercompass.plugins.brain import BrainInvocationError
    result = score_candidate_batch(
        StubBrain(BrainInvocationError("simulated 500")),
        _candidates(),
        direction_raw="d",
        judge_examples=_examples(),
    )
    assert result["error"] and "brain failure" in result["error"]
    assert result["scores"] == []


def test_score_returns_error_when_unparseable():
    """Brain returns dict without 'scores' field."""
    result = score_candidate_batch(
        StubBrain({"wrong_key": []}),
        _candidates(),
        direction_raw="d",
        judge_examples=_examples(),
    )
    assert result["scores"] == []
    assert result["error"]


def test_score_prompt_contains_direction_and_examples():
    brain = StubBrain({"scores": []})
    score_candidate_batch(
        brain, _candidates(),
        direction_raw="My research direction here",
        judge_examples=_examples(),
        publication_scope="strict ACL or arXiv scope",
    )
    prompt = brain.last_prompt or ""
    assert "My research direction here" in prompt
    assert "Anchor In" in prompt
    assert "Anchor Out" in prompt
    assert "strict ACL or arXiv scope" in prompt

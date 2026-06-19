"""Smoke tests for embed module. Skipped when sentence-transformers absent."""

import pytest

pytest.importorskip("sentence_transformers")

from papercompass.auto.embed import (
    candidate_text,
    cosine_similarity_normalized,
    is_available,
    score_candidates_against_topic,
)


def test_is_available_when_lib_installed():
    assert isinstance(is_available(), bool)


def test_score_candidates_against_topic_returns_correct_shape():
    if not is_available():
        pytest.skip("embedding model cache is not available")
    candidates = [
        {"title": "Speculative decoding for LLM inference",
         "abstract": "We accelerate LLM decoding with draft-and-verify."},
        {"title": "Octopus reef ecology study",
         "abstract": "Marine biology of cephalopods."},
    ]
    target = "speculative decoding LLM inference acceleration draft model"
    scores = score_candidates_against_topic(candidates, target)
    assert len(scores) == 2
    # speculative decoding match should score higher than octopus reef
    assert scores[0] > scores[1]
    # all in 0-100
    assert all(0.0 <= s <= 100.0 for s in scores)


def test_score_candidates_handles_empty_inputs():
    assert score_candidates_against_topic([], "anything") == []
    # Empty target = no signal. None propagates so fuse_scores can
    # redistribute the embedding weight; 0.0 would silently bias fusion low.
    scores = score_candidates_against_topic([{"title": "x", "abstract": "y"}], "")
    assert scores == [None]


def test_candidate_text_caps_long_abstracts():
    huge = "x " * 5000
    s = candidate_text({"title": "T", "abstract": huge})
    assert len(s) < 2000


def test_cosine_zero_for_mismatched_lengths():
    assert cosine_similarity_normalized([1.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity_normalized([], []) == 0.0

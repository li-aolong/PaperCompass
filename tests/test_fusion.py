"""Unit tests for the v3 three-channel fusion."""

from papercompass.auto.fusion import (
    BoundaryThresholds,
    FusionThresholds,
    FusionWeights,
    fuse_and_verdict,
    fuse_and_verdict_with_policy,
    fuse_scores,
    resolve_boundary,
    verdict_from_score,
)


def test_fuse_scores_simple_weighted_average():
    s = fuse_scores(50.0, 50.0, 50.0, FusionWeights(0.3, 0.5, 0.2))
    assert s == 50.0


def test_fuse_scores_redistributes_weight_when_channel_missing():
    """When emb is None (e.g. no sentence-transformers), weight redistributes
    to the available channels proportionally."""
    s = fuse_scores(None, 80.0, 20.0, FusionWeights(0.3, 0.5, 0.2))
    # available weights 0.5 + 0.2 = 0.7 → emb=80 weight 0.5/0.7, meta=20 weight 0.2/0.7
    expected = (80.0 * 0.5 + 20.0 * 0.2) / 0.7
    assert abs(s - round(expected, 2)) < 0.05


def test_fuse_scores_zero_when_all_missing():
    assert fuse_scores(None, None, None) == 0.0


def test_fusion_weights_normalize_when_unsumming():
    w = FusionWeights(2.0, 4.0, 4.0).normalize()
    assert abs(w.alpha_emb + w.beta_brain + w.gamma_meta - 1.0) < 1e-6


def test_verdict_thresholds():
    t = FusionThresholds(tau_high=70.0, tau_low=40.0)
    assert verdict_from_score(85.0, t) == "in_scope"
    assert verdict_from_score(70.0, t) == "in_scope"
    assert verdict_from_score(60.0, t) == "boundary"
    assert verdict_from_score(40.0, t) == "boundary"
    assert verdict_from_score(20.0, t) == "out_of_scope"


def test_default_thresholds_aligned_with_strict_prompt():
    """tau_high default should match BRAIN_SCORE_PROMPT's '≥75 = clearly in
    scope' rubric so the build verdict line matches the audit standard."""
    t = FusionThresholds()
    assert t.tau_high == 75.0
    assert verdict_from_score(74.0, t) == "boundary"
    assert verdict_from_score(75.0, t) == "in_scope"


def test_fuse_and_verdict_returns_full_record():
    rec = fuse_and_verdict(80.0, 70.0, 50.0)
    assert "score" in rec
    assert "verdict" in rec
    assert rec["emb"] == 80.0
    assert rec["brain"] == 70.0


def test_fusion_policy_keeps_mid_brain_as_boundary_without_embedding():
    rec = fuse_and_verdict_with_policy(None, 45.0, 10.0)
    assert rec["score"] < FusionThresholds().tau_low
    assert rec["verdict"] == "boundary"
    assert rec["policy"] == "brain_mid_boundary_floor"


def test_fusion_policy_promotes_clear_brain_score():
    rec = fuse_and_verdict_with_policy(10.0, 80.0, 10.0)
    assert rec["verdict"] == "in_scope"
    assert rec["policy"] == "brain_clear_in_scope"


def test_fusion_policy_does_not_boundary_unscored_tail():
    rec = fuse_and_verdict_with_policy(95.0, None, 60.0)
    assert rec["score"] >= FusionThresholds().tau_high
    assert rec["verdict"] == "out_of_scope"
    assert rec["policy"] == "no_brain_conservative_drop"


def test_resolve_boundary_strong_brain_promotes_in_scope():
    assert resolve_boundary({}, 75.0, 30.0) == "in_scope"  # second-pass strong
    assert resolve_boundary({"brain_score": 80.0}, None, 30.0) == "in_scope"  # first-pass strong


def test_resolve_boundary_low_brain_rejects_even_with_high_metadata():
    """A brain score ≤59 (boundary range) is treated as evidence against
    the paper, not as ambiguity. High metadata alone cannot rescue it."""
    # second-pass low brain + high meta → out (was incorrectly in_scope)
    assert resolve_boundary({"brain_score": 25.0}, None, 100.0) == "out_of_scope"
    # second-pass low brain regardless of meta
    assert resolve_boundary({}, 30.0, 100.0) == "out_of_scope"


def test_resolve_boundary_mid_brain_needs_strong_metadata():
    """Mid-brain (40-59) only flips to in_scope with metadata ≥75 —
    multi-source agreement plus citation plus venue match."""
    assert resolve_boundary({"brain_score": 50.0}, None, 80.0) == "in_scope"  # mid + strong meta
    assert resolve_boundary({"brain_score": 50.0}, None, 60.0) == "out_of_scope"  # mid + weak meta
    assert resolve_boundary({}, 50.0, 80.0) == "in_scope"  # second-pass mid + strong meta


def test_resolve_boundary_no_brain_at_all_is_out():
    """Boundary paper that no brain ever scored → reject (conservative)."""
    assert resolve_boundary({}, None, 80.0) == "out_of_scope"
    assert resolve_boundary({"brain_score": None}, None, 100.0) == "out_of_scope"


def test_resolve_boundary_actually_uses_thresholds():
    """Regression: thresholds parameter used to be silently ignored.

    With brain_promote=80, a brain score of 70 must NOT promote (was
    hardcoded to 60 and would have flipped to in_scope).
    """
    strict = BoundaryThresholds(brain_promote=80.0, brain_mid_low=50.0,
                                meta_promote_floor=90.0)
    # 70 < 80 = no longer "decisive in"; 70 in [50, 80) = mid; meta 50 < 90 = drop
    assert resolve_boundary({"brain_score": 70.0}, None, 50.0,
                            thresholds=strict) == "out_of_scope"
    # Default thresholds: 70 ≥ 60 → in
    assert resolve_boundary({"brain_score": 70.0}, None, 50.0) == "in_scope"
    # Strict: meta high enough now lifts mid-brain
    assert resolve_boundary({"brain_score": 70.0}, None, 95.0,
                            thresholds=strict) == "in_scope"

"""Three-channel fusion + verdict for v3 paper scoring.

Combines independent 0-100 scores from:
  - emb (sentence-transformer cosine similarity)
  - brain (LLM judge with in/out anchors)
  - meta (multi-source / citation / venue / freshness)

into a single fused score, then a verdict via two thresholds:
  score ≥ tau_high          → in_scope
  tau_low ≤ score < tau_high → boundary  (resolved by 2nd-pass brain or metadata)
  score < tau_low            → out_of_scope

Defaults from a small grid-search baseline; the calibrate command can
override per workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FusionWeights:
    """Per-channel weights. Sum to 1.0; missing channel score gets 0.

    Defaults: brain dominates (β=0.65), embedding is the safety-net recall
    signal (α=0.20), metadata is a tiebreaker (γ=0.15). The intent-fusion
    zhihu writeup tunes α even lower (0.10) when LLM is reliable; we keep
    α a bit higher because PaperCompass calls brain on far fewer papers
    than the embedding sees.
    """

    alpha_emb: float = 0.20
    beta_brain: float = 0.65
    gamma_meta: float = 0.15

    def normalize(self) -> "FusionWeights":
        s = self.alpha_emb + self.beta_brain + self.gamma_meta
        if s <= 0:
            return FusionWeights(0.34, 0.33, 0.33)
        return FusionWeights(
            self.alpha_emb / s, self.beta_brain / s, self.gamma_meta / s
        )


@dataclass
class FusionThresholds:
    # tau_high aligned with BRAIN_SCORE_PROMPT's "≥75 = clearly in scope"
    # rubric so build verdicts match the audit-strictness standard.
    tau_high: float = 75.0
    tau_low: float = 40.0


@dataclass
class BoundaryThresholds:
    """Thresholds for resolve_boundary's brain-vs-metadata heuristic.

    These act on raw brain scores (0-100), NOT the fused channel score.
    Distinct from FusionThresholds, which gates fused-score → verdict.
    Brain dominates: a paper with brain ≥ brain_promote skips meta entirely.
    Mid-brain papers (brain in [brain_mid_low, brain_promote)) need
    metadata ≥ meta_promote_floor to flip in. No-brain papers are dropped
    (current policy: meta alone is too noisy to anchor precision).
    """

    brain_promote: float = 60.0
    brain_mid_low: float = 40.0
    meta_promote_floor: float = 75.0


def fuse_scores(
    emb: float | None,
    brain: float | None,
    meta: float | None,
    weights: FusionWeights | None = None,
) -> float:
    """Weighted average of three channel scores. None → 0 contribution.

    When a channel is unavailable (e.g. embedding library missing) its score
    is treated as 0 and its weight is redistributed proportionally to the
    other channels. Otherwise the channel weights act on actual values.
    """
    w = (weights or FusionWeights()).normalize()
    available = []
    if emb is not None:
        available.append(("emb", float(emb), w.alpha_emb))
    if brain is not None:
        available.append(("brain", float(brain), w.beta_brain))
    if meta is not None:
        available.append(("meta", float(meta), w.gamma_meta))
    if not available:
        return 0.0
    weight_sum = sum(weight for _, _, weight in available)
    if weight_sum <= 0:
        return 0.0
    return round(
        sum(score * weight / weight_sum for _, score, weight in available), 2
    )


def verdict_from_score(
    score: float, thresholds: FusionThresholds | None = None
) -> str:
    t = thresholds or FusionThresholds()
    if score >= t.tau_high:
        return "in_scope"
    if score >= t.tau_low:
        return "boundary"
    return "out_of_scope"


def fuse_and_verdict(
    emb: float | None,
    brain: float | None,
    meta: float | None,
    weights: FusionWeights | None = None,
    thresholds: FusionThresholds | None = None,
) -> dict[str, Any]:
    """Full pipeline for one paper: fuse channels then label."""
    fused = fuse_scores(emb, brain, meta, weights)
    return {
        "score": fused,
        "verdict": verdict_from_score(fused, thresholds),
        "emb": emb,
        "brain": brain,
        "meta": meta,
    }


def fuse_and_verdict_with_policy(
    emb: float | None,
    brain: float | None,
    meta: float | None,
    weights: FusionWeights | None = None,
    thresholds: FusionThresholds | None = None,
) -> dict[str, Any]:
    """Fuse channels, then apply PaperCompass's delivery policy.

    The raw weighted score is useful for ranking, but it should not silently
    discard a candidate that the brain rubric considers ambiguous. In
    particular, when the embedding channel is unavailable, a mid brain score
    plus low metadata can fall below ``tau_low`` even though it should get a
    boundary second pass. Keep the score, but floor the verdict by brain
    signal:

    - brain >= 75: clearly in scope by the judge rubric
    - 40 <= brain < 75: at least boundary, unless fused already promoted it
    """
    result = fuse_and_verdict(emb, brain, meta, weights, thresholds)
    if brain is None:
        if result["verdict"] != "out_of_scope":
            result["verdict"] = "out_of_scope"
            result["policy"] = "no_brain_conservative_drop"
        else:
            result["policy"] = "fused"
        return result
    brain_val = float(brain)
    if brain_val >= 75.0:
        result["verdict"] = "in_scope"
        result["policy"] = "brain_clear_in_scope"
    elif brain_val >= 40.0 and result["verdict"] == "out_of_scope":
        result["verdict"] = "boundary"
        result["policy"] = "brain_mid_boundary_floor"
    else:
        result["policy"] = "fused"
    return result


def resolve_boundary(
    paper_score: dict[str, Any],
    second_brain_score: float | None,
    metadata_score_value: float,
    thresholds: BoundaryThresholds | None = None,
) -> str:
    """Force a boundary paper to in_scope or out_of_scope.

    Brain dominates. A first- or second-brain score ≥ brain_promote (default
    60) flips to in_scope; mid-brain (in [brain_mid_low, brain_promote))
    flips only if metadata ≥ meta_promote_floor; no-brain papers always
    drop (metadata alone is too noisy to anchor precision per audit data).

    Always returns "in_scope" or "out_of_scope" — never "boundary".
    """
    t = thresholds or BoundaryThresholds()
    # Field name accommodates both `brain` and `brain_score` shapes.
    if isinstance(paper_score, dict):
        first_brain = paper_score.get("brain", paper_score.get("brain_score"))
    else:
        first_brain = None

    if second_brain_score is not None and second_brain_score >= t.brain_promote:
        return "in_scope"
    if first_brain is not None and first_brain >= t.brain_promote:
        return "in_scope"
    mid_brain_signal = (
        (second_brain_score is not None and t.brain_mid_low <= second_brain_score < t.brain_promote)
        or (first_brain is not None and t.brain_mid_low <= first_brain < t.brain_promote)
    )
    if mid_brain_signal and metadata_score_value >= t.meta_promote_floor:
        return "in_scope"
    return "out_of_scope"

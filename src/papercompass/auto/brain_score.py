"""LLM judge channel: per-paper 0-100 relevance scoring with in/out anchors.

Brain returns a numeric score so fusion can weight it against embedding
and metadata channels uniformly.

Failure modes are tolerated: BrainInvocationError → score=None for the
batch (fusion treats None as no-data, redistributes weight). Hallucinated
candidate_keys are filtered out via lint.filter_hallucinated_decisions.
"""

from __future__ import annotations

from typing import Any

from ..plugins.brain import BrainInvocationError, BrainPlugin
from ..roles import CORE_METHOD, normalize_role
from .lint import filter_hallucinated_decisions
from .prompts import BRAIN_SCORE_PROMPT, brain_score_schema, render_judge_examples


def _render_candidate_block(batch: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for cand in batch:
        if not isinstance(cand, dict):
            continue
        ck = (cand.get("candidate_key") or "").strip() or "?"
        title = (cand.get("title") or "").strip()
        year = cand.get("year") or ""
        venue = (cand.get("venue") or "").strip()
        abstract = (cand.get("abstract") or cand.get("summary") or "").strip()
        if len(abstract) > 800:
            abstract = abstract[:800] + "…"
        lines.append(
            f"- candidate_key: {ck}\n"
            f"  title: {title}\n"
            f"  year: {year}  venue: {venue}\n"
            f"  abstract: {abstract}"
        )
    return "\n".join(lines)


def score_candidate_batch(
    brain: BrainPlugin,
    candidates: list[dict[str, Any]],
    *,
    direction_raw: str,
    judge_examples: dict[str, list[dict[str, Any]]],
    publication_scope: str = "",
    timeout: int = 600,
) -> dict[str, Any]:
    """Score one batch (≤30) of candidates. Returns:
        {
          "scores": [{"candidate_key", "score" (int 0-100), "reason"}],
          "error": str | None,
          "raw_text": str,
          "duration": float,
          "plugin": str,
        }

    On brain failure or unparseable output, scores=[] and error is set.
    Caller should treat missing-score candidates as None for fusion (NOT 0).
    """
    in_scope = render_judge_examples(judge_examples.get("in_scope") or [], "in_scope")
    out_scope = render_judge_examples(judge_examples.get("out_of_scope") or [], "out_of_scope")
    block = _render_candidate_block(candidates)
    prompt = BRAIN_SCORE_PROMPT.format(
        direction_raw=direction_raw,
        publication_scope=publication_scope or "未配置明确 publication/source scope。",
        in_scope_examples=in_scope,
        out_of_scope_examples=out_scope,
        batch_size=len(candidates),
        candidate_block=block,
    )
    error: str | None = None
    raw_text = ""
    duration = 0.0
    plugin_name = brain.name
    parsed: Any = None
    resp = None
    try:
        resp = brain.ask(prompt, schema=brain_score_schema(), timeout=timeout, retries=1)
        raw_text = resp.text
        duration = resp.duration_seconds
        plugin_name = resp.plugin
        parsed = resp.parsed
    except BrainInvocationError as exc:
        error = f"brain failure: {str(exc)[:300]}"

    queue_keys = {
        (c.get("candidate_key") or "").strip()
        for c in candidates
        if isinstance(c, dict) and (c.get("candidate_key") or "").strip()
    }

    scores: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        rows = parsed.get("scores") or []
        if isinstance(rows, list):
            cleaned, _ = filter_hallucinated_decisions(rows, queue_keys)
            seen: set[str] = set()
            for row in cleaned:
                key = (row.get("candidate_key") or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                try:
                    score_val = int(row.get("score"))
                except (TypeError, ValueError):
                    continue
                score_val = max(0, min(100, score_val))
                scores.append(
                    {
                        "candidate_key": key,
                        "score": score_val,
                        "paper_role": normalize_role(row.get("paper_role"), default=CORE_METHOD),
                        "reason": (row.get("reason") or "").strip()[:200],
                    }
                )

    if not scores and error is None:
        error = "brain returned no parseable scores"

    return {
        "scores": scores,
        "error": error,
        "raw_text": raw_text,
        "duration": duration,
        "plugin": plugin_name,
        # Carry brain plugin's `extra` (input_tokens / output_tokens /
        # cost_usd / model) so callers can record per-batch usage.
        "usage": dict(resp.extra) if (resp is not None and hasattr(resp, 'extra') and isinstance(resp.extra, dict)) else {},
    }

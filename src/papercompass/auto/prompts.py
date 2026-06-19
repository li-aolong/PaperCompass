"""Prompt templates and JSON schemas used by the auto-build orchestrator.

Each public function returns a (prompt_text, schema_dict) tuple. Schemas use a
dialect compatible with OpenAI's strict structured-output mode after passing
through `papercompass.plugins.brain._strict_schema`.

Design principles for these prompts:

- Always return JSON, never prose. The orchestrator parses the response and
  only logs the prose for debugging.
- Make the brain do the *semantic* work, not the *bookkeeping*. The prompts
  ask for terms / patterns / decisions, not file layouts. The orchestrator
  turns the structured output into topic.yaml / sources.yaml / decisions
  files using deterministic templates.
- Be explicit about boundaries learned from the implicit-cot-2024plus
  postmortem: strong rules require multi-condition AND, not loose OR;
  generic words like "latent" / "implicit" must not become standalone
  strong terms. Paper anchors must come from programmatic sources, not model
  memory.
"""

from __future__ import annotations

from typing import Any


# --------------------------------------------------------------------------- #
# Stage 1: direction decomposition
# --------------------------------------------------------------------------- #


PLAN_PROMPT = """\
You are designing a high-recall paper library for a research direction. The
pipeline downstream is:

  recall (broad keyword search) → embedding similarity + LLM judge + metadata
  → fused score per paper → in_scope / out_of_scope verdict.

Your job is to give the recall stage broad search hints AND give the LLM judge
clear in/out anchors. Do NOT try to compress the direction into a Boolean
keyword rule — keyword rules cannot capture "paper IS about X" vs "paper
mentions X". Trust the LLM judge for that, give it good anchors instead.

User direction (verbatim, do not paraphrase): {direction}
Year floor (papers strictly before this are excluded): {min_year}{prior_markdown_section}

Output a single JSON object with these fields:

1. topic_id, name, description, min_year — standard.

2. search_hints (6-10 strings): free-text search queries for arXiv / OpenAlex /
   Semantic Scholar. Do NOT use Boolean syntax (AND/OR/NOT). Each hint is a
   short phrase that a source's `search` field handles. Cover the direction
   with synonyms / named methods / model families:

   GOOD examples for "Small language model agents":
     - "small language model agent"
     - "compact LLM agent"
     - "on-device language model tool use"
     - "TinyLlama agent"
     - "Phi-3 agent function calling"
     - "edge LLM agent"
     - "SLM tool calling"
     - "sub-7B parameter agent"

   GOOD examples for "Speculative decoding":
     - "speculative decoding"
     - "speculative sampling"
     - "draft model verification"
     - "Medusa decoding"
     - "EAGLE speculative"

   Aim for breadth — over-recall is fine, the judge stage rejects noise.

   For cross-cutting directions, cover every major axis explicitly rather than
   only the most obvious wording. If the direction combines a METHOD axis with
   an APPLICATION/SYSTEM axis, include hints for both the direct phrase and the
   likely bridge terms between axes. Examples:
     - latent/internal/implicit reasoning + agents: include planning/action,
       tool use/RAG/coding agents, GUI/mobile/embodied/VLA agents, multi-agent
       collaboration/communication, and safety/monitorability when relevant.
     - efficient inference + serving: include decoding, batching/scheduling,
       memory/KV-cache, distributed serving, and hardware/runtime terms.
     - privacy + federated/medical: include privacy mechanisms, domain terms,
       deployment setting, and evaluation/attack terms.

   Do not force every example axis into every topic; use the axes that are
   semantically plausible for the user's direction. The goal is to avoid
   missing an entire subfield because it uses a different bridge vocabulary.

3. search_keyword_text (1 paragraph, 60-150 words): keyword-dense free text
   used as a TARGET for embedding similarity. Mix synonyms / variants /
   adjacent concepts / typical phrasing. Embedding will compute cosine
   similarity between this paragraph and each candidate's title+abstract.

   Example for "Small language model agents":
   "Small language model agents, SLM agents, compact LLM tool use, on-device
    language model agents, edge LLM, TinyLlama based agent, Phi-3 agent,
    Octopus on-device language model, function calling on small models, tool
    use with sub-7B models, mobile-side AI agents, agent capabilities of
    compact transformer models, low-resource agentic systems."

4. discriminator_terms (8-15 strings): terms / phrases you EXPECT to appear
   verbatim in the title or abstract of an on-topic paper, AND that would NOT
   appear in most off-topic papers. Used to filter the wide-recall paperlists
   venue baseline (~80k papers/year) down to the on-topic neighborhood before
   the LLM-judge stage. Pass-rule: a paper is kept if any one of these
   substrings matches title+abstract (case-insensitive).

   Pick highly specific anchors:
     - distinctive model names: "TinyLlama", "Phi-3", "Octopus v2",
       "MobileLLM", "TinyAgent"
     - acronyms unique to this topic: "SLM" (works as standalone in this
       domain), "MoE", "GEC"
     - multi-word phrases (≥3 tokens, distinctive): "small language model",
       "edge LLM", "on-device LLM", "function-calling agent"

   AVOID generic single words that match too many papers:
     - "agent" alone, "model" alone, "language" alone, "compact" alone,
       "small" alone, "tool" alone — these appear in most NLP papers and
       blow up recall.

   GOOD discriminator_terms for "Small language model agents":
     ["TinyLlama", "Phi-3", "Octopus v2", "TinyAgent", "MobileLLM",
      "small language model", "edge LLM", "on-device LLM", "SLM agent",
      "sub-7B", "function-calling agent", "tool-augmented small model"]

   GOOD for "Speculative decoding":
     ["speculative decoding", "speculative sampling", "draft model",
      "draft-then-verify", "Medusa", "EAGLE speculative", "tree attention",
      "lookahead decoding", "self-speculative"]

5. judge_examples: in/out boundary anchors for the LLM judge. Each item is
   {{"title": str, "reason": str}}. Pick REAL papers (no fabrication) or
   archetypes that are unmistakably in or out.
   - in_scope: 5-8 examples. Reason should say WHY in scope (main contribution).
   - out_of_scope: 5-8 examples. Pick papers that share keywords but are NOT
     the direction (the typical confusion pool).

   GOOD in_scope for "Small language model agents":
     {{"title": "TinyAgent: Function Calling at the Edge",
       "reason": "core contribution: SLM-based agent for tool use"}}
     {{"title": "Octopus v2: On-device language model for super agent",
       "reason": "on-device SLM agent system"}}

   GOOD out_of_scope:
     {{"title": "GPT-4 web agent",
       "reason": "agent paper but uses large model, not SLM-focused"}}
     {{"title": "Phi-3 technical report",
       "reason": "SLM training paper, no agent application"}}
     {{"title": "Survey of LLM agents (mentions small models)",
       "reason": "survey, not original SLM agent contribution"}}
     {{"title": "Octopus-inspired adhesive material",
       "reason": "name collision; no language model"}}

If you want to express "papers like X are NOT in scope", encode it as an
out_of_scope judge_example first. Do not invent extra schema fields.

Return JSON only.
"""


def plan_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "topic_id": {
                "type": "string",
                "description": "lower-case kebab-case slug, ascii only, <=40 chars",
            },
            "name": {"type": "string"},
            "description": {
                "type": "string",
                "description": "one-paragraph topic description (<= 400 chars)",
            },
            "min_year": {"type": "integer"},
            "search_hints": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "6-10 free-text query strings for source recall (no AND/OR "
                    "syntax). Cover synonyms, named methods, model families."
                ),
            },
            "search_keyword_text": {
                "type": "string",
                "description": (
                    "60-150 word keyword-dense paragraph used as embedding "
                    "similarity target. Include synonyms / variants / adjacent "
                    "concepts / typical phrasings of the direction."
                ),
            },
            "discriminator_terms": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "8-15 specific anchors expected to substring-match real "
                    "paper title/abstract. Used to filter wide-recall venue "
                    "baselines. Prefer model names / acronyms / multi-word "
                    "phrases over generic single words."
                ),
            },
            "judge_examples": {
                "type": "object",
                "required": ["in_scope", "out_of_scope"],
                "properties": {
                    "in_scope": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["title", "reason"],
                            "properties": {
                                "title": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                        },
                    },
                    "out_of_scope": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["title", "reason"],
                            "properties": {
                                "title": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                        },
                    },
                },
                "description": "Boundary anchors used by the LLM judge stage.",
            },
        },
        "required": [
            "topic_id",
            "name",
            "description",
            "min_year",
            "search_hints",
            "search_keyword_text",
            "discriminator_terms",
            "judge_examples",
        ],
    }


# --------------------------------------------------------------------------- #
# Stage 7a: weak-queue triage (large queue → propose tightening)
# --------------------------------------------------------------------------- #


BRAIN_SCORE_PROMPT = """\
Score each candidate paper 0-100 for relevance to the user's research direction.
Be STRICT: only score ≥75 if the paper is CLEARLY the kind we want — its main
contribution must directly advance the user's direction, not merely touch it.
Adjacent / partial / mentioning the topic in passing → mid score, not high.

User direction (verbatim, do NOT reinterpret): {direction_raw}

Publication/source scope:
{publication_scope}

In-scope anchors (papers that score 80-100 — they CLEARLY belong):
{in_scope_examples}

Out-of-scope anchors (papers that score 0-25 — often share keywords but
main contribution is elsewhere):
{out_of_scope_examples}

Role labels:
- core_method: the paper's main contribution is a method for the direction.
- mechanism_eval: central mechanism, faithfulness, diagnostic, benchmark, or
  analysis paper for the direction.
- background_anchor: important historical / baseline / survey / problem framing
  paper. Useful for a survey or boundary map, but not part of default retrieval.
- boundary_negative: close-looking paper that should calibrate exclusion.
- out_of_scope: ordinary reject.

Strict scoring rubric (this matches the audit standard — be precise):
- 85-100: CLEARLY in scope. Main contribution = the user's direction itself.
          Same kind as in-scope anchors above. No reasonable reviewer would
          dispute this paper belongs.
- 60-84:  Probably in scope but with caveats — one axis present but the
          other is implicit, or applied to an adjacent sub-domain. A
          reviewer might call it "boundary" rather than "in".
- 35-59:  Tangential or background-anchor. Paper touches the topic (related
          work / baseline / one component) but main contribution is elsewhere.
          NOT what the user wants in their default main library.
- 0-34:   Off-topic despite keyword overlap. Different domain that happens
          to share vocabulary, or pure survey / position paper.

If strict publication/source scope is configured, a candidate outside that
scope should score below 60 unless it is a required seed or background anchor.

Do not inflate background anchors to 75+. Give them their honest score and set
paper_role="background_anchor". The pipeline will preserve them separately.

When in doubt, score LOW (50 not 70). It's better for a paper to fall to
boundary and get caught by resolve_boundary than to silently pad the main
library with adjacent work.

Candidates ({batch_size}):
{candidate_block}

For each: candidate_key (echo back), score (integer 0-100),
paper_role, reason (≤25 words).
Return JSON only.
"""


def brain_score_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate_key": {"type": "string"},
                        "score": {"type": "integer"},
                        "paper_role": {
                            "type": "string",
                            "enum": [
                                "core_method",
                                "mechanism_eval",
                                "background_anchor",
                                "boundary_negative",
                                "out_of_scope",
                            ],
                        },
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    }


def render_judge_examples(examples: list[dict] | None, kind: str) -> str:
    """Render in_scope / out_of_scope examples as a numbered list for prompts."""
    examples = examples or []
    if not examples:
        return f"  (no {kind} anchors provided)"
    lines: list[str] = []
    for i, ex in enumerate(examples, 1):
        if not isinstance(ex, dict):
            continue
        title = (ex.get("title") or "").strip()
        reason = (ex.get("reason") or "").strip()
        if not title:
            continue
        lines.append(f"  {i}. {title}\n     reason: {reason}")
    return "\n".join(lines) if lines else f"  (no {kind} anchors provided)"


def render_candidate_block(rows: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for idx, row in enumerate(rows, 1):
        signals = row.get("topic_signal_hits") or row.get("topic_signals", {}).get(
            "topic_signal_hits", []
        )
        venue = row.get("venue") or ""
        ids = row.get("ids") or {}
        id_str = " ".join(
            filter(None, [f"arxiv:{ids.get('arxiv', '')}", f"doi:{ids.get('doi', '')}"])
        ).strip()
        snippet = (row.get("abstract") or "")[:600]
        parts.append(
            "\n".join(
                [
                    f"### candidate {idx}",
                    f"candidate_key: {row.get('candidate_key', '')}",
                    f"title: {row.get('title', '')}",
                    f"year: {row.get('year', '')}",
                    f"venue: {venue}",
                    f"ids: {id_str}",
                    f"topic_signal_hits: {', '.join(signals) if signals else '(none)'}",
                    f"abstract: {snippet}",
                ]
            )
        )
    return "\n\n".join(parts)

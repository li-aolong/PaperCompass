"""Render the brain's plan output into deterministic topic.yaml + sources.yaml
+ optional source-backed anchors.

The brain only decides terms, judge anchors, and broad source vocabulary. Paper
anchors are accepted only if code has attached programmatic source evidence.
The exact file structure is owned by code so that the same brain output always
produces the same workspace, and the topic↔sources alignment is enforced.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..anchors import write_anchor_rows
from ..config import write_yaml
from ..roles import NEGATIVE_ROLES, normalize_role, seed_has_source_evidence, seed_required
from ..scope import infer_publication_scope
from ..source_budget import ensure_arxiv_budget_floor
from ..text import clean_text


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return cleaned or "topic"


def _phrase_pattern(term: str) -> str:
    """Convert a phrase like "implicit chain-of-thought" into a regex pattern
    that tolerates whitespace and hyphens between words."""
    parts = re.split(r"[\s\-]+", term.strip().lower())
    parts = [re.escape(p) for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return rf"\b{parts[0]}\b"
    sep = r"[\s\-]+"
    return rf"\b{sep.join(parts)}\b"


def _arxiv_exact_query(term: str) -> str:
    return f'all:"{term}"'


def _arxiv_token_query(term: str) -> str:
    tokens = _distinctive_tokens(term)
    if len(tokens) < 2:
        return ""
    if len(tokens) == 2 and frozenset(tokens) in _LOW_PRECISION_BROAD_TOKEN_SETS:
        return ""
    return " AND ".join(f"all:{token}" for token in tokens[:4])


def _arxiv_query_variants(term: str) -> list[str]:
    """Return deterministic arXiv recall queries for one human phrase.

    Exact phrase queries are precise but fragile for fast-moving topics:
    "latent reasoning language model" misses "Training LLMs to reason in a
    continuous latent space". The token-AND variant keeps the user's concept
    while tolerating title wording changes.
    """
    term = _clean_phrase(term)
    if not term:
        return []
    variants: list[str] = []
    broad = _arxiv_token_query(term)
    if broad:
        variants.append(broad)
    variants.append(_arxiv_exact_query(term))
    return _dedupe_keep_order(variants)


# Generic 1-word fragments that must NOT become standalone strong terms even
# if they are the head of a longer phrase. Anything shorter / more specific
# than these is allowed.
_GENERIC_FRAGMENTS: set[str] = {
    "large language model",
    "language model",
    "large language",
    "deep learning",
    "neural network",
    "machine learning",
    "transformer model",
    "transformer based",
    "attention based",
    "decoding strategy",
    "training strategy",
    "inference strategy",
    "model inference",
    "language model decoding",
}

# Function words that should never be the only "interesting" token in a 2-word
# canonical fragment. If a 2-word fragment contains one of these, it is
# semantically useless ("draft and", "from a") and we drop it.
_STOPWORDS: set[str] = {
    "a", "an", "the", "of", "for", "from", "to", "and", "or", "but", "with",
    "without", "via", "in", "on", "at", "by", "as", "is", "are", "be",
    "this", "that", "these", "those",
}

_GENERIC_QUERY_TOKENS: set[str] = {
    "large", "language", "model", "models", "llm", "llms", "paper",
    "papers", "study", "studies", "method", "methods", "approach",
    "approaches", "system", "systems", "framework", "frameworks",
    "involve", "involves", "include", "includes", "including", "actual",
    "variants", "related", "key",
}

# Pure ML / meta phrases that must never become a *standalone* recall anchor.
# As a bare source-filter substring they flood discovery with off-topic papers
# (e.g. "scaling laws" pulled 267, "bayesian inference" 123 in the ICL build).
# They are only useful combined with the topic ("scaling laws of in-context
# learning"), which survives because only the exact bare phrase is denied.
_DENY_STANDALONE_RECALL_TERMS: set[str] = {
    "scaling laws", "scaling law", "bayesian inference", "bayesian",
    "gradient descent", "attention", "self attention", "transformer",
    "transformers", "deep learning", "machine learning", "neural network",
    "neural networks", "language model", "language models",
    "large language model", "large language models", "representation learning",
    "optimization", "generalization", "reinforcement learning",
    "meta learning", "fine tuning", "pretraining", "pre training",
    "distribution shift", "out of distribution", "in distribution",
}

_LOW_PRECISION_BROAD_TOKEN_SETS: set[frozenset[str]] = {
    frozenset({"chain", "thought"}),
    frozenset({"test", "time"}),
    frozenset({"hidden", "state"}),
    frozenset({"compressed", "reasoning"}),
    frozenset({"internalized", "reasoning"}),
    frozenset({"recurrent", "reasoning"}),
}


def _clean_phrase(value: str) -> str:
    value = (value or "").strip()
    value = value.replace("Chain-of-Thought", "chain-of-thought")
    value = value.replace("chain-of-thought", "chain thought")
    value = value.replace("Chain of Thought", "chain thought")
    value = value.replace("CoT", "cot")
    value = value.replace("LLMs", "llms").replace("LLM", "llm")
    value = re.sub(r"[^A-Za-z0-9+\-\s]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _term_tokens(term: str) -> list[str]:
    cleaned = _clean_phrase(term).lower()
    return [tok for tok in re.split(r"[\s\-]+", cleaned) if tok]


def _distinctive_tokens(term: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tok in _term_tokens(term):
        if tok in _STOPWORDS or tok in _GENERIC_QUERY_TOKENS:
            continue
        if len(tok) < 3:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _is_generic(phrase: str) -> bool:
    p = re.sub(r"\s+", " ", phrase.strip().lower())
    if p in _GENERIC_FRAGMENTS:
        return True
    words = p.split()
    if any(w in _STOPWORDS for w in words):
        return True
    if all(len(w) <= 3 for w in words):
        return True
    return False


def canonical_short_terms(terms: list[str]) -> list[str]:
    """Given the brain's strong_terms, derive the canonical 2-3 word core of
    every long term. Used to recover recall when the brain over-specifies
    (e.g. "speculative decoding large language models" → "speculative
    decoding"). Returns terms ordered by their first appearance, deduped
    case-insensitively, skipping anything in `_GENERIC_FRAGMENTS`.
    """
    seen: set[str] = set()
    out: list[str] = []

    def push(phrase: str) -> None:
        phrase = phrase.strip()
        if not phrase or _is_generic(phrase):
            return
        key = phrase.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(phrase)

    for term in terms:
        if not isinstance(term, str):
            continue
        words = _term_tokens(term)
        n = len(words)
        # always keep the original (deduped, generic-filtered)
        push(term)
        # for long phrases, also rescue the leading 2-word core. We only do
        # the leading window because that's typically the canonical name in
        # CS paper writing ("speculative decoding [large language models]"
        # not "[some adjective] speculative decoding").
        if n >= 4:
            push(" ".join(words[:2]))
        if "chain" in words and "thought" in words:
            push("chain thought")
    return out


def _ss_query(term: str) -> str:
    return _clean_phrase(term)


def _keyword_phrase_candidates(keyword_text: str) -> list[str]:
    """Extract cheap query candidates from the embedding paragraph.

    The brain may put useful method names in search_keyword_text instead of
    search_hints. This parser deliberately stays simple and deterministic:
    only explicit comma / semicolon / line-separated phrases are promoted.
    """
    out: list[str] = []

    def push(phrase: str) -> None:
        phrase = _clean_phrase(phrase)
        if not phrase or _is_generic(phrase):
            return
        if len(phrase.split()) > 5:
            return
        if phrase.lower() not in {x.lower() for x in out}:
            out.append(phrase)

    for chunk in re.split(r"[,;。\n]+", keyword_text or ""):
        push(chunk)

    return out


def _recall_aliases(term: str) -> list[str]:
    tokens = set(_term_tokens(term))
    aliases: list[str] = []

    def add(alias: str) -> None:
        alias = _clean_phrase(alias)
        if alias and alias.lower() != _clean_phrase(term).lower():
            aliases.append(alias)

    if "faithfulness" in tokens or "faithful" in tokens:
        add("unfaithful chain thought")
        add("faithful chain thought")
        add("faithfulness internal reasoning")
        add("internal reasoning faithfulness")
    if "compressed" in tokens or "compressing" in tokens:
        add("compressed chain thought")
        add("compressing chain thought")
        add("chain thought compression")
        add("thought compression")
    if "continuous" in tokens or "latent" in tokens:
        if "reasoning" in tokens:
            add("latent reasoning")
            add("latent thought")
            add("latent chain thought")
            add("continuous chain thought")
            add("latent thought models")
            add("inference time latent reasoning")
            add("variational latent reasoning")
            add("latent search")
            add("latent backtracking")
    if "internalized" in tokens or "internalize" in tokens:
        add("internalize chain thought")
        add("internalized chain thought")
        add("implicit chain thought")
        add("implicit cot")
    if "implicit" in tokens and ({"chain", "thought", "cot"} & tokens):
        add("implicit cot")
        add("implicit chain thought")
        add("internal reasoning")
        add("hidden reasoning")
        add("latent thought")
    if "hidden" in tokens and ({"state", "states", "computation", "reasoning"} & tokens):
        add("hidden computation")
        add("internal computation")
        add("hidden computation transformer")
        add("filler tokens")
    if "pause" in tokens and ("token" in tokens or "tokens" in tokens):
        add("hidden computation")
        add("filler tokens")
    if "soft" in tokens and ({"token", "tokens", "thought", "cot"} & tokens):
        add("soft token reasoning")
        add("softcot")
        add("soft chain thought")
    return _dedupe_keep_order(aliases)


def _blob_has_any(blob: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, blob) for pattern in patterns)


# Markers that introduce an exclusion clause inside a free-text direction.
# Words appearing only after one of these must not drive axis detection: a
# direction whose out-of-scope clause mentions "agent" is not an agent topic.
_OUT_OF_SCOPE_MARKERS: tuple[str, ...] = (
    "out-of-scope",
    "out of scope",
    "out of-scope",
    "out-of scope",
    "not in scope",
    "not in-scope",
    "excluding",
    "excludes",
    "exclude:",
    "排除",
    "不包括",
    "不收录",
    "不纳入",
)


def _in_scope_text(direction: str) -> str:
    """Return only the in-scope portion of a direction.

    Axis detection for cross-axis bridge terms scans free text, so an exclusion
    clause like "...out-of-scope: ... agent online adaptation" would otherwise
    make a pure in-context-learning topic look like an agent topic and inject
    unrelated multi-agent bridge queries. Truncating at the earliest exclusion
    marker keeps only the positive scope.
    """
    if not direction:
        return direction
    low = direction.lower()
    cut = len(direction)
    for marker in _OUT_OF_SCOPE_MARKERS:
        idx = low.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return direction[:cut]


def _coverage_axis_aliases(*texts: str) -> list[str]:
    """Infer bridge queries for cross-axis research directions.

    The brain often captures the obvious phrase ("latent reasoning agent") but
    misses neighboring subfields whose papers use different bridge vocabulary
    ("latent communication", "KV cache communication"). These aliases are not
    paper-specific seeds; they are generic query families activated only when
    the user's direction combines the relevant axes.
    """
    blob = _clean_phrase(" ".join(text for text in texts if text)).lower()
    aliases: list[str] = []

    def add(*values: str) -> None:
        aliases.extend(_clean_phrase(value) for value in values if _clean_phrase(value))

    latent_axis = _blob_has_any(blob, (
        r"\blatent\b",
        r"\bimplicit\b",
        r"\binternal\b",
        r"\binternalized\b",
        r"\bhidden\b",
        r"\bcontinuous\b",
        r"\bsoft\b",
        r"\bcompressed\b",
    ))
    agent_axis = _blob_has_any(blob, (
        r"\bagent\b",
        r"\bagents\b",
        r"\bagentic\b",
        r"\btool use\b",
        r"\btool using\b",
        r"\btool calling\b",
        r"\brag\b",
        r"\bretrieval\b",
        r"\bcoding agent\b",
        r"\bgui agent\b",
        r"\bmobile agent\b",
        r"\bembodied\b",
        r"\brobot\b",
        r"\brobotic\b",
        r"\bvla\b",
        r"\bvision language action\b",
    ))
    multi_agent_axis = _blob_has_any(blob, (
        r"\bmulti agent\b",
        r"\bmultiagent\b",
        r"\bagent collaboration\b",
        r"\bagent communication\b",
        r"\bdebate\b",
        r"\bcollaboration\b",
        r"\bcommunication\b",
    ))
    embodied_axis = _blob_has_any(blob, (
        r"\bembodied\b",
        r"\brobot\b",
        r"\brobotic\b",
        r"\bvla\b",
        r"\bvision language action\b",
        r"\bmobile agent\b",
        r"\bgui agent\b",
        r"\bautonomous driving\b",
    ))
    safety_axis = _blob_has_any(blob, (
        r"\bsafety\b",
        r"\bmonitor\b",
        r"\bmonitorability\b",
        r"\baudit\b",
        r"\bguard\b",
        r"\battack\b",
        r"\bbackdoor\b",
        r"\bmisaligned\b",
    ))

    if latent_axis and (agent_axis or multi_agent_axis):
        add(
            "latent communication",
            "latent collaboration",
            "latent multi agent",
            "multi agent latent communication",
            "hidden state communication",
            "kv cache communication",
            "model to model communication",
            "state delta trajectory",
            "hybrid latent text",
            "internalized multi agent",
        )
    if latent_axis and agent_axis:
        add(
            "latent agent",
            "latent agentic reasoning",
            "implicit reasoning agent",
            "hidden state agent",
            "latent planning",
            "latent action selection",
            "latent tool use",
            "latent rag",
            "latent coding agent",
        )
    if latent_axis and embodied_axis:
        add(
            "latent vla",
            "latent motion",
            "latent world model",
            "latent visual planning",
            "implicit reasoning vla",
            "latent gui agent",
            "latent mobile agent",
        )
    if latent_axis and safety_axis:
        add(
            "latent reasoning safety",
            "latent communication safety",
            "continuous thought monitoring",
            "latent attack",
            "hidden reasoning audit",
            "chain thought monitorability",
        )
    return _dedupe_keep_order([alias for alias in aliases if not _is_generic(alias)])


_PRIORITY_RECALL_PHRASES: tuple[tuple[str, int], ...] = (
    ("implicit cot", 0),
    ("implicit chain thought", 0),
    ("latent reasoning", 1),
    ("latent thought", 1),
    ("latent chain thought", 1),
    ("continuous chain thought", 1),
    ("hidden computation", 2),
    ("internal reasoning", 2),
    ("hidden reasoning", 2),
    ("filler tokens", 2),
    ("pause tokens", 2),
    ("compressed chain thought", 1),
    ("compressed reasoning", 1),
    ("compressing chain thought", 3),
    ("chain thought compression", 3),
    ("thought compression", 3),
    ("soft token reasoning", 1),
    ("soft token", 1),
    ("soft chain thought", 1),
    ("softcot", 1),
    ("faithful chain thought", 4),
    ("unfaithful chain thought", 4),
    ("faithfulness internal reasoning", 4),
)


def _priority_term_score(term: str) -> int:
    cleaned = _clean_phrase(term).lower()
    for phrase, score in _PRIORITY_RECALL_PHRASES:
        if phrase in cleaned:
            return score
    return 20


def _prioritized_terms(terms: list[str]) -> list[str]:
    return [term for _, term in sorted(enumerate(terms), key=lambda item: (_priority_term_score(item[1]), item[0]))]


def recall_terms_for_plan(
    search_hints: list[str],
    search_keyword_text: str = "",
    discriminator_terms: list[str] | None = None,
    seed_titles: list[str] | None = None,
    *,
    direction: str = "",
    cap: int = 28,
) -> list[str]:
    """Build the deterministic recall term list used by all remote sources.

    Brain output is treated as semantic material, not source syntax. The code
    expands over-specified phrases into canonical cores, pulls short method
    names from the keyword paragraph. Seed titles are deliberately excluded
    from remote query generation: source-backed anchors get exact title probes,
    while broad title-derived queries can flood the pool with contrast papers.
    """
    _ = seed_titles  # retained for API stability; intentionally unused.
    terms: list[str] = []
    protected_terms: list[str] = []

    def extend_with_aliases(values: list[str]) -> None:
        for raw in values:
            term = _clean_phrase(raw)
            if not term:
                continue
            terms.append(term)
            terms.extend(_recall_aliases(term))

    base_hints = [_clean_phrase(h) for h in search_hints if _clean_phrase(h)]
    protected_terms.extend(base_hints)
    coverage_aliases = _coverage_axis_aliases(
        _in_scope_text(direction),
        " ".join(search_hints),
        search_keyword_text,
        " ".join(discriminator_terms or []),
    )
    protected_terms.extend(coverage_aliases[:8])
    extend_with_aliases(base_hints)
    base_keys = {h.lower() for h in base_hints}
    extend_with_aliases([t for t in canonical_short_terms(search_hints) if t.lower() not in base_keys])
    extend_with_aliases(canonical_short_terms(discriminator_terms or []))
    extend_with_aliases(_keyword_phrase_candidates(search_keyword_text))
    extend_with_aliases(coverage_aliases)
    cleaned = _dedupe_keep_order([_clean_phrase(t) for t in terms if _clean_phrase(t)])
    protected = _dedupe_keep_order([_clean_phrase(t) for t in protected_terms if _clean_phrase(t)])
    ordered = _dedupe_keep_order(protected + cleaned)
    # Drop bare generic/meta phrases that would over-recall as standalone source
    # filters; topic-specific compounds containing them are unaffected.
    ordered = [t for t in ordered if t.lower() not in _DENY_STANDALONE_RECALL_TERMS]
    return ordered[:cap]


def _source_ordered_terms(terms: list[str], priority_terms: list[str] | None = None) -> list[str]:
    protected = _dedupe_keep_order([_clean_phrase(t) for t in (priority_terms or []) if _clean_phrase(t)])
    protected_keys = {t.lower() for t in protected}
    rest = [term for term in terms if _clean_phrase(term).lower() not in protected_keys]
    return _dedupe_keep_order(protected + _prioritized_terms(rest))


def arxiv_queries_for_terms(
    terms: list[str],
    *,
    cap: int = 24,
    priority_terms: list[str] | None = None,
) -> list[str]:
    queries: list[str] = []
    for term in _source_ordered_terms(terms, priority_terms):
        variants = _arxiv_query_variants(term)
        if not variants:
            continue
        queries.extend(variants)
    return _dedupe_keep_order(queries)[:cap]


def openalex_queries_for_terms(
    terms: list[str],
    *,
    cap: int = 16,
    priority_terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for idx, term in enumerate(_source_ordered_terms(terms, priority_terms)[:cap]):
        specs.append({
            "text": term,
            "strength": "strong" if idx < 10 else "weak",
            "modes": ["search"],
        })
    return specs


def semantic_scholar_queries_for_terms(
    terms: list[str],
    *,
    cap: int = 12,
    priority_terms: list[str] | None = None,
) -> list[str]:
    return [_ss_query(term) for term in _source_ordered_terms(terms, priority_terms)[:cap] if _ss_query(term)]


def _plan_topic_blob(direction: str, terms: list[str], plan: dict[str, Any]) -> str:
    pieces = [direction, plan.get("name") or "", plan.get("description") or "", plan.get("search_keyword_text") or ""]
    pieces.extend(terms)
    return _clean_phrase(" ".join(str(piece) for piece in pieces if piece)).lower()


def _plan_default_sources(direction: str, terms: list[str], plan: dict[str, Any]) -> list[str]:
    blob = _plan_topic_blob(direction, terms, plan)
    sources = ["paperlists", "openalex", "crossref", "dblp", "arxiv"]
    if re.search(r"\b(nlp|natural language|language model|llm|llms|acl|emnlp|naacl|eacl|coling|tacl|aacl|gec|csc)\b", blob):
        sources.append("acl_anthology")
    if re.search(r"\b(biomed|biomedical|clinical|medicine|medical|health|patient|pubmed|pmc|gene|protein|drug|disease|biology|bioinformatics)\b", blob):
        sources.extend(["europepmc", "pubmed"])
    if re.search(r"\b(openreview|iclr|neurips|icml|tmlr|colm)\b", blob):
        # The adapter only runs when invitations are configured; keeping it in
        # sources.yaml makes the missing venue-specific handle visible.
        sources.append("openreview")
    return _dedupe_keep_order(sources)


def _seed_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    for raw_seed in plan.get("seed_papers") or []:
        if not isinstance(raw_seed, dict):
            continue
        title = (raw_seed.get("title") or "").strip()
        if not title:
            continue
        role = normalize_role(raw_seed.get("paper_role") or raw_seed.get("role"))
        source_backed = raw_seed.get("verified") is True and seed_has_source_evidence(raw_seed)
        if not source_backed:
            continue
        requested_required = seed_required({"paper_role": role, "required": raw_seed.get("required")})
        seed: dict[str, Any] = {
            "title": title,
            "year": int(raw_seed.get("year") or 0) or None,
            "why_seed": (raw_seed.get("why_seed") or raw_seed.get("reason") or "").strip(),
            "paper_role": role,
            "required": bool(source_backed and requested_required),
        }
        for k in ("arxiv_id", "doi", "url"):
            v = (raw_seed.get(k) or "").strip()
            if v:
                seed[k] = v
        for k in ("verified", "verify_note", "verified_arxiv_id", "verified_title", "verified_openalex_id", "requested_required"):
            if k in raw_seed:
                seed[k] = raw_seed[k]
        evidence = raw_seed.get("evidence") or raw_seed.get("seed_evidence")
        if isinstance(evidence, dict):
            seed["evidence"] = evidence
        seeds.append(seed)
    return seeds


def _seed_exact_titles(seeds: list[dict[str, Any]], *, cap: int = 16) -> list[str]:
    titles: list[str] = []
    for seed in seeds:
        role = normalize_role(seed.get("paper_role"))
        if role in NEGATIVE_ROLES or not seed_required(seed):
            continue
        title = (seed.get("title") or "").strip()
        if title:
            titles.append(title)
    return _dedupe_keep_order(titles)[:cap]


def _openalex_seed_queries(seeds: list[dict[str, Any]], *, cap: int = 16) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for seed in seeds:
        role = normalize_role(seed.get("paper_role"))
        if role in NEGATIVE_ROLES or not seed_required(seed):
            continue
        title = (seed.get("title") or "").strip()
        if not title:
            continue
        specs.append({
            "text": title,
            "strength": "must_recall",
            # Seed probes are exact-recall anchors, not broad-recall queries.
            # OpenAlex `search` can return ~100 papers that merely cite or
            # mention the seed title, which floods the weak-review queue.
            "modes": ["title"],
            "max_pages": 1,
            "page_size": 5,
            "paper_role": role,
        })
    return _dedupe_openalex_specs(specs)[:cap]


def _dedupe_openalex_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for spec in specs:
        text = (spec.get("text") or "").strip()
        modes = spec.get("modes") or spec.get("mode") or []
        mode_key = ",".join(modes) if isinstance(modes, list) else str(modes)
        key = (text.lower(), mode_key.lower())
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(spec)
    return out


def render_plan(
    plan: dict[str, Any],
    direction: str,
    *,
    topic_id_override: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Convert a plan into (topic_yaml, sources_yaml, source-backed anchors).

    Brain outputs search_hints (broad recall queries), discriminator_terms
    (specific anchors), search_keyword_text (embedding target), and
    judge_examples (LLM-judge in/out anchors). Paper anchors are accepted only
    when code attaches source evidence. Precision is the fusion stage's job
    (embedding + LLM judge + metadata).
    """
    topic_id = _slugify(topic_id_override or plan.get("topic_id") or direction[:30])
    name = plan.get("name") or direction
    description = plan.get("description") or direction
    min_year = int(plan.get("min_year") or 0) or None

    search_hints = _dedupe_keep_order(plan.get("search_hints") or [])
    search_keyword_text = (plan.get("search_keyword_text") or "").strip()
    if not search_keyword_text and search_hints:
        # cheap fallback: stitch the hints together as the embedding target
        search_keyword_text = ", ".join(search_hints)
    discriminator_terms = _dedupe_keep_order(
        [t for t in (plan.get("discriminator_terms") or []) if isinstance(t, str) and t.strip()]
    )

    raw_examples = plan.get("judge_examples") or {}
    if not isinstance(raw_examples, dict):
        raw_examples = {}
    judge_examples = {
        "in_scope": [
            {"title": (e.get("title") or "").strip(), "reason": (e.get("reason") or "").strip()}
            for e in (raw_examples.get("in_scope") or [])
            if isinstance(e, dict) and (e.get("title") or "").strip()
        ],
        "out_of_scope": [
            {"title": (e.get("title") or "").strip(), "reason": (e.get("reason") or "").strip()}
            for e in (raw_examples.get("out_of_scope") or [])
            if isinstance(e, dict) and (e.get("title") or "").strip()
        ],
    }

    seeds = _seed_rows(plan)
    seed_titles = _seed_exact_titles(seeds)
    recall_terms = recall_terms_for_plan(
        search_hints,
        search_keyword_text,
        discriminator_terms,
        seed_titles,
        direction=direction,
    )

    # Build per-source queries from deterministic recall terms. The brain
    # supplies meaning; code owns source syntax, broadening, dedupe, and caps.
    # discriminator_terms may contribute bounded recall probes after the main
    # hints, but seed titles stay out of remote queries to avoid title-noise
    # flooding from contrast papers.
    arxiv_recent_year_count = 3
    arxiv_max_results = 35
    arxiv_page_size = 35
    arxiv_query_cap = 12
    base_priority_terms = [_clean_phrase(h) for h in search_hints if _clean_phrase(h)]
    coverage_priority_terms = _coverage_axis_aliases(
        _in_scope_text(direction),
        " ".join(search_hints),
        search_keyword_text,
        " ".join(discriminator_terms),
    )
    # Keep the brain's top hints first, but reserve early slots for inferred
    # cross-axis bridge terms so a whole adjacent subfield is not pushed past
    # source query caps.
    source_priority_terms = _dedupe_keep_order(
        base_priority_terms[:4] + coverage_priority_terms[:4] + base_priority_terms[4:]
    )
    arxiv_queries = arxiv_queries_for_terms(
        recall_terms,
        cap=arxiv_query_cap,
        priority_terms=source_priority_terms,
    )
    openalex_queries = _dedupe_openalex_specs(
        _openalex_seed_queries(seeds)
        + openalex_queries_for_terms(recall_terms, priority_terms=source_priority_terms)
    )
    # Exact seed-title recall goes through OpenAlex title/search mode first.
    # Semantic Scholar bulk queries are expensive across year buckets, so do
    # not prepend every seed title there.
    ss_queries = semantic_scholar_queries_for_terms(recall_terms, priority_terms=source_priority_terms)
    structured_queries = [q["text"] for q in openalex_queries_for_terms(recall_terms, cap=12, priority_terms=source_priority_terms)]
    default_sources = _plan_default_sources(direction, recall_terms, plan)

    # Discovery source queries and source-level topic filtering must share the
    # same deterministic recall vocabulary. Otherwise a source query such as
    # "latent reasoning" can retrieve a relevant paper while the topic filter
    # only records a much broader auto-derived hit like "latent space".
    source_filter_terms = _dedupe_keep_order(recall_terms)

    topic_yaml: dict[str, Any] = {
        "topic_id": topic_id,
        "name": name,
        "description": description,
        "min_year": min_year,
        "direction_raw": direction,
        "search_hints": search_hints,
        "search_keyword_text": search_keyword_text,
        "discriminator_terms": discriminator_terms,
        "source_filter_terms": source_filter_terms,
        "judge_examples": judge_examples,
    }
    publication_scope = infer_publication_scope(direction, search_hints)
    if publication_scope:
        topic_yaml["publication_scope"] = publication_scope

    sources_yaml: dict[str, Any] = {
        "sources": {
            "arxiv": {
                "enabled": True,
                "type": "arxiv",
                "max_results": arxiv_max_results,
                "sort_by": ["relevance", "submittedDate"],
                "sleep_seconds": 0.35,
                "timeout": 35,
                "queries": list(arxiv_queries),
            }
        },
        "discovery": {
            "max_remote_calls": 200,
            "sources": default_sources,
            "openalex": {
                "base_url": "https://api.openalex.org",
                "api_key": "",
                "api_key_env": "OPENALEX_API_KEY",
                "mailto": "",
                "mailto_env": "OPENALEX_EMAIL",
                "page_size": 100,
                "max_pages": 2,
                "weak_max_pages": 1,
                "modes": ["exact"],
                "sorts": ["relevance"],
                "topic_ids": [],
                "queries": openalex_queries,
            },
            "crossref": {
                "base_url": "https://api.crossref.org",
                "mailto": "",
                "mailto_env": "OPENALEX_EMAIL",
                "page_size": 25,
                "max_pages": 1,
                "max_results": 50,
                "queries": structured_queries,
            },
            "dblp": {
                "base_url": "https://dblp.org/search/publ/api",
                "page_size": 25,
                "max_pages": 1,
                "max_results": 50,
                "queries": structured_queries,
            },
            "acl_anthology": {
                "base_url": "https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml",
                "venues": ["acl", "emnlp", "naacl", "eacl", "coling"],
                "sleep_seconds": 0.25,
            },
            "europepmc": {
                "base_url": "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                "page_size": 25,
                "max_pages": 1,
                "max_results": 50,
                "queries": structured_queries,
            },
            "pubmed": {
                "base_url": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
                "api_key": "",
                "api_key_env": "NCBI_API_KEY",
                "page_size": 20,
                "max_results": 30,
                "queries": structured_queries,
            },
            "openreview": {
                "base_url": "https://api2.openreview.net",
                "invitations": [],
                "limit": 50,
                "max_pages": 1,
            },
            "semanticscholar": {
                "mode": "bulk",
                "year_strategy": "range",
                "max_pages": 1,
                "max_results": 300,
                "page_size": 100,
                "max_kept_per_run": 25,
                "sleep_seconds": 6.0,
                "no_key_sleep_seconds": 6.0,
                "retry_attempts": 1,
                "sorts": ["citationCount:desc", "publicationDate:desc"],
                "base_url": "https://api.semanticscholar.org/graph/v1",
                "api_key": "",
                "api_key_env": "SEMANTIC_SCHOLAR_API_KEY",
                "queries": ss_queries,
            },
            "arxiv": {
                "budget_policy": "auto_floor",
                "max_results": arxiv_max_results,
                "page_size": arxiv_page_size,
                "sleep_seconds": 3.2,
                "sort_by": "relevance",
                "recent_first": True,
                "recent_year_count": arxiv_recent_year_count,
                "retry_attempts": 1,
                "rate_limit_error_limit": 2,
                "queries": list(arxiv_queries),
            },
        },
    }
    ensure_arxiv_budget_floor(sources_yaml["discovery"]["arxiv"])

    return topic_yaml, sources_yaml, seeds


def _title_similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    a_norm = re.sub(r"[^a-z0-9]+", " ", (a or "").lower()).strip()
    b_norm = re.sub(r"[^a-z0-9]+", " ", (b or "").lower()).strip()
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def _arxiv_search_with_retry(query: str, max_results: int, timeout: int, max_retries: int = 3) -> list[dict[str, Any]]:
    """Wrapper around arxiv_search that retries on HTTP 429 (rate limit) with
    exponential backoff. arxiv's official policy is 3 seconds between requests."""
    import time
    from urllib.error import HTTPError
    from ..sources.arxiv import arxiv_search

    backoff = 5.0
    for attempt in range(max_retries):
        try:
            return arxiv_search(query, max_results=max_results, timeout=timeout)
        except HTTPError as exc:
            if exc.code == 429 and attempt + 1 < max_retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise


def verify_seeds_via_arxiv(
    seeds: list[dict[str, Any]],
    *,
    sleep_seconds: float = 3.5,
    timeout: int = 20,
    title_threshold: float = 0.7,
) -> list[dict[str, Any]]:
    """Validate legacy/manual seed rows against the arXiv API.

    For each seed, sets:
      - ``verified``: True | False | None (None = skipped or network error)
      - ``verify_note``: short string explaining the outcome
      - ``verified_arxiv_id`` / ``verified_title``: canonical values when verified

    Honours ``PAPERCOMPASS_SKIP_SEED_VERIFY=1`` to bypass entirely (for offline
    / CI use).
    """
    import os
    import time

    if os.environ.get("PAPERCOMPASS_SKIP_SEED_VERIFY", "").strip().lower() in {"1", "true", "yes"}:
        return list(seeds)

    out: list[dict[str, Any]] = []
    for seed in seeds:
        new_seed = dict(seed)
        arxiv_id_raw = str(new_seed.get("arxiv_id") or "").strip()
        clean_id = re.sub(r"^arxiv:", "", arxiv_id_raw, flags=re.IGNORECASE).strip()
        title = str(new_seed.get("title") or "").strip()

        if not clean_id and not title:
            new_seed["verified"] = None
            new_seed["verify_note"] = "skipped_no_id_or_title"
            out.append(new_seed)
            continue

        try:
            results: list[dict[str, Any]] = []
            if clean_id:
                results = _arxiv_search_with_retry(f"id:{clean_id}", max_results=1, timeout=timeout)
                if results:
                    found_title = results[0].get("title") or ""
                    if title and _title_similarity(title, found_title) < 0.4:
                        new_seed["verified"] = False
                        new_seed["verify_note"] = "arxiv_id_exists_but_title_mismatch"
                        new_seed["verified_arxiv_id"] = results[0].get("arxiv_id") or clean_id
                        new_seed["verified_title"] = found_title
                    else:
                        new_seed["verified"] = True
                        new_seed["verify_note"] = "verified_by_arxiv_id"
                        new_seed["verified_arxiv_id"] = results[0].get("arxiv_id") or clean_id
                        new_seed["verified_title"] = found_title
                        new_seed["verified_paper"] = results[0]
                        new_seed["evidence"] = {
                            "source": "arxiv",
                            "query": f"id:{clean_id}",
                            "source_item_id": results[0].get("arxiv_id") or clean_id,
                            "source_url": results[0].get("url") or f"https://arxiv.org/abs/{clean_id}",
                            "match_type": "arxiv_id_exact",
                        }
                        if new_seed.get("requested_required") and normalize_role(new_seed.get("paper_role")) not in NEGATIVE_ROLES:
                            new_seed["required"] = True
                    out.append(new_seed)
                    time.sleep(sleep_seconds)
                    continue
                new_seed["verify_note"] = "arxiv_id_not_found"

            if title:
                time.sleep(sleep_seconds)
                title_results = _arxiv_search_with_retry(f'ti:"{title}"', max_results=3, timeout=timeout)
                best_score = 0.0
                best = None
                for r in title_results:
                    score = _title_similarity(title, r.get("title") or "")
                    if score > best_score:
                        best_score = score
                        best = r
                if best and best_score >= title_threshold:
                    new_seed["verified"] = True
                    new_seed["verify_note"] = (
                        "verified_by_title" if not clean_id else "verified_by_title_id_was_invalid"
                    )
                    new_seed["verified_arxiv_id"] = best.get("arxiv_id") or ""
                    new_seed["verified_title"] = best.get("title") or ""
                    new_seed["verified_paper"] = best
                    new_seed["evidence"] = {
                        "source": "arxiv",
                        "query": f'ti:"{title}"',
                        "source_item_id": best.get("arxiv_id") or "",
                        "source_url": best.get("url") or "",
                        "match_type": "arxiv_title_match",
                    }
                    if new_seed.get("requested_required") and normalize_role(new_seed.get("paper_role")) not in NEGATIVE_ROLES:
                        new_seed["required"] = True
                else:
                    new_seed["verified"] = False
                    new_seed["verify_note"] = (
                        "title_not_found_in_arxiv" if not clean_id else "arxiv_id_invalid_and_title_unfound"
                    )
            else:
                new_seed["verified"] = False
                new_seed["verify_note"] = "arxiv_id_not_found_no_title_fallback"
        except Exception as exc:
            new_seed["verified"] = None
            new_seed["verify_note"] = f"verify_error:{type(exc).__name__}"

        out.append(new_seed)
        time.sleep(sleep_seconds)
    return out


def verify_seeds_via_openalex(
    seeds: list[dict[str, Any]],
    *,
    min_title_similarity: float = 0.82,
    timeout: int = 25,
    http_get_json: Any = None,
) -> list[dict[str, Any]]:
    """Attach OpenAlex evidence to legacy/manual seed rows that are not verified yet."""
    import os

    from ..discovery import (
        OPENALEX_API,
        http_get_json as _default_http_get_json,
        openalex_item_to_raw,
        openalex_select_fields,
        openalex_url,
    )

    if os.environ.get("PAPERCOMPASS_SKIP_SEED_VERIFY", "").strip().lower() in {"1", "true", "yes"}:
        return list(seeds)

    fetch = http_get_json or _default_http_get_json
    api_key = os.environ.get("OPENALEX_API_KEY", "")
    out: list[dict[str, Any]] = []
    for seed in seeds:
        new_seed = dict(seed)
        if new_seed.get("verified") is True and seed_has_source_evidence(new_seed):
            out.append(new_seed)
            continue
        title = clean_text(new_seed.get("title"))
        doi = clean_text(new_seed.get("doi"))
        if not title and not doi:
            out.append(new_seed)
            continue
        filters = ["type:article"]
        params: dict[str, Any] = {
            "per-page": 5,
            "select": openalex_select_fields(),
        }
        match_type = "openalex_title_match"
        if doi:
            filters.append(f"doi:{doi.removeprefix('https://doi.org/').removeprefix('http://doi.org/')}")
            match_type = "doi_exact"
        elif title:
            filters.append(f"title.search:{title}")
        params["filter"] = ",".join(filters)
        if api_key:
            params["api_key"] = api_key
        url = openalex_url(OPENALEX_API, params)
        try:
            data = fetch(
                url,
                timeout=timeout,
                attempts=2,
                retry_status={429, 500, 502, 503, 504},
                backoff=3.0,
                source="seed_verify_openalex",
            )
        except Exception as exc:  # noqa: BLE001
            new_seed.setdefault("verify_note", f"openalex_verify_error:{type(exc).__name__}")
            out.append(new_seed)
            continue
        items = data.get("results") if isinstance(data, dict) else []
        if not isinstance(items, list) or not items:
            if new_seed.get("verified") is not False:
                new_seed["verified"] = False
                new_seed["verify_note"] = "not_found_in_openalex"
            out.append(new_seed)
            continue
        best_raw: dict[str, Any] | None = None
        best_item: dict[str, Any] | None = None
        best_score = 0.0
        for item in items:
            if not isinstance(item, dict):
                continue
            raw = openalex_item_to_raw(item)
            score = 1.0 if doi and clean_text(raw.get("doi")).lower().endswith(doi.lower()) else _title_similarity(title, raw.get("title") or "")
            if score > best_score:
                best_score = score
                best_raw = raw
                best_item = item
        if best_raw and (doi or best_score >= min_title_similarity):
            source_id = clean_text(best_raw.get("openalex_id") or (best_item or {}).get("id"))
            new_seed["verified"] = True
            new_seed["verify_note"] = "verified_by_openalex_doi" if doi else "verified_by_openalex_title"
            new_seed["verified_openalex_id"] = source_id
            new_seed["verified_title"] = best_raw.get("title") or title
            new_seed["verified_paper"] = best_raw
            if best_raw.get("doi") and not new_seed.get("doi"):
                new_seed["doi"] = best_raw["doi"]
            if best_raw.get("arxiv_id") and not new_seed.get("arxiv_id"):
                new_seed["arxiv_id"] = best_raw["arxiv_id"]
            if best_raw.get("url") and not new_seed.get("url"):
                new_seed["url"] = best_raw["url"]
            new_seed["evidence"] = {
                "source": "openalex",
                "query": doi or title,
                "api_url": url,
                "source_item_id": source_id,
                "source_url": best_raw.get("url") or source_id,
                "match_type": match_type,
            }
            if new_seed.get("requested_required") and normalize_role(new_seed.get("paper_role")) not in NEGATIVE_ROLES:
                new_seed["required"] = True
        elif new_seed.get("verified") is not False:
            new_seed["verified"] = False
            new_seed["verify_note"] = "openalex_title_below_threshold"
        out.append(new_seed)
    return out


def inject_verified_seeds_to_raw(
    workspace: Path,
    seeds: list[dict[str, Any]],
) -> int:
    """Write every seed with a verified arXiv result to ``.raw/manual/`` so that
    must_recall anchors always enter the candidate pool, even if OpenAlex /
    Semantic Scholar / arXiv search miss them.

    Returns the number of papers injected. No-op for seeds that lack
    ``verified=True`` or ``verified_paper`` (e.g. brain-hallucinated IDs).
    """
    from datetime import datetime
    from ..config import raw_dir

    payload_rows: list[dict[str, Any]] = []
    for seed in seeds:
        if seed.get("verified") is not True:
            continue
        paper = seed.get("verified_paper")
        if not isinstance(paper, dict) or not paper.get("title"):
            continue
        raw_paper: dict[str, Any] = {
            "title": paper.get("title", ""),
            "authors": paper.get("authors", ""),
            "year": paper.get("year"),
            "venue": paper.get("venue") or "arXiv",
            "abstract": paper.get("abstract", ""),
            "url": paper.get("url", ""),
            "pdf_url": paper.get("pdf_url", ""),
            "arxiv_id": paper.get("arxiv_id", ""),
            "doi": paper.get("doi", ""),
            "categories": paper.get("categories") or [],
            "notes": [f"injected from verified seed (note={seed.get('verify_note', '')})"],
            "paper_role": normalize_role(
                seed.get("paper_role") or seed.get("role"),
                default="core_method",
            ),
        }
        if seed_required(seed):
            raw_paper["required"] = True
            raw_paper["force_include_reason"] = "must_recall_seed_injection"
        payload_rows.append({
            "source_name": "verified_seed_injection",
            "source_type": "manual",
            "query": "verified_seed_arxiv_id",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "source_item_id": raw_paper.get("arxiv_id") or "",
            "raw": raw_paper,
        })
    if not payload_rows:
        return 0
    manual_dir = raw_dir(workspace) / "manual"
    manual_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime as _dt2
    out_path = manual_dir / f"{_dt2.now().strftime('%Y%m%d_%H%M%S')}_verified_seed_injection.jsonl"
    out_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in payload_rows) + "\n",
        encoding="utf-8",
    )
    return len(payload_rows)


def write_plan(workspace: Path, topic: dict[str, Any], sources: dict[str, Any], seeds: list[dict[str, Any]]) -> None:
    write_yaml(workspace / "topic.yaml", topic)
    write_yaml(workspace / "sources.yaml", sources)
    write_anchor_rows(workspace, seeds)


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if not isinstance(v, str):
            continue
        v = v.strip()
        if not v:
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out

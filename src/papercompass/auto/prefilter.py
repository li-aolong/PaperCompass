from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import hashlib
import math
import re
from pathlib import Path
from typing import Any

from ..config import data_dir, load_topic_config, workspace_label, workspace_relative_path
from ..text import as_list, clean_text, normalize_title, parse_year, title_tokens
from ..text import read_json, write_jsonl


PREFILTER_POLICY_VERSION = "papercompass.prefilter.v1"
GENERIC_TOKEN_DENYLIST = {
    "agent", "agents", "ai", "artificial", "benchmark", "benchmarks", "data", "dataset",
    "datasets", "deep", "evaluation", "framework", "generation", "inference", "language",
    "large", "learning", "machine", "method", "model", "models", "multi", "neural",
    "paper", "performance", "reasoning", "review", "search", "study", "system",
    "systems", "task", "tasks", "training",
}


@dataclass(frozen=True)
class PrefilterPolicy:
    low_threshold: float = 25.0
    high_threshold: float = 75.0
    hard_reject_threshold: float = 15.0
    strong_requires_source_count: int = 2
    strong_audit_rate: float = 0.10
    strong_audit_min: int = 5
    max_llm_review_candidates: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PrefilterPolicy":
        data = data or {}
        return cls(
            low_threshold=float(data.get("low_threshold", cls.low_threshold)),
            high_threshold=float(data.get("high_threshold", cls.high_threshold)),
            hard_reject_threshold=float(data.get("hard_reject_threshold", cls.hard_reject_threshold)),
            strong_requires_source_count=int(data.get("strong_requires_source_count", cls.strong_requires_source_count)),
            strong_audit_rate=float(data.get("strong_audit_rate", cls.strong_audit_rate)),
            strong_audit_min=int(data.get("strong_audit_min", cls.strong_audit_min)),
            max_llm_review_candidates=(
                int(data["max_llm_review_candidates"])
                if data.get("max_llm_review_candidates") not in (None, "")
                else None
            ),
        )


@dataclass
class PrefilterDecision:
    action: str
    score: float
    reasons: list[str]
    topic_hits: list[str]
    negative_hits: list[str]
    confidence: float = 0.0
    features: dict[str, Any] = field(default_factory=dict)
    paper_key: str = ""
    candidate_key: str = ""
    policy_version: str = PREFILTER_POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BM25Scorer:
    def __init__(self) -> None:
        self.documents: list[list[str]] = []
        self.doc_freq: Counter[str] = Counter()
        self.avgdl = 0.0

    def fit(self, texts: list[str]) -> None:
        self.documents = [_tokenize(text) for text in texts]
        self.doc_freq = Counter()
        for doc in self.documents:
            self.doc_freq.update(set(doc))
        self.avgdl = sum(len(doc) for doc in self.documents) / len(self.documents) if self.documents else 0.0

    def score(self, index: int, query_terms: list[str]) -> float:
        if index < 0 or index >= len(self.documents) or not self.documents:
            return 0.0
        doc = self.documents[index]
        if not doc:
            return 0.0
        q_tokens: list[str] = []
        for term in query_terms:
            q_tokens.extend(_tokenize(term))
        if not q_tokens:
            return 0.0
        tf = Counter(doc)
        n_docs = len(self.documents)
        k1 = 1.5
        b = 0.75
        score = 0.0
        doc_len = len(doc)
        avgdl = self.avgdl or 1.0
        for token in set(q_tokens):
            df = self.doc_freq.get(token, 0)
            if df <= 0:
                continue
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            freq = tf.get(token, 0)
            denom = freq + k1 * (1 - b + b * doc_len / avgdl)
            if denom:
                score += idf * (freq * (k1 + 1) / denom)
        return round(min(35.0, score * 6.0), 3)


def _tokenize(text: str) -> list[str]:
    return title_tokens(text)


def _dedupe(items: list[str], *, cap: int = 80) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = clean_text(item)
        key = normalize_title(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= cap:
            break
    return out


def _keyword_terms(text: Any) -> list[str]:
    cleaned = clean_text(text)
    if not cleaned:
        return []
    pieces = [part.strip() for part in re.split(r"[,;|/()\[\]\n]", cleaned) if part.strip()]
    tokens = title_tokens(cleaned)
    bigrams = [f"{tokens[i]} {tokens[i + 1]}" for i in range(len(tokens) - 1)]
    return pieces + bigrams + tokens


def build_positive_terms(topic: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for key in ("source_filter_terms", "discriminator_terms", "search_hints"):
        terms.extend(as_list(topic.get(key)))
    terms.extend(_keyword_terms(topic.get("search_keyword_text")))
    examples = topic.get("judge_examples") if isinstance(topic.get("judge_examples"), dict) else {}
    for row in examples.get("in_scope") or []:
        if isinstance(row, dict):
            terms.extend(_keyword_terms(row.get("title")))
            terms.extend(_keyword_terms(row.get("reason")))
    return _dedupe([term for term in terms if len(normalize_title(term)) >= 3])


def split_positive_terms(terms: list[str]) -> dict[str, list[str]]:
    exact_phrases: list[str] = []
    soft_tokens: list[str] = []
    generic_tokens: list[str] = []
    for term in terms:
        key = normalize_title(term)
        if not key:
            continue
        tokens = key.split()
        if len(tokens) == 1:
            token = tokens[0]
            if token in GENERIC_TOKEN_DENYLIST:
                generic_tokens.append(term)
            elif any("\u4e00" <= ch <= "\u9fff" for ch in token):
                exact_phrases.append(term)
            else:
                soft_tokens.append(term)
        else:
            exact_phrases.append(term)
            soft_tokens.extend(token for token in tokens if token not in GENERIC_TOKEN_DENYLIST)
    return {
        "exact_phrases": _dedupe(exact_phrases),
        "soft_tokens": _dedupe(soft_tokens),
        "generic_tokens": _dedupe(generic_tokens),
    }


def build_negative_terms(topic: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for key in ("negative_patterns", "negative_terms", "exclude_terms", "out_of_scope_terms"):
        terms.extend(as_list(topic.get(key)))
    examples = topic.get("judge_examples") if isinstance(topic.get("judge_examples"), dict) else {}
    for row in examples.get("out_of_scope") or []:
        if isinstance(row, dict):
            terms.extend(_keyword_terms(row.get("title")))
            terms.extend(_keyword_terms(row.get("reason")))
    return _dedupe([term for term in terms if len(normalize_title(term)) >= 3], cap=50)


def phrase_hits(text: str, terms: list[str]) -> list[str]:
    haystack = normalize_title(text)
    if not haystack:
        return []
    padded = f" {haystack} "
    hits: list[str] = []
    for term in terms:
        needle = normalize_title(term)
        if not needle:
            continue
        if " " in needle:
            matched = needle in haystack
        elif any("\u4e00" <= ch <= "\u9fff" for ch in needle):
            matched = needle in haystack
        else:
            matched = f" {needle} " in padded
        if matched:
            hits.append(term)
    return hits


def candidate_text(paper: dict[str, Any]) -> str:
    fields = [
        paper.get("title"),
        paper.get("abstract"),
        paper.get("summary"),
        paper.get("venue"),
        " ".join(as_list(paper.get("keywords"))),
    ]
    return "\n".join(clean_text(field) for field in fields if clean_text(field))


def _source_count(paper: dict[str, Any]) -> int:
    return len({source for source in as_list(paper.get("sources")) if source})


def _source_prior(paper: dict[str, Any]) -> float:
    count = _source_count(paper)
    if count >= 3:
        return 20.0
    if count == 2:
        return 12.0
    if count == 1:
        return 4.0
    return 0.0


def _citation_prior(paper: dict[str, Any]) -> float:
    for key in ("max_citation", "citation_count", "gs_citation", "cited_by_count"):
        value = paper.get(key)
        if value is None:
            continue
        try:
            count = int(float(value))
        except (TypeError, ValueError):
            continue
        if count >= 100:
            return 15.0
        if count >= 25:
            return 10.0
        if count > 0:
            return 5.0
    return 0.0


def _is_bad_publication_status(paper: dict[str, Any]) -> bool:
    return clean_text(paper.get("publication_status") or paper.get("status")).lower() in {
        "reject",
        "rejected",
        "withdraw",
        "withdrawn",
        "desk_reject",
        "desk reject",
        "retracted",
    }


class PrefilterPipeline:
    def __init__(self, topic: dict[str, Any], policy: dict[str, Any] | None = None):
        self.topic = topic
        self.policy = PrefilterPolicy.from_dict(policy)
        self.positive_terms = build_positive_terms(topic)
        self.positive_term_groups = split_positive_terms(self.positive_terms)
        self.exact_phrases = self.positive_term_groups["exact_phrases"]
        self.bm25_terms = self._bm25_terms(self.positive_term_groups["soft_tokens"])
        self.negative_terms = build_negative_terms(topic)
        self.bm25 = BM25Scorer()

    @staticmethod
    def _bm25_terms(terms: list[str]) -> list[str]:
        filtered: list[str] = []
        for term in terms:
            key = normalize_title(term)
            tokens = key.split()
            if len(tokens) == 1 and tokens[0] in GENERIC_TOKEN_DENYLIST:
                continue
            filtered.append(term)
        return filtered

    def fit(self, candidates: list[dict[str, Any]]) -> None:
        self.bm25.fit([candidate_text(candidate) for candidate in candidates])

    def evaluate(
        self,
        paper: dict[str, Any],
        *,
        index: int = -1,
        protected: bool = False,
    ) -> PrefilterDecision:
        reasons: list[str] = []
        title = clean_text(paper.get("title"))
        abstract = clean_text(paper.get("abstract"))
        text = candidate_text(paper)
        exact_title_hits = phrase_hits(title, self.exact_phrases)
        exact_topic_hits = phrase_hits(text, self.exact_phrases)
        soft_title_hits = phrase_hits(title, self.bm25_terms)
        soft_topic_hits = phrase_hits(text, self.bm25_terms)
        title_hits = _dedupe(exact_title_hits + soft_title_hits, cap=12)
        topic_hits = _dedupe(exact_topic_hits + soft_topic_hits, cap=12)
        negative_hits = phrase_hits(text, self.negative_terms)

        min_year = parse_year(self.topic.get("min_year"))
        year = parse_year(paper.get("year"))
        if protected:
            reasons.append("protected_required_seed")
        if min_year and year and year < min_year:
            reasons.append("before_min_year")
        if _is_bad_publication_status(paper):
            reasons.append("bad_publication_status")
        if not abstract:
            reasons.append("missing_abstract")
        if not topic_hits:
            reasons.append("no_topic_signal")
        if negative_hits and not exact_title_hits:
            reasons.append("negative_signal_without_title_hit")

        bm25 = self.bm25.score(index, self.bm25_terms)
        non_title_exact_hits = [hit for hit in exact_topic_hits if hit not in set(exact_title_hits)]
        non_title_soft_hits = [hit for hit in soft_topic_hits if hit not in set(soft_title_hits)]
        phrase = min(
            35.0,
            9.0 * len(exact_title_hits)
            + 4.0 * len(non_title_exact_hits)
            + 5.0 * len(soft_title_hits)
            + 2.0 * len(non_title_soft_hits),
        )
        title_boost = 15.0 if title_hits else 0.0
        source_prior = _source_prior(paper)
        citation_prior = _citation_prior(paper)
        negative_penalty = 30.0 if negative_hits and not exact_title_hits else 0.0
        year_penalty = 35.0 if "before_min_year" in reasons else 0.0
        status_penalty = 40.0 if "bad_publication_status" in reasons else 0.0
        abstract_penalty = 10.0 if "missing_abstract" in reasons else 0.0
        score = max(
            0.0,
            phrase
            + bm25
            + title_boost
            + source_prior
            + citation_prior
            - negative_penalty
            - year_penalty
            - status_penalty
            - abstract_penalty,
        )
        score = round(min(100.0, score), 1)

        if protected:
            action = "protected"
            confidence = 1.0
        elif "before_min_year" in reasons or "bad_publication_status" in reasons:
            action = "hard_reject"
            confidence = 0.95
        elif score < self.policy.hard_reject_threshold and negative_hits:
            action = "hard_reject"
            reasons.append("low_score_with_negative_signal")
            confidence = 0.9
        elif score < self.policy.low_threshold:
            action = "reject"
            reasons.append("low_deterministic_relevance")
            confidence = 0.75
        elif (
            score >= self.policy.high_threshold
            and _source_count(paper) >= self.policy.strong_requires_source_count
        ):
            action = "strong"
            reasons.append("high_deterministic_relevance")
            confidence = 0.8
        else:
            action = "review"
            reasons.append("uncertain_band")
            confidence = 0.55
        if action == "strong" and "missing_abstract" in reasons:
            action = "review"
            reasons.append("strong_downgraded_missing_abstract")
            confidence = min(confidence, 0.6)
        return PrefilterDecision(
            action=action,
            score=score,
            confidence=confidence,
            reasons=_dedupe(reasons, cap=12),
            topic_hits=_dedupe(topic_hits, cap=12),
            negative_hits=_dedupe(negative_hits, cap=12),
            paper_key=clean_text(paper.get("paper_key")),
            candidate_key=clean_text(paper.get("candidate_key")),
            features={
                "bm25": bm25,
                "phrase": phrase,
                "title_boost": title_boost,
                "source_prior": source_prior,
                "citation_prior": citation_prior,
                "negative_penalty": negative_penalty,
                "year_penalty": year_penalty,
                "status_penalty": status_penalty,
                "abstract_penalty": abstract_penalty,
                "source_count": _source_count(paper),
                "exact_phrase_count": len(self.exact_phrases),
                "soft_token_count": len(self.bm25_terms),
                "generic_token_count": len(self.positive_term_groups["generic_tokens"]),
                "exact_title_hit_count": len(exact_title_hits),
                "soft_title_hit_count": len(soft_title_hits),
            },
        )

    def partition(
        self,
        candidates: list[dict[str, Any]],
        *,
        protected_keys: set[str] | None = None,
    ) -> tuple[list[PrefilterDecision], list[int]]:
        protected_keys = protected_keys or set()
        self.fit(candidates)
        decisions = [
            self.evaluate(
                candidate,
                index=index,
                protected=clean_text(candidate.get("candidate_key")) in protected_keys,
            )
            for index, candidate in enumerate(candidates)
        ]
        review_indices = prefilter_review_indices(decisions, self.policy)
        return decisions, review_indices


def prefilter_review_indices(decisions: list[PrefilterDecision], policy: PrefilterPolicy) -> list[int]:
    protected = [i for i, decision in enumerate(decisions) if decision.action == "protected"]
    review = [i for i, decision in enumerate(decisions) if decision.action == "review"]
    strong = [i for i, decision in enumerate(decisions) if decision.action == "strong"]
    strong_sample_count = max(policy.strong_audit_min, int(math.ceil(len(strong) * policy.strong_audit_rate))) if strong else 0
    strong_sample = _stratified_strong_sample(strong, decisions, strong_sample_count)
    selected = protected + review + strong_sample
    if policy.max_llm_review_candidates is not None:
        keep_protected = protected
        remaining_cap = max(0, policy.max_llm_review_candidates - len(keep_protected))
        rest = [i for i in selected if i not in set(keep_protected)]
        rest = sorted(rest, key=lambda i: decisions[i].score, reverse=True)[:remaining_cap]
        selected = keep_protected + rest
    return list(dict.fromkeys(selected))


def _stable_sample_key(index: int, decision: PrefilterDecision) -> str:
    raw = decision.candidate_key or decision.paper_key or str(index)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _stratified_strong_sample(
    strong: list[int],
    decisions: list[PrefilterDecision],
    count: int,
) -> list[int]:
    if count <= 0 or not strong:
        return []
    ranked = sorted(strong, key=lambda i: decisions[i].score, reverse=True)
    if count >= len(ranked):
        return ranked
    third = max(1, math.ceil(len(ranked) / 3))
    strata = [ranked[:third], ranked[third: third * 2], ranked[third * 2:]]
    selected: list[int] = []
    base_quota = max(1, count // 3)
    for stratum in strata:
        if not stratum or len(selected) >= count:
            continue
        quota = min(base_quota, count - len(selected), len(stratum))
        selected.extend(sorted(stratum, key=lambda i: _stable_sample_key(i, decisions[i]))[:quota])
    if len(selected) < count:
        selected_set = set(selected)
        leftovers = [i for i in ranked if i not in selected_set]
        selected.extend(
            sorted(leftovers, key=lambda i: _stable_sample_key(i, decisions[i]))[: count - len(selected)]
        )
    return selected[:count]


def summarize_prefilter(decisions: list[PrefilterDecision], *, review_indices: list[int] | None = None) -> dict[str, Any]:
    actions = Counter(decision.action for decision in decisions)
    reasons = Counter(reason for decision in decisions for reason in decision.reasons)
    scores = [decision.score for decision in decisions]
    review_set = set(review_indices or [])
    llm_count = len(review_set) if review_indices is not None else actions.get("review", 0)
    return {
        "policy_version": PREFILTER_POLICY_VERSION,
        "candidate_count": len(decisions),
        "action_counts": dict(actions),
        "average_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "topic_hit_count": sum(1 for decision in decisions if decision.topic_hits),
        "negative_hit_count": sum(1 for decision in decisions if decision.negative_hits),
        "llm_review_count": llm_count,
        "llm_review_ratio": round(llm_count / len(decisions), 3) if decisions else 0.0,
        "top_reasons": dict(reasons.most_common(8)),
    }


def run_prefilter_workspace(workspace: Path) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    topic = load_topic_config(workspace)
    candidates = read_json(data_dir(workspace) / "pending_review_candidates.json", [])
    if not isinstance(candidates, list):
        candidates = []
    candidate_dicts = [candidate if isinstance(candidate, dict) else {} for candidate in candidates]
    prefilter_cfg = topic.get("prefilter") if isinstance(topic.get("prefilter"), dict) else {}
    pipeline = PrefilterPipeline(topic, prefilter_cfg)
    protected_keys = {
        clean_text(candidate.get("candidate_key"))
        for candidate in candidate_dicts
        if clean_text(candidate.get("candidate_key")) and clean_text(candidate.get("required_seed_role"))
    }
    decisions, review_indices = pipeline.partition(candidate_dicts, protected_keys=protected_keys)
    out_path = data_dir(workspace) / "prefilter_decisions.jsonl"
    review_set = set(review_indices)
    write_jsonl(
        out_path,
        [
            {
                **decision.to_dict(),
                "candidate_key": decision.candidate_key or clean_text(candidate_dicts[idx].get("candidate_key")),
                "title": candidate_dicts[idx].get("title", ""),
                "year": candidate_dicts[idx].get("year"),
                "sent_to_llm": idx in review_set,
            }
            for idx, decision in enumerate(decisions)
        ],
    )
    return {
        "schema_version": "papercompass.prefilter_run.v1",
        "workspace": workspace_label(workspace),
        "summary": summarize_prefilter(decisions, review_indices=review_indices),
        "decisions": workspace_relative_path(workspace, out_path),
    }

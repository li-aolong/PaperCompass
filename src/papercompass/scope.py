from __future__ import annotations

import re
from typing import Any

from .roles import BACKGROUND_ANCHOR, normalize_role
from .text import as_list, clean_text


PROFILE_VENUES: dict[str, list[str]] = {
    "nlp_top_ai": [
        "ACL",
        "EMNLP",
        "NAACL",
        "COLING",
        "Findings",
        "EACL",
        "AACL",
        "TACL",
        "AAAI",
        "IJCAI",
        "ICLR",
        "NeurIPS",
        "ICML",
    ],
    "ml_top": ["ICLR", "NeurIPS", "ICML", "AAAI", "IJCAI"],
    "cv_top": ["CVPR", "ICCV", "ECCV", "NeurIPS", "ICLR", "ICML"],
    "ir_web_data_top": ["SIGIR", "WWW", "KDD", "WSDM", "CIKM", "ACL", "EMNLP"],
    "systems_top": ["OSDI", "SOSP", "NSDI", "SIGCOMM", "MobiCom"],
    "general_top_cs": [
        "ACL",
        "EMNLP",
        "NAACL",
        "COLING",
        "Findings",
        "AAAI",
        "ICLR",
        "NeurIPS",
        "ICML",
        "IJCAI",
        "CVPR",
        "ICCV",
        "ECCV",
        "SIGIR",
        "WWW",
        "KDD",
        "SIGMOD",
        "VLDB",
        "OSDI",
        "SOSP",
        "NSDI",
        "MobiCom",
        "SIGCOMM",
    ],
}

DEFAULT_TOP_VENUES: list[str] = PROFILE_VENUES["general_top_cs"]


VENUE_ALIASES: dict[str, list[str]] = {
    "ACL": ["acl", "annual meeting of the association for computational linguistics"],
    "EMNLP": ["emnlp", "empirical methods in natural language processing"],
    "NAACL": ["naacl", "north american chapter of the association for computational linguistics"],
    "COLING": ["coling", "computational linguistics"],
    "Findings": ["findings", "findings of the association for computational linguistics"],
    "EACL": ["eacl", "european chapter of the association for computational linguistics"],
    "AACL": ["aacl", "asia-pacific chapter of the association for computational linguistics"],
    "TACL": ["tacl", "transactions of the association for computational linguistics"],
    "AAAI": ["aaai", "association for the advancement of artificial intelligence"],
    "ICLR": ["iclr", "learning representations"],
    "NeurIPS": ["neurips", "nips", "neural information processing systems"],
    "ICML": ["icml", "international conference on machine learning"],
    "IJCAI": ["ijcai"],
    "CVPR": ["cvpr", "computer vision and pattern recognition"],
    "ICCV": ["iccv", "international conference on computer vision"],
    "ECCV": ["eccv", "european conference on computer vision"],
    "SIGIR": ["sigir", "information retrieval"],
    "WWW": ["www", "web conference", "world wide web conference"],
    "KDD": ["kdd", "knowledge discovery and data mining"],
    "WSDM": ["wsdm"],
    "CIKM": ["cikm"],
    "SIGMOD": ["sigmod"],
    "VLDB": ["vldb"],
    "OSDI": ["osdi"],
    "SOSP": ["sosp"],
    "NSDI": ["nsdi"],
    "MobiCom": ["mobicom"],
    "SIGCOMM": ["sigcomm"],
}


_GENERIC_TOP_VENUE_PATTERNS = (
    "顶会",
    "顶级会议",
    "一流会议",
    "主会",
    "top conference",
    "top-tier conference",
    "top tier conference",
    "premier conference",
)
_PREPRINT_PATTERNS = ("arxiv", "preprint", "预印本", "新近 arxiv", "较新的 arxiv", "新的 arxiv")
_EXHAUSTIVE_PATTERNS = ("仅限", "只限", "只包括", "限定为", "只要", "仅", "only", "strictly", "limited to")

_DOMAIN_PROFILE_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "nlp_top_ai",
        (
            "nlp",
            "natural language",
            "language model",
            "large language model",
            "llm",
            "grammar",
            "grammatical",
            "spelling correction",
            "spell checking",
            "cgec",
            "csc",
            "text correction",
            "chinese",
            "中文",
            "语法",
            "拼写",
            "纠错",
            "语言模型",
        ),
    ),
    ("cv_top", ("vision", "image", "video", "multimodal", "computer vision", "视觉", "图像", "视频")),
    ("ir_web_data_top", ("retrieval", "search", "recommendation", "web", "ranking", "information retrieval", "检索", "推荐")),
    ("systems_top", ("network", "wireless", "distributed system", "operating system", "systems", "网络", "系统")),
    ("ml_top", ("machine learning", "deep learning", "reinforcement learning", "representation learning", "机器学习", "强化学习")),
]


def _normalize_blob(value: str) -> str:
    return re.sub(r"\s+", " ", clean_text(value)).strip().lower()


def _mentioned_venues(text: str) -> list[str]:
    blob = _normalize_blob(text)
    out: list[str] = []
    for venue, aliases in VENUE_ALIASES.items():
        if any(re.search(rf"(?<![a-z0-9]){re.escape(alias.lower())}(?![a-z0-9])", blob) for alias in aliases):
            out.append(venue)
    return _dedupe(out)


def _has_exhaustive_language(text: str) -> bool:
    blob = _normalize_blob(text)
    return any(pattern in blob for pattern in _EXHAUSTIVE_PATTERNS)


def _infer_venue_profile(text: str, mentioned: list[str]) -> str:
    blob = _normalize_blob(text)
    mentioned_set = {item.lower() for item in mentioned}
    if mentioned_set & {item.lower() for item in PROFILE_VENUES["nlp_top_ai"][:8]}:
        return "nlp_top_ai"
    if mentioned_set & {item.lower() for item in PROFILE_VENUES["cv_top"][:3]}:
        return "cv_top"
    for profile, needles in _DOMAIN_PROFILE_RULES:
        if any(needle in blob for needle in needles):
            return profile
    return "general_top_cs"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def infer_publication_scope(
    direction: str,
    search_hints: list[str] | None = None,
) -> dict[str, Any]:
    """Infer deterministic publication constraints from user-facing text.

    This deliberately only handles explicit scope language. The brain may still
    discuss venues semantically, but code owns the hard workflow fields.
    """
    text = "\n".join([direction, *(search_hints or [])])
    blob = _normalize_blob(text)
    mentioned = _mentioned_venues(text)
    wants_top_venues = bool(mentioned) or any(pattern in blob for pattern in _GENERIC_TOP_VENUE_PATTERNS)
    include_preprints = any(pattern in blob for pattern in _PREPRINT_PATTERNS)
    if not wants_top_venues and not include_preprints:
        return {}
    profile = _infer_venue_profile(text, mentioned) if wants_top_venues else ""
    strict_venue_list = _has_exhaustive_language(text)
    profile_venues = PROFILE_VENUES.get(profile, DEFAULT_TOP_VENUES if wants_top_venues else [])
    venues = mentioned if strict_venue_list else _dedupe([*profile_venues, *mentioned])
    scope = {
        "policy": "preferred_venues_or_preprints",
        "strict": bool(wants_top_venues),
        "venue_profile": profile,
        "strict_venue_list": strict_venue_list,
        "explicit_venues": mentioned,
        "preferred_venues": venues,
        "include_preprints": include_preprints,
        "reason": "从用户方向中的 venue/preprint 范围词自动推断",
    }
    return {key: value for key, value in scope.items() if value not in ([], "", None)}


def publication_scope_from_topic(topic: dict[str, Any]) -> dict[str, Any]:
    configured = topic.get("publication_scope")
    if isinstance(configured, dict):
        return normalize_publication_scope(
            configured,
            clean_text(topic.get("direction_raw") or topic.get("description") or topic.get("name")),
            [clean_text(item) for item in as_list(topic.get("search_hints")) if clean_text(item)],
        )
    hints = [clean_text(item) for item in as_list(topic.get("search_hints")) if clean_text(item)]
    return infer_publication_scope(
        clean_text(topic.get("direction_raw") or topic.get("description") or topic.get("name")),
        hints,
    )


def normalize_publication_scope(
    configured: dict[str, Any],
    direction: str = "",
    search_hints: list[str] | None = None,
) -> dict[str, Any]:
    text = "\n".join([direction, *(search_hints or []), " ".join(as_list(configured.get("preferred_venues")))])
    explicit = [clean_text(v) for v in as_list(configured.get("preferred_venues")) if clean_text(v)]
    explicit.extend(v for v in _mentioned_venues(text) if v.lower() not in {x.lower() for x in explicit})
    profile = clean_text(configured.get("venue_profile"))
    if profile not in PROFILE_VENUES:
        profile = _infer_venue_profile(text, explicit) if explicit else ""
    strict_venue_list = bool(
        configured.get("strict_venue_list")
        or configured.get("exact_venues")
        or configured.get("venues_are_exhaustive")
    )
    if not strict_venue_list and _has_exhaustive_language(text):
        strict_venue_list = True
    include = [clean_text(v) for v in as_list(configured.get("include_venues") or configured.get("additional_venues")) if clean_text(v)]
    excluded = [clean_text(v) for v in as_list(configured.get("excluded_venues") or configured.get("exclude_venues")) if clean_text(v)]
    base = explicit if strict_venue_list else [*(PROFILE_VENUES.get(profile, []) if profile else []), *explicit]
    venues = _dedupe([*base, *include])
    excluded_keys = {v.lower() for v in excluded}
    venues = [v for v in venues if v.lower() not in excluded_keys]
    scope = dict(configured)
    scope["preferred_venues"] = venues
    scope["explicit_venues"] = _dedupe(explicit)
    scope["strict_venue_list"] = strict_venue_list
    if profile:
        scope["venue_profile"] = profile
    if excluded:
        scope["excluded_venues"] = _dedupe(excluded)
    return {key: value for key, value in scope.items() if value not in ([], "", None)}


def render_publication_scope(scope: dict[str, Any]) -> str:
    if not scope:
        return "未配置明确 publication/source scope。按研究主题相关性评分。"
    venues = ", ".join(scope.get("preferred_venues") or []) or "未限定"
    profile = clean_text(scope.get("venue_profile") or "")
    preprints = "允许 arXiv/preprint" if scope.get("include_preprints") else "不额外允许 preprint"
    strict = "严格清单" if scope.get("strict_venue_list") else "严格约束" if scope.get("strict", True) else "软约束"
    profile_text = f"Venue profile: {profile}. " if profile else ""
    return (
        f"{strict}：主库论文应来自 preferred venues 或允许的 preprint。"
        f"{profile_text}Preferred venues: {venues}. {preprints}。"
        "明显不在该 publication scope 的候选，除非是 seed/背景锚点，"
        "brain relevance score 应低于 60。"
    )


def _paper_is_preprint(paper: dict[str, Any]) -> bool:
    venue = _normalize_blob(clean_text(paper.get("venue")))
    sources = [_normalize_blob(item) for item in as_list(paper.get("sources"))]
    tags = [_normalize_blob(item) for item in as_list(paper.get("tags"))]
    ids = paper.get("ids") if isinstance(paper.get("ids"), dict) else {}
    urls = paper.get("urls") if isinstance(paper.get("urls"), dict) else {}
    url_blob = " ".join(clean_text(urls.get(key) or paper.get(key) or "") for key in ("landing", "pdf", "url", "pdf_url"))
    return (
        "arxiv" in venue
        or any("arxiv" in item for item in sources)
        or any("arxiv" in item or "preprint" in item for item in tags)
        or bool(ids.get("arxiv") or paper.get("arxiv_id"))
        or "arxiv.org" in url_blob.lower()
    )


def _paper_matches_venue(paper: dict[str, Any], preferred_venues: list[str]) -> bool:
    venue_blob = _normalize_blob(clean_text(paper.get("venue")))
    if not venue_blob:
        return False
    for venue in preferred_venues:
        for alias in VENUE_ALIASES.get(venue, [venue]):
            alias_blob = alias.lower()
            if re.search(rf"(?<![a-z0-9]){re.escape(alias_blob)}(?![a-z0-9])", venue_blob):
                return True
    return False


def _is_must_recall_paper(paper: dict[str, Any]) -> bool:
    """Paper came from a user-pinned must_recall seed query."""
    if paper.get("seed_required") is True:
        return True
    force_reason = clean_text(paper.get("force_include_reason"))
    if force_reason and "must_recall" in force_reason.lower():
        return True
    strength = clean_text(paper.get("query_strength"))
    if strength and strength.lower() == "must_recall":
        return True
    return False


def paper_matches_publication_scope(paper: dict[str, Any], scope: dict[str, Any]) -> tuple[bool, str]:
    if not scope or not scope.get("strict", True):
        return True, "scope_not_active"
    if _is_must_recall_paper(paper):
        return True, "must_recall_exempted"
    role = normalize_role(paper.get("paper_role") or paper.get("role"))
    if role == BACKGROUND_ANCHOR:
        return True, "background_anchor_allowed"
    excluded = [clean_text(v) for v in as_list(scope.get("excluded_venues") or scope.get("exclude_venues")) if clean_text(v)]
    if excluded and _paper_matches_venue(paper, excluded):
        return False, "excluded_venue"
    is_preprint = _paper_is_preprint(paper)
    if is_preprint and scope.get("include_preprints"):
        return True, "preprint_allowed"
    venues = [clean_text(v) for v in as_list(scope.get("preferred_venues")) if clean_text(v)]
    if venues and _paper_matches_venue(paper, venues):
        return True, "preferred_venue_match"
    if not venues:
        return True, "no_preferred_venues_configured"
    if is_preprint:
        return False, "preprint_not_allowed_by_scope"
    return False, "outside_publication_scope"


def publication_scope_report(papers: list[dict[str, Any]], topic: dict[str, Any]) -> dict[str, Any]:
    scope = publication_scope_from_topic(topic)
    if not scope or not scope.get("strict", True):
        return {"active": bool(scope), "status": "not_strict", "scope": scope, "violation_count": 0, "violation_examples": []}
    violations: list[dict[str, Any]] = []
    for paper in papers:
        matched, reason = paper_matches_publication_scope(paper, scope)
        if matched:
            continue
        violations.append({
            "title": paper.get("title"),
            "year": paper.get("year"),
            "venue": paper.get("venue"),
            "paper_role": paper.get("paper_role") or paper.get("role"),
            "reason": reason,
        })
    by_venue: dict[str, int] = {}
    for item in violations:
        venue = clean_text(item.get("venue") or "N/A")
        by_venue[venue] = by_venue.get(venue, 0) + 1
    return {
        "active": True,
        "status": "violations" if violations else "ok",
        "scope": scope,
        "violation_count": len(violations),
        "violation_by_venue": sorted(by_venue.items(), key=lambda item: item[1], reverse=True),
        "violation_examples": violations[:30],
        "violations": violations,
    }

from __future__ import annotations

from typing import Any


CORE_METHOD = "core_method"
MECHANISM_EVAL = "mechanism_eval"
BACKGROUND_ANCHOR = "background_anchor"
BOUNDARY_NEGATIVE = "boundary_negative"
OUT_OF_SCOPE = "out_of_scope"

PAPER_ROLES = {
    CORE_METHOD,
    MECHANISM_EVAL,
    BACKGROUND_ANCHOR,
    BOUNDARY_NEGATIVE,
    OUT_OF_SCOPE,
}

MAIN_LIBRARY_ROLES = {CORE_METHOD, MECHANISM_EVAL}
ANCHOR_ROLES = {BACKGROUND_ANCHOR}
NEGATIVE_ROLES = {BOUNDARY_NEGATIVE, OUT_OF_SCOPE}


def normalize_role(value: Any, default: str = CORE_METHOD) -> str:
    role = str(value or "").strip().lower().replace("-", "_")
    if role in {"core", "method", "main", "in_scope"}:
        return CORE_METHOD
    if role in {"mechanism", "evaluation", "mechanism_evaluation", "faithfulness"}:
        return MECHANISM_EVAL
    if role in {"anchor", "background", "background_paper", "context"}:
        return BACKGROUND_ANCHOR
    if role in {"negative", "boundary", "contrast", "counterexample", "counter_example"}:
        return BOUNDARY_NEGATIVE
    if role in {"reject", "rejected", "off_topic"}:
        return OUT_OF_SCOPE
    return role if role in PAPER_ROLES else default


def role_bucket(role: Any) -> str:
    normalized = normalize_role(role)
    if normalized in MAIN_LIBRARY_ROLES:
        return "core"
    if normalized in ANCHOR_ROLES:
        return "anchor"
    if normalized in NEGATIVE_ROLES:
        return "negative"
    return "core"


def seed_required(seed: dict[str, Any]) -> bool:
    if not isinstance(seed, dict):
        return False
    value = seed.get("required")
    if value is None:
        return normalize_role(seed.get("paper_role") or seed.get("role")) not in NEGATIVE_ROLES
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "n"}


def seed_source_evidence(seed: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(seed, dict):
        return {}
    evidence = seed.get("evidence") or seed.get("seed_evidence") or {}
    return evidence if isinstance(evidence, dict) else {}


def seed_has_source_evidence(seed: dict[str, Any]) -> bool:
    evidence = seed_source_evidence(seed)
    source = str(evidence.get("source") or "").strip()
    match_type = str(evidence.get("match_type") or "").strip()
    source_item = str(evidence.get("source_item_id") or evidence.get("source_url") or "").strip()
    return bool(source and match_type and source_item)


def seed_verified_source_backed(seed: dict[str, Any]) -> bool:
    if not isinstance(seed, dict):
        return False
    return seed.get("verified") is True and seed_has_source_evidence(seed)

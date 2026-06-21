from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime
from typing import Any

from ..config import cache_dir
from ..confirmation import sha256_json, sha256_text
from ..text import append_jsonl_locked, as_list, clean_text, iter_jsonl, normalize_title, parse_year


REVIEW_PROMPT_VERSION = "papercompass.brain_score.v2"
REVIEW_SCHEMA_VERSION = "papercompass.review_schema.v2"
REVIEW_POLICY_VERSION = "papercompass.review_policy.v2"
REFLECTION_PROMPT_VERSION = "papercompass.reflection.v1"
REFLECTION_SCHEMA_VERSION = "papercompass.reflection_schema.v1"
REFLECTION_POLICY_VERSION = "papercompass.boundary_reflection_policy.v1"


def candidate_fingerprint(candidate: dict[str, Any]) -> str:
    abstract = clean_text(candidate.get("abstract") or candidate.get("summary"))
    payload = {
        "title": normalize_title(candidate.get("title", "")),
        "year": parse_year(candidate.get("year")),
        "venue": clean_text(candidate.get("venue")),
        "abstract_hash": sha256_text(abstract) if abstract else "",
        "ids": candidate.get("ids") if isinstance(candidate.get("ids"), dict) else {
            key: clean_text(candidate.get(key))
            for key in ("doi", "arxiv_id", "acl_id", "semantic_scholar_id")
            if clean_text(candidate.get(key))
        },
        "sources": sorted(as_list(candidate.get("sources"))),
        "publication_status": clean_text(candidate.get("publication_status")),
    }
    return sha256_json(payload)


def topic_context_hash(topic: dict[str, Any]) -> str:
    payload = {
        "topic_id": clean_text(topic.get("topic_id")),
        "direction_raw": clean_text(topic.get("direction_raw") or topic.get("name")),
        "min_year": topic.get("min_year"),
        "source_filter_terms": sorted(as_list(topic.get("source_filter_terms"))),
        "discriminator_terms": sorted(as_list(topic.get("discriminator_terms"))),
        "negative_terms": sorted(as_list(topic.get("negative_terms") or topic.get("out_of_scope_terms"))),
        "publication_scope": topic.get("publication_scope") if isinstance(topic.get("publication_scope"), dict) else {},
        "judge_examples": topic.get("judge_examples") if isinstance(topic.get("judge_examples"), dict) else {},
        "prefilter": topic.get("prefilter") if isinstance(topic.get("prefilter"), dict) else {},
        "fusion": topic.get("fusion") if isinstance(topic.get("fusion"), dict) else {},
    }
    return sha256_json(payload)


def brain_model_name(brain: Any) -> str:
    env_specific = os.environ.get(f"PAPERCOMPASS_{clean_text(getattr(brain, 'name', '')).upper()}_MODEL", "")
    if env_specific:
        return clean_text(env_specific)
    model_fn = getattr(brain, "_model", None)
    if callable(model_fn):
        try:
            model = model_fn()
            if clean_text(model):
                revision_fn = getattr(brain, "_model_revision", None)
                revision = ""
                if callable(revision_fn):
                    with_revision = revision_fn()
                    revision = clean_text(with_revision)
                elif os.environ.get("PAPERCOMPASS_BRAIN_MODEL_REVISION"):
                    revision = clean_text(os.environ.get("PAPERCOMPASS_BRAIN_MODEL_REVISION"))
                return clean_text(f"{model}@{revision}") if revision else clean_text(model)
        except Exception:
            pass
    model = clean_text(getattr(brain, "model", "") or os.environ.get("PAPERCOMPASS_BRAIN_MODEL", ""))
    revision = clean_text(os.environ.get("PAPERCOMPASS_BRAIN_MODEL_REVISION"))
    return clean_text(f"{model}@{revision}") if model and revision else model


def review_cache_key(
    *,
    candidate: dict[str, Any],
    topic_hash: str,
    brain_name: str,
    model_name: str,
    prompt_version: str = REVIEW_PROMPT_VERSION,
    schema_version: str = REVIEW_SCHEMA_VERSION,
    policy_version: str = REVIEW_POLICY_VERSION,
) -> str:
    return sha256_json({
        "candidate_fingerprint": candidate_fingerprint(candidate),
        "topic_context_hash": topic_hash,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "policy_version": policy_version,
        "brain_name": clean_text(brain_name),
        "model_name": clean_text(model_name),
    })


def review_cache_path(workspace: Path) -> Path:
    return cache_dir(workspace) / "review" / "brain_scores.v2.jsonl"


def reflection_cache_path(workspace: Path) -> Path:
    return cache_dir(workspace) / "review" / "reflections.v1.jsonl"


def _corrupt_rows_path(workspace: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return cache_dir(workspace) / "review" / f"corrupt_rows_{stamp}.jsonl"


def load_review_cache(workspace: Path, *, strict: bool = False) -> dict[str, dict[str, Any]]:
    path = review_cache_path(workspace)
    if not path.exists():
        return {}
    cache: dict[str, dict[str, Any]] = {}
    corrupt: list[dict[str, Any]] = []
    try:
        rows = list(iter_jsonl(path))
    except ValueError:
        if strict:
            raise
        rows = []
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    import json

                    rows.append(json.loads(stripped))
                except Exception as exc:  # noqa: BLE001
                    corrupt.append({"line_no": line_no, "line": stripped[:2000], "error": str(exc)})
        append_jsonl_locked(_corrupt_rows_path(workspace), corrupt)
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = clean_text(row.get("cache_key"))
        decision = row.get("decision")
        if key and isinstance(decision, dict):
            cache[key] = row
    return cache


def append_review_cache_rows(workspace: Path, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    return append_jsonl_locked(review_cache_path(workspace), rows)


def append_reflection_cache_rows(workspace: Path, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    return append_jsonl_locked(reflection_cache_path(workspace), rows)

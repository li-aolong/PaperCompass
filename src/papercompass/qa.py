from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
from pathlib import Path
from typing import Any

from .anchors import existing_anchors_plan_path, iter_anchor_rows
from .build import iter_raw_candidates
from .candidate_review import (
    applied_decisions_path,
    candidate_key,
    validate_review_decisions,
    workspace_decision_context_hash,
)
from .config import (
    catalog_dir,
    data_dir,
    load_sources_config,
    load_topic_config,
    manifests_dir,
    portable_workspace_data,
    raw_dir,
    state_dir,
    workspace_label,
    workspace_relative_path,
)
from .normalize import identity_keys
from .roles import (
    ANCHOR_ROLES,
    BACKGROUND_ANCHOR,
    CORE_METHOD,
    MAIN_LIBRARY_ROLES,
    MECHANISM_EVAL,
    normalize_role,
    role_bucket,
    seed_has_source_evidence,
    seed_required,
    seed_verified_source_backed,
)
from .scope import publication_scope_report
from .text import (
    atomic_write_text,
    clean_text,
    is_derived_tag,
    iter_jsonl,
    normalize_title,
    read_json,
    workspace_lock,
    write_json,
    write_jsonl,
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def read_list(path: Path) -> list[dict[str, Any]]:
    data = read_json(path, [])
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def key_for(item: dict[str, Any]) -> str:
    return candidate_key(item) or (identity_keys(item)[0] if identity_keys(item) else "")


def duplicate_report(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    examples: dict[str, dict[str, Any]] = {}
    for item in items:
        key = key_for(item)
        if not key:
            continue
        counts[key] += 1
        examples.setdefault(key, {"title": item.get("title"), "year": item.get("year"), "key": key})
    duplicate_keys = [key for key, count in counts.items() if count > 1]
    return {
        "row_count": len(items),
        "unique_count": len(counts),
        "duplicate_key_count": len(duplicate_keys),
        "duplicate_extra_rows": sum(counts[key] - 1 for key in duplicate_keys),
        "examples": [examples[key] | {"count": counts[key]} for key in duplicate_keys[:20]],
    }


def raw_pollution_report(workspace: Path) -> dict[str, Any]:
    polluted_files: dict[str, int] = defaultdict(int)
    polluted_tags: Counter[str] = Counter()
    raw_root = raw_dir(workspace)
    if not raw_root.exists():
        return {"polluted_file_count": 0, "polluted_row_count": 0, "polluted_tags": {}, "examples": []}
    row_count = 0
    for path in sorted(raw_root.glob("**/*.jsonl")):
        for row in iter_jsonl(path):
            if not isinstance(row, dict):
                continue
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else row
            tags = raw.get("tags") if isinstance(raw, dict) else []
            bad = [clean_text(tag) for tag in tags or [] if is_derived_tag(tag)]
            if bad:
                row_count += 1
                rel = str(path.relative_to(workspace))
                polluted_files[rel] += 1
                for tag in bad:
                    polluted_tags[tag] += 1
    return {
        "polluted_file_count": len(polluted_files),
        "polluted_row_count": row_count,
        "polluted_tags": dict(polluted_tags.most_common(20)),
        "examples": [{"path": path, "rows": rows} for path, rows in list(polluted_files.items())[:20]],
    }


def latest_file(directory: Path, pattern: str) -> Path | None:
    files = sorted(directory.glob(pattern))
    return files[-1] if files else None


def review_report(workspace: Path) -> dict[str, Any]:
    reviews = state_dir(workspace) / "reviews"
    queue = latest_file(reviews, "weak_candidates_*.json")
    if not queue:
        return {"status": "no_queue", "queue_count": 0, "decision_count": 0, "deferred_count": 0}
    queue_run = queue.stem.removeprefix("weak_candidates_")
    preferred = reviews / f"review_decisions_{queue_run}.jsonl"
    candidates: list[Path] = []
    candidates.extend(sorted(reviews.glob("review_decisions_resolved_*.jsonl"), reverse=True))
    if preferred.exists():
        candidates.append(preferred)
    candidates.extend(
        path
        for path in sorted(reviews.glob("review_decisions_*.jsonl"), reverse=True)
        if path not in candidates
    )
    attempted: list[dict[str, Any]] = []
    selected: tuple[Path, dict[str, Any]] | None = None
    for decisions in candidates:
        validation = validate_review_decisions(queue, decisions)
        attempted.append({
            "decisions": str(decisions),
            "valid": bool(validation.get("valid")),
            "coverage": validation.get("coverage", {}),
        })
        if validation.get("valid"):
            selected = (decisions, validation)
            break
    if not selected:
        data = read_json(queue, {})
        items = data.get("review_candidates") if isinstance(data, dict) else []
        if candidates and attempted:
            return {
                "status": "invalid",
                "queue": str(queue),
                "queue_count": len(items or []),
                "decision_count": 0,
                "deferred_count": 0,
                "attempted_decisions": attempted[:10],
            }
        return {"status": "no_decisions", "queue": str(queue), "queue_count": len(items or []), "decision_count": 0, "deferred_count": 0}
    decisions, validation = selected
    return {
        "status": "valid" if validation.get("valid") else "invalid",
        "queue": str(queue),
        "decisions": str(decisions),
        "queue_count": validation.get("queue_count", 0),
        "decision_count": validation.get("decision_count", 0),
        "decision_counts": validation.get("decision_counts", {}),
        "deferred_count": (validation.get("decision_counts") or {}).get("defer", 0),
        "coverage": validation.get("coverage", {}),
        "validation": validation,
    }


def applied_review_decisions_report(workspace: Path) -> dict[str, Any]:
    path = applied_decisions_path(workspace)
    current_context_hash = workspace_decision_context_hash(workspace)
    if not path.exists():
        return {
            "status": "not_configured",
            "path": workspace_relative_path(workspace, path),
            "decision_count": 0,
            "matching_context_count": 0,
            "stale_context_count": 0,
            "invalid_decision_count": 0,
            "current_context_hash": current_context_hash,
            "decision_counts": {},
            "stale_context_hashes": {},
            "stale_examples": [],
        }

    decision_count = 0
    matching_context_count = 0
    stale_context_count = 0
    invalid_decision_count = 0
    decision_counts: Counter[str] = Counter()
    stale_context_hashes: Counter[str] = Counter()
    stale_examples: list[dict[str, Any]] = []
    for row in iter_jsonl(path):
        if not isinstance(row, dict):
            invalid_decision_count += 1
            continue
        decision = clean_text(row.get("decision")).lower()
        if decision not in {"accept", "reject", "defer", "anchor"}:
            invalid_decision_count += 1
            continue
        decision_count += 1
        decision_counts[decision] += 1
        decision_context_hash = clean_text(row.get("decision_context_hash"))
        if decision_context_hash == current_context_hash:
            matching_context_count += 1
            continue
        stale_context_count += 1
        stale_context_hashes[decision_context_hash or "<missing>"] += 1
        if len(stale_examples) < 20:
            stale_examples.append({
                "candidate_key": row.get("candidate_key"),
                "title": row.get("title"),
                "year": row.get("year"),
                "decision": decision,
                "decision_context_hash": decision_context_hash,
            })

    if not decision_count and invalid_decision_count:
        status = "invalid"
    elif stale_context_count and not matching_context_count:
        status = "stale_context"
    elif stale_context_count:
        status = "partial_stale_context"
    else:
        status = "valid"
    return {
        "status": status,
        "path": workspace_relative_path(workspace, path),
        "decision_count": decision_count,
        "matching_context_count": matching_context_count,
        "stale_context_count": stale_context_count,
        "invalid_decision_count": invalid_decision_count,
        "current_context_hash": current_context_hash,
        "decision_counts": dict(decision_counts),
        "stale_context_hashes": dict(stale_context_hashes),
        "stale_examples": stale_examples,
    }


def score_decisions_report(workspace: Path) -> dict[str, Any]:
    reviews = state_dir(workspace) / "reviews"
    score_path = latest_file(reviews, "score_decisions_*.jsonl")
    if not score_path:
        return {
            "status": "no_score_decisions",
            "decision_count": 0,
            "no_brain_count": 0,
            "no_brain_verdict_violation_count": 0,
            "no_brain_verdict_violation_examples": [],
        }
    decision_count = 0
    no_brain_count = 0
    violation_count = 0
    violations: list[dict[str, Any]] = []
    verdict_counts: Counter[str] = Counter()
    for row in iter_jsonl(score_path):
        if not isinstance(row, dict):
            continue
        decision_count += 1
        verdict = clean_text(row.get("verdict"))
        verdict_counts[verdict or "unknown"] += 1
        if row.get("brain_score") is None:
            no_brain_count += 1
            prefilter_action = clean_text(row.get("prefilter_action"))
            if verdict in {"in_scope", "boundary"} and prefilter_action not in {"reject", "hard_reject", "strong"}:
                violation_count += 1
                if len(violations) < 30:
                    violations.append({
                        "candidate_key": row.get("candidate_key"),
                        "title": row.get("title"),
                        "year": row.get("year"),
                        "verdict": verdict,
                        "policy": row.get("policy"),
                        "score": row.get("score"),
                        "emb_score": row.get("emb_score"),
                        "meta_score": row.get("meta_score"),
                    })
    return {
        "status": "checked",
        "path": str(score_path),
        "decision_count": decision_count,
        "verdict_counts": dict(verdict_counts),
        "no_brain_count": no_brain_count,
        "no_brain_verdict_violation_count": violation_count,
        "no_brain_verdict_violation_examples": violations,
    }


def prefilter_efficiency_report(workspace: Path) -> dict[str, Any]:
    path = data_dir(workspace) / "prefilter_decisions.jsonl"
    if not path.exists():
        return {"status": "missing", "path": workspace_relative_path(workspace, path)}
    rows = [row for row in iter_jsonl(path) if isinstance(row, dict)]
    actions = Counter(clean_text(row.get("action")) or "unknown" for row in rows)
    reasons = Counter(
        clean_text(reason)
        for row in rows
        for reason in (row.get("reasons") or [])
        if clean_text(reason)
    )
    sent_to_llm = sum(1 for row in rows if bool(row.get("sent_to_llm")))
    rejected = actions.get("reject", 0) + actions.get("hard_reject", 0)
    return {
        "status": "checked",
        "path": workspace_relative_path(workspace, path),
        "candidate_count": len(rows),
        "action_counts": dict(actions),
        "sent_to_llm": sent_to_llm,
        "llm_review_ratio": round(sent_to_llm / len(rows), 3) if rows else 0.0,
        "deterministic_reject_count": rejected,
        "top_reject_reasons": dict(reasons.most_common(10)),
    }


def source_preflight_report(workspace: Path) -> dict[str, Any]:
    latest = manifests_dir(workspace) / "source_preflight_latest.json"
    payload = read_json(latest, {})
    if not isinstance(payload, dict):
        payload = {}
    rows = payload.get("preflight") if isinstance(payload.get("preflight"), list) else []
    if not rows:
        return {
            "status": "missing",
            "path": workspace_relative_path(workspace, latest),
            "source_count": 0,
            "warning_count": 0,
            "blocked_count": 0,
            "warnings": [],
            "blocked_sources": [],
        }
    warning_rows = [
        row for row in rows
        if isinstance(row, dict) and row.get("warnings")
    ]
    blocked = [
        clean_text(row.get("source"))
        for row in rows
        if isinstance(row, dict) and clean_text(row.get("status")).lower() == "blocked"
    ]
    return {
        "status": "checked",
        "path": workspace_relative_path(workspace, latest),
        "manifest": workspace_relative_path(workspace, payload.get("manifest", latest)),
        "source_count": len(rows),
        "warning_count": len(warning_rows),
        "blocked_count": len(blocked),
        "warnings": warning_rows[:20],
        "blocked_sources": [source for source in blocked if source],
    }


def coverage_report(workspace: Path) -> dict[str, Any]:
    coverage = read_json(manifests_dir(workspace) / "source_coverage.json", {})
    if not isinstance(coverage, dict):
        coverage = {}
    statuses: Counter[str] = Counter()
    risks: Counter[str] = Counter()
    hard_examples: list[dict[str, Any]] = []
    medium_examples: list[dict[str, Any]] = []
    optional_examples: list[dict[str, Any]] = []
    auth_problem_examples: list[dict[str, Any]] = []
    for key, item in coverage.items():
        if not isinstance(item, dict):
            continue
        status = clean_text(item.get("execution_status") or item.get("status") or "unknown")
        statuses[status] += 1
        explicit_risk = clean_text(item.get("coverage_risk"))
        risk = explicit_risk or "unknown"
        risks[risk] += 1
        source_exhausted = item.get("source_exhausted")
        optional_source = bool(item.get("optional_source"))
        auth_problem = clean_text(item.get("auth_problem"))
        auth_hint = clean_text(item.get("auth_hint"))
        errors = item.get("errors", [])
        error_blob = " ".join(clean_text(error).lower() for error in errors if clean_text(error))
        authenticated = item.get("authenticated")
        if (
            not auth_problem
            and item.get("source") == "semanticscholar"
            and authenticated is True
            and ("403" in error_blob or "forbidden" in error_blob)
        ):
            auth_problem = "semanticscholar_api_key_forbidden"
            auth_hint = (
                "旧 coverage 记录显示 Semantic Scholar 带 key 请求被 403 拒绝；"
                "请检查 SEMANTIC_SCHOLAR_API_KEY 是否有效、额度或权限是否可用。"
            )
        if (
            not auth_problem
            and item.get("source") == "semanticscholar"
            and authenticated is True
            and ("401" in error_blob or "unauthorized" in error_blob)
        ):
            auth_problem = "semanticscholar_api_key_unauthorized"
            auth_hint = "旧 coverage 记录显示 Semantic Scholar key 未通过认证；请检查环境变量。"
        problem = {
            "key": key,
            "source": item.get("source"),
            "status": status,
            "coverage_risk": risk,
            "optional_source": optional_source,
            "optional_reason": item.get("optional_reason", ""),
            "authenticated": authenticated,
            "auth_status": item.get("auth_status"),
            "auth_problem": auth_problem,
            "auth_hint": auth_hint,
            "source_exhausted": source_exhausted,
            "errors": errors,
            "next_offset": item.get("next_offset"),
            "next_token": item.get("next_token"),
            "budget_complete": item.get("budget_complete"),
        }
        if auth_problem:
            auth_problem_examples.append(problem)
        if optional_source and status in {"failed", "rate_limited", "budget_exhausted", "source_missing"}:
            optional_examples.append(problem)
            continue
        if risk == "high" or (not explicit_risk and status in {"failed", "rate_limited"}):
            hard_examples.append(problem)
        budget_complete = item.get("budget_complete")
        if isinstance(budget_complete, str):
            budget_complete = budget_complete.strip().lower() in {"1", "true", "yes"}
        if (
            risk == "medium"
            or (status == "source_missing" and risk != "low")
            or (source_exhausted is False and budget_complete is False)
        ):
            medium_examples.append(problem)
    # Some sources are always optional from PaperCompass's perspective: their
    # value adds long-tail coverage, but a build remains complete without them.
    # Treat these as gracefully skippable on auth failure regardless of whether
    # the user nominally configured a key (a stale / rate-limited key looks
    # identical to "no key" from the user's point of view).
    always_optional_sources = {"semanticscholar"}
    auth_problem_sources = sorted({
        clean_text(p.get("source"))
        for p in auth_problem_examples
        if clean_text(p.get("source"))
    })
    auth_problem_non_optional_sources = sorted({
        clean_text(p.get("source"))
        for p in auth_problem_examples
        if (
            clean_text(p.get("source"))
            and not p.get("optional_source")
            and clean_text(p.get("source")) not in always_optional_sources
        )
    })
    return {
        "entry_count": len(coverage),
        "statuses": dict(statuses),
        "coverage_risks": dict(risks),
        "problem_count": len(hard_examples),
        "medium_problem_count": len(medium_examples),
        "optional_problem_count": len(optional_examples),
        "auth_problem_count": len(auth_problem_examples),
        "auth_problem_sources": auth_problem_sources,
        "auth_problem_non_optional_sources": auth_problem_non_optional_sources,
        "problem_examples": hard_examples[:30],
        "medium_problem_examples": medium_examples[:30],
        "optional_problem_examples": optional_examples[:30],
        "auth_problem_examples": auth_problem_examples[:30],
    }


def recall_pool_report(
    workspace: Path,
    papers: list[dict[str, Any]],
    review: dict[str, Any],
) -> dict[str, Any]:
    coverage = read_json(manifests_dir(workspace) / "source_coverage.json", {})
    if not isinstance(coverage, dict):
        coverage = {}
    source_kept: Counter[str] = Counter()
    source_seen: Counter[str] = Counter()
    for item in coverage.values():
        if not isinstance(item, dict):
            continue
        source = clean_text(item.get("source") or "unknown")
        try:
            source_kept[source] += int(item.get("kept_count") or 0)
        except (TypeError, ValueError):
            pass
        try:
            source_seen[source] += int(item.get("result_count") or item.get("processed_count") or 0)
        except (TypeError, ValueError):
            pass
    raw_candidate_count = 0
    try:
        raw_candidate_count = sum(1 for _ in iter_raw_candidates(workspace))
    except Exception:
        raw_candidate_count = 0
    queue_count = int(review.get("queue_count") or 0)
    thresholds = {
        "min_review_queue": 50,
        "min_raw_candidates": 80,
        "min_final_papers": 40,
    }
    underpowered = (
        queue_count > 0
        and queue_count < thresholds["min_review_queue"]
        and raw_candidate_count < thresholds["min_raw_candidates"]
        and len(papers) < thresholds["min_final_papers"]
    )
    reasons = []
    if underpowered:
        reasons.append(
            "review queue, raw candidate pool, and final library are all below "
            "minimum recall thresholds"
        )
    return {
        "status": "underpowered" if underpowered else "ok",
        "underpowered": underpowered,
        "reasons": reasons,
        "thresholds": thresholds,
        "review_queue_count": queue_count,
        "raw_candidate_count": raw_candidate_count,
        "final_paper_count": len(papers),
        "source_kept_counts": dict(source_kept),
        "source_seen_counts": dict(source_seen),
    }


def metadata_report(papers: list[dict[str, Any]]) -> dict[str, Any]:
    gaps = {"no_abstract": 0, "no_external_id": 0, "no_pdf_or_url": 0, "no_venue": 0, "negative_citation": 0}
    venue_counts: Counter[str] = Counter()
    for paper in papers:
        ids = paper.get("ids") if isinstance(paper.get("ids"), dict) else {}
        urls = paper.get("urls") if isinstance(paper.get("urls"), dict) else {}
        if not clean_text(paper.get("abstract")):
            gaps["no_abstract"] += 1
        if not any(ids.get(key) for key in ("doi", "arxiv", "acl", "semantic_scholar", "openreview", "openalex", "dblp", "pmid", "pmcid", "europepmc")):
            gaps["no_external_id"] += 1
        if not (urls.get("landing") or urls.get("pdf") or paper.get("url") or paper.get("pdf_url")):
            gaps["no_pdf_or_url"] += 1
        if not clean_text(paper.get("venue")):
            gaps["no_venue"] += 1
        if any(_int_value(paper.get(key)) < 0 for key in ("citation_count", "gs_citation", "max_citation", "reference_count")):
            gaps["negative_citation"] += 1
        venue_counts[clean_text(paper.get("venue") or "N/A")] += 1
    arxiv_variants = {venue: count for venue, count in venue_counts.items() if "arxiv" in venue.lower() and venue != "arXiv"}
    return {
        "gaps": gaps,
        "top_venues": venue_counts.most_common(30),
        "venue_normalization_warnings": {"arxiv_variants": arxiv_variants},
    }


def _int_value(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def role_placement_report(
    papers: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
) -> dict[str, Any]:
    def invalid_examples(items: list[dict[str, Any]], allowed: set[str]) -> list[dict[str, Any]]:
        examples = []
        for item in items:
            role = normalize_role(item.get("paper_role"), default=CORE_METHOD)
            if role in allowed:
                continue
            examples.append({
                "title": item.get("title"),
                "year": item.get("year"),
                "paper_key": item.get("paper_key"),
                "paper_role": role,
                "decision_reason": (item.get("decision") or {}).get("reason")
                if isinstance(item.get("decision"), dict)
                else "",
            })
        return examples

    main_invalid = invalid_examples(papers, MAIN_LIBRARY_ROLES)
    anchor_invalid = invalid_examples(anchors, ANCHOR_ROLES)
    return {
        "main_allowed_roles": sorted(MAIN_LIBRARY_ROLES),
        "anchor_allowed_roles": sorted(ANCHOR_ROLES),
        "main_invalid_count": len(main_invalid),
        "anchor_invalid_count": len(anchor_invalid),
        "main_invalid_examples": main_invalid[:30],
        "anchor_invalid_examples": anchor_invalid[:30],
    }


def catalog_report(workspace: Path, paper_count: int) -> dict[str, Any]:
    manifest = read_json(catalog_dir(workspace) / "manifest.json", {})
    manifest_count = manifest.get("paper_count") if isinstance(manifest, dict) else None
    orphan_dirs = sorted(
        path.name
        for path in workspace.glob(".catalog.*")
        if path.is_dir() and (path.name.startswith(".catalog.tmp.") or path.name.startswith(".catalog.prev."))
    )
    return {
        "manifest_exists": bool(manifest),
        "manifest_paper_count": manifest_count,
        "data_paper_count": paper_count,
        "count_matches": manifest_count == paper_count,
        "orphan_generation_dirs": orphan_dirs[:20],
        "orphan_generation_count": len(orphan_dirs),
    }


def _record_count_for_output(path: Path) -> int | None:
    if not path.exists():
        return None
    if path.suffix == ".jsonl":
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    if path.suffix == ".json":
        data = read_json(path, None)
        if isinstance(data, list):
            return len(data)
    return None


def build_manifest_integrity_report(workspace: Path) -> dict[str, Any]:
    manifest = read_json(manifests_dir(workspace) / "latest.json", {})
    outputs = manifest.get("outputs") if isinstance(manifest, dict) else {}
    if not isinstance(outputs, dict) or not outputs:
        return {"status": "missing", "mismatch_count": 0, "mismatches": []}
    mismatches: list[dict[str, Any]] = []
    checked = 0
    for name, item in outputs.items():
        if not isinstance(item, dict):
            continue
        rel_path = clean_text(item.get("path"))
        path = workspace / rel_path
        checked += 1
        if not path.exists():
            mismatches.append({"output": name, "path": rel_path, "reason": "missing"})
            continue
        data = path.read_bytes()
        actual = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "records": _record_count_for_output(path),
        }
        for key in ("sha256", "bytes", "records"):
            expected = item.get(key)
            if expected is not None and actual.get(key) != expected:
                mismatches.append({
                    "output": name,
                    "path": rel_path,
                    "field": key,
                    "expected": expected,
                    "actual": actual.get(key),
                })
    return {
        "status": "checked",
        "checked_count": checked,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],
    }


def coverage_manifest_report(workspace: Path, paper_count: int, *, refresh: bool = False) -> dict[str, Any]:
    path = manifests_dir(workspace) / "coverage_report.json"
    if not refresh:
        current = read_json(path, {})
        current_count = current.get("paper_count") if isinstance(current, dict) else None
        return {
            "refresh_status": "not_refreshed" if path.exists() else "missing",
            "path": workspace_relative_path(workspace, path),
            "paper_count": current_count,
            "data_paper_count": paper_count,
            "count_matches": current_count == paper_count if path.exists() else True,
        }
    try:
        from .discovery import make_coverage_report

        report = make_coverage_report(workspace)
    except Exception as exc:  # noqa: BLE001
        current = read_json(path, {})
        current_count = current.get("paper_count") if isinstance(current, dict) else None
        return {
            "refresh_status": "failed",
            "path": workspace_relative_path(workspace, path),
            "paper_count": current_count,
            "data_paper_count": paper_count,
            "count_matches": current_count == paper_count,
            "error": str(exc)[:500],
        }
    current_count = report.get("paper_count") if isinstance(report, dict) else None
    return {
        "refresh_status": "refreshed",
        "path": workspace_relative_path(workspace, path),
        "paper_count": current_count,
        "data_paper_count": paper_count,
        "count_matches": current_count == paper_count,
    }


def seed_report(workspace: Path) -> dict[str, Any]:
    seed_path = existing_anchors_plan_path(workspace)
    if not seed_path.exists():
        return {
            "status": "not_configured",
            "seed_count": 0,
            "required_count": 0,
            "source_backed_seed_count": 0,
            "verified_required_count": 0,
            "required_without_evidence_count": 0,
            "missing_count": 0,
            "missing_examples": [],
            "by_role": {},
        }
    seeds = iter_anchor_rows(workspace)
    pools: dict[str, set[str]] = {name: set() for name in ("main", "anchors", "pending", "rejected", "raw")}
    for label, path in (
        ("main", data_dir(workspace) / "papers.json"),
        ("anchors", data_dir(workspace) / "anchor_papers.json"),
        ("pending", data_dir(workspace) / "pending_review_candidates.json"),
        ("rejected", data_dir(workspace) / "rejected_candidates.json"),
    ):
        for item in read_list(path):
            pools[label].add(normalize_title(item.get("title", "")))
            pools[label].update(identity_keys(item))
    for row in iter_raw_candidates(workspace):
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else row
        if isinstance(raw, dict):
            pools["raw"].add(normalize_title(raw.get("title", "")))
            pools["raw"].update(identity_keys(raw))
    missing = []
    misplaced = []
    required_without_evidence: list[dict[str, Any]] = []
    unverified_suggestions: list[dict[str, Any]] = []
    evidence_sources: Counter[str] = Counter()
    by_role: dict[str, dict[str, Any]] = {}
    for seed in seeds:
        role = normalize_role(seed.get("paper_role") or seed.get("role"), default=CORE_METHOD)
        bucket = role_bucket(role)
        stats = by_role.setdefault(
            role,
            {
                "seed_count": 0,
                "required_count": 0,
                "missing_count": 0,
                "misplaced_count": 0,
                "missing_examples": [],
                "misplaced_examples": [],
                "location_counts": {},
            },
        )
        stats["seed_count"] += 1
        required = seed_required(seed)
        if required:
            stats["required_count"] += 1
        source_backed = seed_has_source_evidence(seed)
        if source_backed:
            evidence = seed.get("evidence") if isinstance(seed.get("evidence"), dict) else {}
            evidence_sources[clean_text(evidence.get("source") or "unknown")] += 1
        if required and not seed_verified_source_backed(seed):
            seed_with_role = {"paper_role": role, "role_bucket": bucket, **seed}
            required_without_evidence.append(seed_with_role)
            stats["required_without_evidence_count"] = stats.get("required_without_evidence_count", 0) + 1
            continue
        if not required and not source_backed and len(unverified_suggestions) < 30:
            unverified_suggestions.append({"paper_role": role, "role_bucket": bucket, **seed})
        title_key = normalize_title(seed.get("title", ""))
        keys = {title_key, *identity_keys(seed)} - {""}
        locations = [
            label
            for label, values in pools.items()
            if keys and bool(keys & values)
        ]
        for location in locations:
            stats["location_counts"][location] = stats["location_counts"].get(location, 0) + 1
        if locations or not required:
            rejected_only = (
                required
                and locations
                and set(locations) == {"rejected"}
            )
            if rejected_only:
                seed_with_role = {
                    "paper_role": role,
                    "role_bucket": bucket,
                    "locations": locations,
                    "rejected_seed": True,
                    **seed,
                }
                missing.append(seed_with_role)
                stats["missing_count"] += 1
                stats["rejected_count"] = stats.get("rejected_count", 0) + 1
                if len(stats["missing_examples"]) < 10:
                    stats["missing_examples"].append(seed_with_role)
                continue
            if required and locations:
                expected = "main" if role in {CORE_METHOD, MECHANISM_EVAL} else "anchors" if role == BACKGROUND_ANCHOR else ""
                if expected and expected not in locations:
                    seed_with_role = {"paper_role": role, "role_bucket": bucket, "locations": locations, **seed}
                    misplaced.append(seed_with_role)
                    stats["misplaced_count"] += 1
                    if len(stats["misplaced_examples"]) < 10:
                        stats["misplaced_examples"].append(seed_with_role)
            continue
        seed_with_role = {"paper_role": role, "role_bucket": bucket, **seed}
        missing.append(seed_with_role)
        stats["missing_count"] += 1
        if len(stats["missing_examples"]) < 10:
            stats["missing_examples"].append(seed_with_role)
    core_missing = sum(
        int(stats.get("missing_count") or 0)
        for role, stats in by_role.items()
        if role in {CORE_METHOD, MECHANISM_EVAL}
    )
    anchor_missing = int((by_role.get(BACKGROUND_ANCHOR) or {}).get("missing_count") or 0)
    core_misplaced = sum(
        int(stats.get("misplaced_count") or 0)
        for role, stats in by_role.items()
        if role in {CORE_METHOD, MECHANISM_EVAL}
    )
    anchor_misplaced = int((by_role.get(BACKGROUND_ANCHOR) or {}).get("misplaced_count") or 0)
    core_rejected = sum(
        int(stats.get("rejected_count") or 0)
        for role, stats in by_role.items()
        if role in {CORE_METHOD, MECHANISM_EVAL}
    )
    anchor_rejected = int((by_role.get(BACKGROUND_ANCHOR) or {}).get("rejected_count") or 0)
    rejected_count = sum(
        int(stats.get("rejected_count") or 0) for stats in by_role.values()
    )
    required_count = sum(int(stats.get("required_count") or 0) for stats in by_role.values())
    source_backed_seed_count = sum(1 for seed in seeds if seed_has_source_evidence(seed))
    verified_required_count = sum(1 for seed in seeds if seed_required(seed) and seed_verified_source_backed(seed))
    return {
        "status": "checked",
        "seed_count": len(seeds),
        "required_count": required_count,
        "source_backed_seed_count": source_backed_seed_count,
        "verified_required_count": verified_required_count,
        "required_without_evidence_count": len(required_without_evidence),
        "required_without_evidence_examples": required_without_evidence[:30],
        "unverified_seed_suggestions": unverified_suggestions[:30],
        "seed_evidence_counts_by_source": dict(evidence_sources),
        "missing_count": len(missing),
        "misplaced_count": len(misplaced),
        "rejected_count": rejected_count,
        "core_missing_count": core_missing,
        "anchor_missing_count": anchor_missing,
        "core_misplaced_count": core_misplaced,
        "anchor_misplaced_count": anchor_misplaced,
        "core_rejected_count": core_rejected,
        "anchor_rejected_count": anchor_rejected,
        "by_role": by_role,
        "missing_examples": missing[:30],
        "misplaced_examples": misplaced[:30],
    }


def query_coverage_report(workspace: Path) -> dict[str, Any]:
    """Verify that every search_hint is reachable from a source query string.

    Catches plan output where the brain proposed a hint but deterministic
    source rendering failed to bake an equivalent query into sources.yaml.
    """
    from .auto.plan import _clean_phrase, _distinctive_tokens

    topic = load_topic_config(workspace)
    sources = load_sources_config(workspace)
    terms: list[str] = []
    value = topic.get("search_hints") or []
    if isinstance(value, str):
        value = [value]
    terms.extend(clean_text(item) for item in value if clean_text(item))
    query_texts: list[str] = []
    for cfg_root in (sources.get("sources", {}), sources.get("discovery", {})):
        if isinstance(cfg_root, dict):
            for cfg in cfg_root.values():
                if isinstance(cfg, dict):
                    query_texts.extend(_collect_queries(cfg))
    query_rows = [row.lower() for row in query_texts]
    query_blob = "\n".join(query_rows)

    def covered(term: str) -> bool:
        variants = {
            clean_text(term).lower(),
            clean_text(_clean_phrase(term)).lower(),
        }
        if any(variant and variant in query_blob for variant in variants):
            return True
        tokens = _distinctive_tokens(term)
        return bool(tokens) and any(all(token in row for token in tokens) for row in query_rows)

    uncovered = [term for term in sorted(set(terms), key=str.lower) if not covered(term)]
    return {"term_count": len(set(terms)), "uncovered_count": len(uncovered), "uncovered_terms": uncovered[:50]}


def _collect_queries(cfg: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for key in ("queries", "search_queries", "arxiv_queries"):
        value = cfg.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    rows.append(clean_text(item.get("text") or item.get("query")))
                else:
                    rows.append(clean_text(item))
        elif isinstance(value, str):
            rows.append(clean_text(value))
    return [row for row in rows if row]


def build_quality_report(workspace: Path, strict: bool = False, *, refresh_coverage: bool = False) -> dict[str, Any]:
    with workspace_lock(workspace):
        return _build_quality_report_unlocked(workspace, strict=strict, refresh_coverage=refresh_coverage)


def _build_quality_report_unlocked(workspace: Path, strict: bool = False, *, refresh_coverage: bool = False) -> dict[str, Any]:
    topic = load_topic_config(workspace)
    papers = read_list(data_dir(workspace) / "papers.json")
    anchors = read_list(data_dir(workspace) / "anchor_papers.json")
    pending = read_list(data_dir(workspace) / "pending_review_candidates.json")
    rejected = read_list(data_dir(workspace) / "rejected_candidates.json")
    review = review_report(workspace)
    applied_review_decisions = applied_review_decisions_report(workspace)
    score_decisions = score_decisions_report(workspace)
    prefilter_efficiency = prefilter_efficiency_report(workspace)
    source_preflight = source_preflight_report(workspace)
    raw_pollution = raw_pollution_report(workspace)
    coverage = coverage_report(workspace)
    recall_pool = recall_pool_report(workspace, papers, review)
    metadata = metadata_report(papers)
    catalog = catalog_report(workspace, len(papers))
    build_integrity = build_manifest_integrity_report(workspace)
    coverage_manifest = coverage_manifest_report(workspace, len(papers), refresh=refresh_coverage)
    seed = seed_report(workspace)
    query_coverage = query_coverage_report(workspace)
    publication_scope = publication_scope_report(papers, topic)
    role_placement = role_placement_report(papers, anchors)
    duplicates = {
        "main": duplicate_report(papers),
        "anchors": duplicate_report(anchors),
        "pending": duplicate_report(pending),
        "rejected": duplicate_report(rejected),
    }
    critical = []
    warnings = []
    if duplicates["main"]["duplicate_key_count"]:
        critical.append("main_duplicate_entities")
    if duplicates["anchors"]["duplicate_key_count"] or duplicates["pending"]["duplicate_key_count"] or duplicates["rejected"]["duplicate_key_count"]:
        warnings.append("candidate_duplicate_entities")
    if raw_pollution["polluted_row_count"]:
        warnings.append("raw_derived_tag_pollution")
    if pending:
        warnings.append("pending_review_unresolved")
    if role_placement["main_invalid_count"]:
        critical.append("main_role_placement_violation")
    if role_placement["anchor_invalid_count"]:
        warnings.append("anchor_role_placement_violation")
    if metadata["gaps"].get("negative_citation", 0):
        warnings.append("metadata_negative_citations")
    if review.get("status") == "invalid":
        critical.append("score_papers_decisions_invalid")
    if review.get("status") == "no_decisions" and review.get("queue_count", 0):
        warnings.append("score_papers_decisions_missing")
    if review.get("deferred_count", 0):
        warnings.append("score_papers_has_deferred")
    if applied_review_decisions.get("stale_context_count", 0):
        warnings.append("applied_review_decisions_stale")
    if applied_review_decisions.get("invalid_decision_count", 0):
        warnings.append("applied_review_decisions_invalid")
    if score_decisions.get("no_brain_verdict_violation_count", 0):
        warnings.append("no_brain_boundary_scores")
    if pending and prefilter_efficiency.get("status") == "missing":
        warnings.append("prefilter_decisions_missing")
    if prefilter_efficiency.get("status") == "checked":
        candidate_count = int(prefilter_efficiency.get("candidate_count") or 0)
        action_counts = prefilter_efficiency.get("action_counts") or {}
        ratio = float(prefilter_efficiency.get("llm_review_ratio") or 0.0)
        reviewish = int(action_counts.get("review", 0) or 0) + int(action_counts.get("strong", 0) or 0)
        hard_rejects = int(action_counts.get("hard_reject", 0) or 0)
        if candidate_count >= 300 and ratio > 0.65:
            warnings.append("prefilter_too_permissive")
        if candidate_count >= 100 and reviewish < 10:
            warnings.append("prefilter_too_strict")
        if candidate_count >= 100 and hard_rejects / max(candidate_count, 1) > 0.8:
            warnings.append("prefilter_hard_reject_dominates")
    if source_preflight.get("blocked_count", 0):
        warnings.append("source_preflight_blocked")
    elif source_preflight.get("warning_count", 0):
        warnings.append("source_preflight_has_warnings")
    if recall_pool.get("underpowered"):
        warnings.append("recall_pool_underpowered")
    if coverage["problem_count"]:
        warnings.append("source_coverage_has_risk")
    elif coverage.get("medium_problem_count", 0):
        warnings.append("source_coverage_partial")
    # Treat auth failures by source identity, not by entry: a single SS source
    # paginating 24 requests with 403 used to inflate this to "critical" even
    # though it is one source-level skip. Optional sources (no api key, opt-in
    # services like Semantic Scholar without explicit enablement) never escalate
    # to critical — they degrade gracefully like a missing source.
    auth_non_optional_sources = coverage.get("auth_problem_non_optional_sources") or []
    auth_any_sources = coverage.get("auth_problem_sources") or []
    if len(auth_non_optional_sources) >= 2:
        critical.append("source_auth_failed")
    elif auth_non_optional_sources:
        warnings.append("source_auth_failed")
    elif auth_any_sources:
        warnings.append("source_auth_failed_optional")
    if not catalog["count_matches"]:
        critical.append("catalog_count_mismatch")
    if catalog.get("orphan_generation_count", 0):
        warnings.append("catalog_orphan_generation_dirs")
    if build_integrity.get("mismatch_count", 0):
        critical.append("build_manifest_integrity_mismatch")
    if coverage_manifest["refresh_status"] == "failed":
        warnings.append("coverage_report_refresh_failed")
    elif not coverage_manifest["count_matches"]:
        critical.append("coverage_report_count_mismatch")
    if seed.get("core_rejected_count", 0):
        critical.append("core_seed_rejected")
    if seed.get("anchor_rejected_count", 0):
        warnings.append("anchor_seed_rejected")
    if seed.get("required_without_evidence_count", 0):
        critical.append("seed_without_source_evidence")
    if seed.get("seed_count", 0) and not seed.get("verified_required_count", 0):
        warnings.append("no_verified_required_seeds")
    if seed.get("core_missing_count", 0):
        warnings.append("core_seed_coverage_missing")
    if seed.get("anchor_missing_count", 0):
        warnings.append("anchor_seed_coverage_missing")
    if seed.get("missing_count", 0):
        warnings.append("seed_coverage_missing")
    placement_checks_active = score_decisions.get("status") == "checked" or review.get("status") == "valid"
    if placement_checks_active and seed.get("core_misplaced_count", 0):
        warnings.append("core_seed_misplaced")
    if placement_checks_active and seed.get("anchor_misplaced_count", 0):
        warnings.append("anchor_seed_misplaced")
    if query_coverage.get("uncovered_count", 0):
        warnings.append("query_terms_uncovered")
    if publication_scope.get("violation_count", 0):
        warnings.append("publication_scope_violations")
    status = "failed" if critical or (strict and warnings) else ("warning" if warnings else "passed")
    report = {
        "built_at": now_iso(),
        "workspace": workspace_label(workspace),
        "strict": strict,
        "status": status,
        "critical": critical,
        "warnings": warnings,
        "counts": {"papers": len(papers), "anchors": len(anchors), "pending": len(pending), "rejected": len(rejected)},
        "duplicates": duplicates,
        "raw_pollution": raw_pollution,
        "score_papers": review,
        "applied_review_decisions": applied_review_decisions,
        "score_decisions": score_decisions,
        "prefilter_efficiency": prefilter_efficiency,
        "source_preflight": source_preflight,
        "recall_pool": recall_pool,
        "source_coverage": coverage,
        "coverage_manifest": coverage_manifest,
        "metadata": metadata,
        "role_placement": role_placement,
        "catalog": catalog,
        "build_manifest_integrity": build_integrity,
        "seed_coverage": seed,
        "query_coverage": query_coverage,
        "publication_scope": publication_scope,
    }
    run = stamp()
    manifest_path = manifests_dir(workspace) / f"quality_gates_{run}.json"
    report_path = state_dir(workspace) / "reports" / f"final_audit_{run}.md"
    violations = publication_scope.get("violations") if isinstance(publication_scope, dict) else []
    if isinstance(violations, list) and violations:
        violations_path = manifests_dir(workspace) / f"publication_scope_violations_{run}.jsonl"
        write_jsonl(violations_path, [item for item in violations if isinstance(item, dict)])
        publication_scope["violations_path"] = workspace_relative_path(workspace, violations_path)
    report = portable_workspace_data(workspace, report)
    write_json(manifest_path, report)
    write_quality_markdown(report_path, report)
    report["manifest"] = workspace_relative_path(workspace, manifest_path)
    report["markdown"] = workspace_relative_path(workspace, report_path)
    return report


def write_quality_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# PaperCompass Workspace QA 报告",
        "",
        f"- 时间：{report['built_at']}",
        f"- Workspace：`{report['workspace']}`",
        f"- 状态：`{report['status']}`",
        f"- Critical：{', '.join(report['critical']) or '无'}",
        f"- Warnings：{', '.join(report['warnings']) or '无'}",
        "",
        "## 数量",
        "",
        f"- 主库：{report['counts']['papers']}",
        f"- 锚点库：{report['counts'].get('anchors', 0)}",
        f"- Pending：{report['counts']['pending']}",
        f"- Rejected：{report['counts']['rejected']}",
        "",
        "## 关键检查",
        "",
        f"- 主库重复键：{report['duplicates']['main']['duplicate_key_count']}",
        f"- 锚点库重复键：{report['duplicates'].get('anchors', {}).get('duplicate_key_count', 0)}",
        f"- Pending 重复键：{report['duplicates']['pending']['duplicate_key_count']}",
        f"- Rejected 重复键：{report['duplicates']['rejected']['duplicate_key_count']}",
        f"- Raw 派生标签污染行：{report['raw_pollution']['polluted_row_count']}",
        f"- 主库 role 违规：{report.get('role_placement', {}).get('main_invalid_count', 0)}",
        f"- 锚点库 role 违规：{report.get('role_placement', {}).get('anchor_invalid_count', 0)}",
        f"- score_papers defer 数：{report['score_papers'].get('deferred_count', 0)}",
        f"- applied review stale context："
        f"{report.get('applied_review_decisions', {}).get('stale_context_count', 0)}",
        f"- no-brain boundary/in_scope 违规："
        f"{report['score_decisions'].get('no_brain_verdict_violation_count', 0)}",
        f"- 前筛 LLM 队列：{report.get('prefilter_efficiency', {}).get('sent_to_llm', 0)}/"
        f"{report.get('prefilter_efficiency', {}).get('candidate_count', 0)} "
        f"(ratio={report.get('prefilter_efficiency', {}).get('llm_review_ratio', 0.0)})",
        f"- Source preflight：warnings={report.get('source_preflight', {}).get('warning_count', 0)}, "
        f"blocked={report.get('source_preflight', {}).get('blocked_count', 0)}",
        f"- Recall 候选池状态：{report['recall_pool'].get('status', 'unknown')}",
        f"- Recall queue/.raw/final：{report['recall_pool'].get('review_queue_count', 0)}/"
        f"{report['recall_pool'].get('raw_candidate_count', 0)}/"
        f"{report['recall_pool'].get('final_paper_count', 0)}",
        f"- Source coverage 风险项：{report['source_coverage']['problem_count']}",
        f"- Source auth 问题：{report['source_coverage'].get('auth_problem_count', 0)}",
        f"- Build manifest integrity mismatch：{report.get('build_manifest_integrity', {}).get('mismatch_count', 0)}",
        f"- Coverage report 数量一致：{report.get('coverage_manifest', {}).get('count_matches')}",
        f"- Catalog 数量一致：{report['catalog']['count_matches']}",
        f"- Source-backed anchor：{report['seed_coverage'].get('source_backed_seed_count', 0)}",
        f"- Verified required anchor：{report['seed_coverage'].get('verified_required_count', 0)}",
        f"- Required anchor 缺 evidence：{report['seed_coverage'].get('required_without_evidence_count', 0)}",
        f"- Anchor 缺失：{report['seed_coverage'].get('missing_count', 0)}",
        f"- Anchor 放错位置：{report['seed_coverage'].get('misplaced_count', 0)}",
        f"- Query 未覆盖术语：{report['query_coverage'].get('uncovered_count', 0)}",
        f"- Publication scope 违规：{report.get('publication_scope', {}).get('violation_count', 0)}",
        f"- Publication scope 违规清单：{report.get('publication_scope', {}).get('violations_path', '无')}",
        "",
        "## Metadata",
        "",
        f"- 无摘要：{report['metadata']['gaps']['no_abstract']}",
        f"- 无 venue：{report['metadata']['gaps'].get('no_venue', 0)}",
        f"- 无外部 ID：{report['metadata']['gaps']['no_external_id']}",
        f"- 无 URL/PDF：{report['metadata']['gaps']['no_pdf_or_url']}",
        f"- 负 citation/reference：{report['metadata']['gaps'].get('negative_citation', 0)}",
        "",
        "详细结构化结果见同名 `quality_gates_*.json`。",
        "",
    ]
    atomic_write_text(path, "\n".join(lines))


def refresh_final_summary_from_qa(workspace: Path, report: dict[str, Any]) -> dict[str, Any]:
    summary_path = state_dir(workspace) / "auto" / "final_summary.json"
    summary = read_json(summary_path, None)
    if not isinstance(summary, dict):
        return {
            "refreshed": False,
            "reason": "missing_or_invalid_summary",
            "path": workspace_relative_path(workspace, summary_path),
        }

    old_counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    counts = report.get("counts") if isinstance(report.get("counts"), dict) else {}
    qa_status = clean_text(report.get("status")) or "unknown"
    critical = list(report.get("critical") or [])
    qa_warnings = list(report.get("warnings") or [])
    environment = summary.get("environment") if isinstance(summary.get("environment"), dict) else {}
    environment_warnings = list(environment.get("warnings") or [])

    quality = summary.get("quality") if isinstance(summary.get("quality"), dict) else {}
    quality.update({
        "qa_status": qa_status,
        "critical": critical,
        "warnings": qa_warnings + environment_warnings,
        "qa_warnings": qa_warnings,
        "environment_warnings": environment_warnings,
        "hard_reasons": critical,
    })

    previous_deliverable = clean_text(summary.get("deliverable_status") or summary.get("status"))
    if qa_status == "passed":
        deliverable_status = previous_deliverable or "passed_authoritative"
    elif critical:
        deliverable_status = "failed_quality_gate"
    else:
        deliverable_status = "usable_with_caveats"
    safe_for_default = qa_status == "passed" and deliverable_status == "passed_authoritative" and not environment_warnings

    artifacts = summary.get("artifacts") if isinstance(summary.get("artifacts"), dict) else {}
    artifacts.update({
        "state": workspace_relative_path(workspace, state_dir(workspace) / "auto" / "state.json"),
        "iterations_log": workspace_relative_path(workspace, state_dir(workspace) / "auto" / "iterations.jsonl"),
        "qa_manifest": report.get("manifest", ""),
        "qa_markdown": report.get("markdown", ""),
        "main_library": workspace_relative_path(workspace, data_dir(workspace) / "papers.jsonl"),
        "anchor_library": workspace_relative_path(workspace, data_dir(workspace) / "anchor_papers.jsonl"),
        "pending": workspace_relative_path(workspace, data_dir(workspace) / "pending_review_candidates.json"),
        "rejected": workspace_relative_path(workspace, data_dir(workspace) / "rejected_candidates.json"),
        "catalog_manifest": workspace_relative_path(workspace, catalog_dir(workspace) / "manifest.json"),
    })

    summary.update({
        "workspace": workspace_label(workspace),
        "counts": counts,
        "qa_status": qa_status,
        "quality": quality,
        "status": deliverable_status,
        "deliverable_status": deliverable_status,
        "safe_for_default_llm_retrieval": safe_for_default,
        "artifacts": artifacts,
    })
    summary = portable_workspace_data(workspace, summary)
    write_json(summary_path, summary)
    return {
        "refreshed": True,
        "path": workspace_relative_path(workspace, summary_path),
        "old_counts": old_counts,
        "new_counts": counts,
        "qa_status": qa_status,
        "deliverable_status": deliverable_status,
        "safe_for_default_llm_retrieval": safe_for_default,
    }

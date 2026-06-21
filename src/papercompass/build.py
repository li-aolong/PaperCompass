from __future__ import annotations

import json
import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import (
    data_dir,
    ensure_workspace_dirs,
    logs_dir,
    manifests_dir,
    overrides_dir,
    portable_workspace_data,
    raw_dir,
    state_dir,
    workspace_label,
    workspace_relative_path,
)
from .config import load_topic_config
from .candidate_review import workspace_decision_context_hash
from .normalize import deduplicate_papers, identity_keys, merge_paper, normalize_raw_candidate, rank_score
from .roles import ANCHOR_ROLES, CORE_METHOD, MAIN_LIBRARY_ROLES, NEGATIVE_ROLES, normalize_role
from .scope import paper_matches_publication_scope, publication_scope_from_topic
from .text import (
    append_jsonl_locked,
    append_text_locked,
    as_list,
    atomic_write_text,
    iter_jsonl,
    normalize_title,
    read_json,
    slugify,
    workspace_lock,
    write_json,
    write_jsonl,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def output_file_manifest(workspace: Path, path: Path, records: int | None = None) -> dict[str, Any]:
    data = path.read_bytes() if path.exists() else b""
    return {
        "path": workspace_relative_path(workspace, path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "records": records,
    }


def read_items(path: Path) -> list[Any]:
    if path.suffix == ".jsonl":
        return list(iter_jsonl(path))
    data = read_json(path, None)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    if isinstance(data, dict) and isinstance(data.get("papers"), list):
        return data["papers"]
    if isinstance(data, dict) and isinstance(data.get("filtered_papers"), list):
        return data["filtered_papers"]
    raise ValueError(f"无法识别输入文件结构：{path}")


def import_records(
    workspace: Path,
    input_path: Path,
    source: str,
    source_type: str,
    query: str = "",
) -> dict[str, Any]:
    ensure_workspace_dirs(workspace)
    items = read_items(input_path)
    run_id = stamp()
    out_dir = {
        "manual": "manual",
        "agent_search": "agent_search",
        "saved_search": "agent_search",
        "imported_paper": "imported",
    }.get(source_type, "imported")
    out_path = raw_dir(workspace) / out_dir / f"{run_id}_{source}.jsonl"
    fetched_at = now_iso()

    def wrap() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            rows.append({
                "source_name": source,
                "source_type": source_type,
                "query": query,
                "fetched_at": fetched_at,
                "raw": item,
            })
        return rows

    rows = wrap()
    count = write_jsonl(out_path, rows)
    log = write_run_log(workspace, "import", {
        "run_id": run_id,
        "input": str(input_path),
        "output": str(out_path),
        "source": source,
        "source_type": source_type,
        "count": count,
    })
    return {
        "run_id": run_id,
        "output": workspace_relative_path(workspace, out_path),
        "count": count,
        "log": workspace_relative_path(workspace, log),
    }


def record_agent_search(workspace: Path, source: str, query: str = "", note: str = "") -> dict[str, Any]:
    ensure_workspace_dirs(workspace)
    run_id = stamp()
    safe_source = slugify(source or "agent_search")
    out_path = raw_dir(workspace) / "agent_search" / f"{run_id}_{safe_source}.jsonl"
    count = write_jsonl(out_path, [])
    log = write_run_log(workspace, "agent_search", {
        "run_id": run_id,
        "output": str(out_path),
        "source": source,
        "source_type": "agent_search",
        "query": query,
        "count": count,
        "note": note,
    })
    return {
        "run_id": run_id,
        "output": workspace_relative_path(workspace, out_path),
        "count": count,
        "log": workspace_relative_path(workspace, log),
        "note": note,
    }


def record_agent_run_step(
    workspace: Path,
    phase: str,
    status: str = "note",
    summary: str = "",
    command: str = "",
    files: list[str] | None = None,
    run_id: str = "",
    new_run: bool = False,
) -> dict[str, Any]:
    ensure_workspace_dirs(workspace)
    log_dir = logs_dir(workspace)
    log_dir.mkdir(parents=True, exist_ok=True)
    current_path = log_dir / "current_agent_run_id"
    if run_id:
        active_run_id = slugify(run_id, 80)
    elif new_run or not current_path.exists() or not current_path.read_text(encoding="utf-8").strip():
        active_run_id = f"agent_{stamp()}"
    else:
        active_run_id = current_path.read_text(encoding="utf-8").strip()
    atomic_write_text(current_path, active_run_id)

    entry = {
        "run_id": active_run_id,
        "created_at": now_iso(),
        "phase": phase,
        "status": status,
        "summary": summary,
        "command": command,
        "files": files or [],
    }
    steps_path = log_dir / "agent_build_steps.jsonl"
    append_jsonl_locked(steps_path, [entry])

    md_path = log_dir / "AGENT_BUILD_LOG.md"
    if not md_path.exists():
        atomic_write_text(md_path, "# Agent 建库日志\n\n")
    md_lines = [
        f"## {entry['created_at']} {phase}",
        "",
        f"- run_id：`{active_run_id}`",
        f"- status：`{status}`",
    ]
    if summary:
        md_lines.append(f"- summary：{summary}")
    if command:
        md_lines.append(f"- command：`{command}`")
    if files:
        md_lines.append(f"- files：{', '.join(f'`{item}`' for item in files)}")
    append_text_locked(md_path, "\n".join(md_lines) + "\n\n")

    worklog = log_dir / "WORKLOG.md"
    append_text_locked(worklog, f"- {entry['created_at']} agent_run: {json.dumps(entry, ensure_ascii=False, sort_keys=True)}\n")
    return {
        "run_id": active_run_id,
        "step_log": workspace_relative_path(workspace, steps_path),
        "markdown": workspace_relative_path(workspace, md_path),
        "entry": entry,
    }


def add_manual_paper(workspace: Path, raw: dict[str, Any], source: str = "manual") -> dict[str, Any]:
    ensure_workspace_dirs(workspace)
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ValueError("manual paper 必须包含 title")
    run_id = stamp()
    out_path = raw_dir(workspace) / "manual" / f"{run_id}_{source}.jsonl"
    row = {
        "source_name": source,
        "source_type": "manual",
        "query": "manual_add",
        "fetched_at": now_iso(),
        "raw": {k: v for k, v in raw.items() if v not in (None, "", [], {})},
    }
    count = write_jsonl(out_path, [row])
    log = write_run_log(workspace, "manual_add", {
        "run_id": run_id,
        "output": str(out_path),
        "source": source,
        "count": count,
        "title": title,
    })
    return {
        "run_id": run_id,
        "output": workspace_relative_path(workspace, out_path),
        "count": count,
        "log": workspace_relative_path(workspace, log),
        "paper": row["raw"],
    }


def iter_raw_candidates(workspace: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    raw_root = raw_dir(workspace)
    if not raw_root.exists():
        return candidates
    for path in sorted(raw_root.glob("**/*.jsonl")):
        for item in iter_jsonl(path):
            if isinstance(item, dict):
                item.setdefault("raw_path", str(path.relative_to(workspace)))
                candidates.append(item)
    return candidates


def review_decisions_path(workspace: Path) -> Path:
    return state_dir(workspace) / "reviews" / "applied_decisions.jsonl"


def candidate_match_keys(record: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for direct in ("candidate_key", "paper_key", "library_id"):
        value = str(record.get(direct) or "").strip()
        if value:
            keys.append(f"{direct}:{value.lower()}")
            if direct == "candidate_key":
                keys.append(value.lower())
    ids = record.get("ids") if isinstance(record.get("ids"), dict) else {}
    for id_name in ("doi", "arxiv", "acl", "openreview", "semantic_scholar", "openalex", "dblp", "pmid", "pmcid", "europepmc"):
        value = str(ids.get(id_name) or record.get(f"{id_name}_id") or record.get(id_name) or "").strip()
        if value:
            keys.append(f"{id_name}:{value.lower()}")
    title = normalize_title(record.get("title", ""))
    year = record.get("year")
    if title:
        keys.append(f"title:{title}:{year or ''}")
        keys.append(f"title:{title}")
    return sorted(set(keys))


def load_applied_review_decisions(workspace: Path) -> dict[str, dict[str, Any]]:
    path = review_decisions_path(workspace)
    if not path.exists():
        return {}
    current_context_hash = workspace_decision_context_hash(workspace)
    index: dict[str, dict[str, Any]] = {}
    for item in iter_jsonl(path):
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision") or "").strip().lower()
        if decision not in {"accept", "reject", "defer", "anchor"}:
            continue
        decision_context_hash = str(item.get("decision_context_hash") or "").strip()
        if decision_context_hash != current_context_hash:
            continue
        for key in candidate_match_keys(item):
            index[key] = item
    return index


def apply_review_decision(paper: dict[str, Any], decision_index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    matched = next((decision_index[key] for key in candidate_match_keys(paper) if key in decision_index), None)
    if not matched:
        return paper
    decision = str(matched.get("decision") or "").strip().lower()
    reason = clean_review_reason(matched)
    paper = dict(paper)
    paper["review_decision"] = {
        "decision": decision,
        "reason": reason,
        "action": str(matched.get("action") or "").strip(),
        "review_run": str(matched.get("review_run") or "").strip(),
        "created_at": str(matched.get("created_at") or "").strip(),
    }
    paper_role = normalize_role(matched.get("paper_role") or paper.get("paper_role"), default=CORE_METHOD)
    paper["paper_role"] = paper_role
    paper["review_decision"]["paper_role"] = paper_role
    for key in ("queue_hash", "decision_context_hash", "scoring_policy"):
        value = str(matched.get(key) or "").strip()
        if value:
            paper["review_decision"][key] = value
    confidence = str(matched.get("confidence") or "").strip().lower()
    if confidence:
        paper["review_decision"]["confidence"] = confidence
    for key in ("inclusion_evidence", "exclusion_evidence", "missing_information"):
        values = matched.get(key)
        if isinstance(values, list) and values:
            paper["review_decision"][key] = values[:3]
    tags = set(paper.get("system_tags") or [])
    if decision == "accept":
        paper["decision"] = {"included": True, "reason": "review_accept", "confidence": "reviewed", "year": paper.get("year")}
        tags.add("review:accepted")
    elif decision == "anchor":
        paper["decision"] = {"included": True, "reason": "review_anchor", "confidence": "reviewed_anchor", "year": paper.get("year")}
        tags.add("review:anchor")
    elif decision == "reject":
        paper["decision"] = {"included": False, "reason": "review_reject", "confidence": "rejected", "needs_review": False, "year": paper.get("year")}
        tags.add("review:rejected")
    elif decision == "defer":
        paper["decision"] = {"included": False, "reason": "review_defer", "confidence": "weak", "needs_review": True, "year": paper.get("year")}
        tags.add("review:deferred")
    paper["system_tags"] = sorted(tags)
    return paper


def topic_relevance_evidence(paper: dict[str, Any]) -> dict[str, Any]:
    """Compact provenance for why this record belongs to the topic library."""
    decision = paper.get("decision") if isinstance(paper.get("decision"), dict) else {}
    review_decision = (
        paper.get("review_decision")
        if isinstance(paper.get("review_decision"), dict)
        else {}
    )
    signals = paper.get("topic_signals") if isinstance(paper.get("topic_signals"), dict) else {}
    source_records = paper.get("source_records") if isinstance(paper.get("source_records"), list) else []
    compact_sources: list[dict[str, Any]] = []
    for record in source_records[:8]:
        if not isinstance(record, dict):
            continue
        compact = {
            key: record.get(key)
            for key in (
                "source_name",
                "source_type",
                "query",
                "discovery_confidence",
                "discovery_reason",
                "raw_path",
                "source_item_id",
                "source_url",
            )
            if record.get(key) not in (None, "", [], {})
        }
        if compact:
            compact_sources.append(compact)
    evidence = {
        "decision_reason": decision.get("reason"),
        "decision_confidence": decision.get("confidence"),
        "review_decision": review_decision.get("decision"),
        "review_reason": review_decision.get("reason"),
        "review_run": review_decision.get("review_run"),
        "publication_scope_gate": paper.get("publication_scope_gate"),
        "paper_role": paper.get("paper_role") or review_decision.get("paper_role"),
        "queue_hash": review_decision.get("queue_hash"),
        "decision_context_hash": review_decision.get("decision_context_hash"),
        "scoring_policy": review_decision.get("scoring_policy"),
        "topic_signal_hits": signals.get("topic_signal_hits") or [],
        "keyword_hits": paper.get("keyword_hits") or [],
        "source_count": len(set(as_list(paper.get("sources")))),
        "source_records": compact_sources,
    }
    return {key: value for key, value in evidence.items() if value not in (None, "", [], {})}


def attach_topic_relevance_evidence(paper: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(paper)
    evidence = topic_relevance_evidence(enriched)
    if evidence:
        enriched["topic_relevance_evidence"] = evidence
    return enriched


def clean_review_reason(decision: dict[str, Any]) -> str:
    return str(decision.get("reason") or decision.get("note") or "").strip()


def keys_index(papers: list[dict[str, Any]]) -> set[str]:
    index: set[str] = set()
    for paper in papers:
        index.update(identity_keys(paper))
        index.update(candidate_match_keys(paper))
    return index


def without_known_papers(papers: list[dict[str, Any]], known_keys: set[str]) -> list[dict[str, Any]]:
    return [paper for paper in papers if not known_keys.intersection(set(identity_keys(paper)) | set(candidate_match_keys(paper)))]


def publication_scope_override_enabled(paper: dict[str, Any]) -> bool:
    tags = {item.lower() for item in (as_list(paper.get("system_tags")) + as_list(paper.get("tags")))}
    return (
        bool(paper.get("publication_scope_override") or paper.get("scope_override"))
        or bool(tags & {"publication_scope_override", "scope_override"})
    )


def reject_by_publication_scope(paper: dict[str, Any], reason: str, scope: dict[str, Any]) -> dict[str, Any]:
    rejected = dict(paper)
    decision = dict(rejected.get("decision") if isinstance(rejected.get("decision"), dict) else {})
    decision.update({
        "included": False,
        "reason": "publication_scope_violation",
        "confidence": "out_of_scope",
        "needs_review": False,
        "scope_reason": reason,
        "year": rejected.get("year"),
    })
    rejected["decision"] = decision
    rejected["publication_scope_gate"] = {
        "status": "rejected",
        "reason": reason,
        "policy": scope.get("policy"),
        "venue_profile": scope.get("venue_profile"),
        "preferred_venues": scope.get("preferred_venues", []),
        "include_preprints": bool(scope.get("include_preprints")),
    }
    tags = set(as_list(rejected.get("system_tags")))
    tags.add("publication_scope_violation")
    rejected["system_tags"] = sorted(tags)
    return rejected


def reject_by_negative_role(paper: dict[str, Any], role: str) -> dict[str, Any]:
    rejected = dict(paper)
    decision = dict(rejected.get("decision") if isinstance(rejected.get("decision"), dict) else {})
    decision.update({
        "included": False,
        "reason": "negative_role_excluded",
        "confidence": "out_of_scope",
        "needs_review": False,
        "year": rejected.get("year"),
    })
    rejected["decision"] = decision
    rejected["paper_role"] = role
    tags = set(as_list(rejected.get("system_tags")))
    tags.add("negative_role_excluded")
    rejected["system_tags"] = sorted(tags)
    return rejected


def apply_publication_scope_gate(
    papers: list[dict[str, Any]],
    topic: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    scope = publication_scope_from_topic(topic)
    if not scope or not scope.get("strict", True):
        return papers, [], {"active": bool(scope), "status": "not_strict", "rejected_count": 0}

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    override_count = 0
    for paper in papers:
        matched, reason = paper_matches_publication_scope(paper, scope)
        if matched:
            accepted.append(paper)
            continue
        if publication_scope_override_enabled(paper):
            allowed = dict(paper)
            allowed["publication_scope_gate"] = {
                "status": "override_allowed",
                "reason": reason,
                "policy": scope.get("policy"),
                "venue_profile": scope.get("venue_profile"),
            }
            accepted.append(allowed)
            override_count += 1
            continue
        rejected.append(reject_by_publication_scope(paper, reason, scope))

    return accepted, rejected, {
        "active": True,
        "status": "rejected" if rejected else "passed",
        "scope": scope,
        "checked_count": len(papers),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "override_count": override_count,
        "rejected_examples": [
            {
                "title": item.get("title"),
                "year": item.get("year"),
                "venue": item.get("venue"),
                "reason": (item.get("publication_scope_gate") or {}).get("reason"),
            }
            for item in rejected[:30]
        ],
    }


def build_workspace(workspace: Path) -> dict[str, Any]:
    with workspace_lock(workspace):
        return _build_workspace_unlocked(workspace)


def _build_workspace_unlocked(workspace: Path) -> dict[str, Any]:
    ensure_workspace_dirs(workspace)
    topic = load_topic_config(workspace)
    run_id = stamp()
    raw_candidates = iter_raw_candidates(workspace)
    normalized: list[dict[str, Any]] = []
    anchor_normalized: list[dict[str, Any]] = []
    pending_review: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    review_decisions = load_applied_review_decisions(workspace)

    for candidate in raw_candidates:
        paper = normalize_raw_candidate(candidate, topic)
        if not paper:
            continue
        paper = apply_review_decision(paper, review_decisions)
        paper = attach_topic_relevance_evidence(paper)
        if paper.get("decision", {}).get("included"):
            role = normalize_role(paper.get("paper_role"), default=CORE_METHOD)
            if role in ANCHOR_ROLES:
                anchor_normalized.append(paper)
            elif role in MAIN_LIBRARY_ROLES:
                normalized.append(paper)
            elif role in NEGATIVE_ROLES:
                rejected.append(reject_by_negative_role(paper, role))
        elif is_pending_review(paper):
            pending_review.append(paper)
        else:
            rejected.append(paper)

    candidate_main = apply_overrides(workspace, deduplicate_papers(normalized))
    gated_main, publication_scope_rejected, publication_scope_gate = apply_publication_scope_gate(candidate_main, topic)
    papers = [
        attach_topic_relevance_evidence(paper)
        for paper in gated_main
    ]
    anchor_papers = [
        attach_topic_relevance_evidence(paper)
        for paper in deduplicate_papers(anchor_normalized)
    ]
    included_keys = keys_index(papers) | keys_index(anchor_papers)
    pending_papers = [
        attach_topic_relevance_evidence(paper)
        for paper in deduplicate_papers(without_known_papers(pending_review, included_keys))
    ]
    pending_keys = included_keys | keys_index(pending_papers)
    rejected_candidates = rejected + publication_scope_rejected
    rejected_papers = [
        attach_topic_relevance_evidence(paper)
        for paper in deduplicate_papers(without_known_papers(rejected_candidates, pending_keys))
    ]
    pending_records = [excluded_candidate_record(paper, queue="pending_review") for paper in pending_papers]
    rejected_records = [excluded_candidate_record(paper, queue="rejected") for paper in rejected_papers]
    topic_papers = [
        {
            "topic_id": topic.get("topic_id", workspace.name),
            "paper_key": paper["paper_key"],
            "title": paper["title"],
            "year": paper.get("year"),
            "keyword_hits": paper.get("keyword_hits", []),
            "tags": paper.get("tags", []),
            "decision": paper.get("decision", {}),
            "rank_score": round(rank_score(paper), 2),
        }
        for paper in papers
    ]

    out_data_dir = data_dir(workspace)
    write_json(out_data_dir / "papers.json", papers)
    write_jsonl(out_data_dir / "papers.jsonl", papers)
    write_json(out_data_dir / "anchor_papers.json", anchor_papers)
    write_jsonl(out_data_dir / "anchor_papers.jsonl", anchor_papers)
    write_jsonl(out_data_dir / "topic_papers.jsonl", topic_papers)
    write_json(out_data_dir / "pending_review_candidates.json", pending_records)
    write_json(out_data_dir / "rejected_candidates.json", rejected_records)

    manifest = {
        "run_id": run_id,
        "built_at": now_iso(),
        "workspace": workspace_label(workspace),
        "topic_id": topic.get("topic_id", workspace.name),
        "raw_candidate_count": len(raw_candidates),
        "included_candidate_count": len(normalized),
        "anchor_candidate_count": len(anchor_normalized),
        "pending_review_candidate_count": len(pending_records),
        "rejected_candidate_count": len(rejected_records),
        "included_entity_count": len(papers),
        "anchor_entity_count": len(anchor_papers),
        "pending_review_raw_candidate_count": len(pending_review),
        "rejected_raw_candidate_count": len(rejected),
        "publication_scope_rejected_count": len(publication_scope_rejected),
        "pending_review_entity_count": len(pending_records),
        "rejected_entity_count": len(rejected_records),
        "applied_review_decision_count": len({id(item) for item in review_decisions.values()}),
        "paper_count": len(papers),
        "anchor_paper_count": len(anchor_papers),
        "publication_scope_gate": publication_scope_gate,
        "outputs": {
            "papers_json": output_file_manifest(workspace, out_data_dir / "papers.json", len(papers)),
            "papers_jsonl": output_file_manifest(workspace, out_data_dir / "papers.jsonl", len(papers)),
            "anchor_papers_json": output_file_manifest(workspace, out_data_dir / "anchor_papers.json", len(anchor_papers)),
            "anchor_papers_jsonl": output_file_manifest(workspace, out_data_dir / "anchor_papers.jsonl", len(anchor_papers)),
            "topic_papers_jsonl": output_file_manifest(workspace, out_data_dir / "topic_papers.jsonl", len(topic_papers)),
            "pending_review_candidates": output_file_manifest(workspace, out_data_dir / "pending_review_candidates.json", len(pending_records)),
            "rejected_candidates": output_file_manifest(workspace, out_data_dir / "rejected_candidates.json", len(rejected_records)),
        },
    }
    manifest = portable_workspace_data(workspace, manifest)
    write_json(manifests_dir(workspace) / f"build_{run_id}.json", manifest)
    write_json(manifests_dir(workspace) / "latest.json", manifest)
    log = write_run_log(workspace, "build", manifest)
    manifest["log"] = workspace_relative_path(workspace, log)
    return manifest


def is_pending_review(paper: dict[str, Any]) -> bool:
    decision = paper.get("decision") if isinstance(paper.get("decision"), dict) else {}
    return (
        decision.get("confidence") == "weak"
        or bool(decision.get("needs_review"))
        or "review:weak_topic_signal" in (paper.get("tags") or [])
    )


def excluded_candidate_record(paper: dict[str, Any], queue: str) -> dict[str, Any]:
    decision = paper.get("decision", {})
    record = {
        "queue": queue,
        "paper_key": paper.get("paper_key", ""),
        "library_id": paper.get("library_id", ""),
        "candidate_key": candidate_match_keys(paper)[0] if candidate_match_keys(paper) else "",
        "title": paper.get("title", ""),
        "authors": paper.get("authors", ""),
        "year": paper.get("year"),
        "venue": paper.get("venue", ""),
        "abstract": paper.get("abstract", ""),
        "ids": paper.get("ids", {}),
        "urls": paper.get("urls", {}),
        "sources": paper.get("sources", []),
        "paper_role": paper.get("paper_role"),
        "keyword_hits": paper.get("keyword_hits", []),
        "tags": paper.get("tags", []),
        "system_tags": paper.get("system_tags", []),
        "decision": decision,
        "reason": decision.get("reason"),
        "confidence": decision.get("confidence"),
        "needs_review": bool(decision.get("needs_review")),
        "source_records": paper.get("source_records", []),
        "publication_scope_gate": paper.get("publication_scope_gate", {}),
        "topic_relevance_evidence": paper.get("topic_relevance_evidence", {}),
    }
    for key in ("citation_count", "gs_citation", "max_citation", "reference_count"):
        if paper.get(key) not in (None, "", [], {}):
            record[key] = paper.get(key)
    return record


def apply_overrides(workspace: Path, papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    override_dir = overrides_dir(workspace)
    if not override_dir.exists():
        return papers
    override_files = sorted(override_dir.glob("*.jsonl")) + sorted(override_dir.glob("*.json"))
    if not override_files:
        return papers

    index: dict[str, int] = {}
    for i, paper in enumerate(papers):
        for key in identity_keys(paper):
            index[key] = i
        if paper.get("paper_key"):
            index[f"paper_key:{paper['paper_key']}"] = i

    for path in override_files:
        items = list(iter_jsonl(path)) if path.suffix == ".jsonl" else read_items(path)
        for raw in items:
            if not isinstance(raw, dict):
                continue
            match_keys = []
            for field, prefix in [
                ("paper_key", "paper_key"),
                ("doi", "doi"),
                ("arxiv_id", "arxiv"),
                ("semantic_scholar_id", "semantic_scholar"),
                ("acl_id", "acl"),
                ("library_id", "source_library_id"),
            ]:
                if raw.get(field):
                    match_keys.append(f"{prefix}:{str(raw[field]).lower()}")
            if raw.get("title"):
                title = str(raw["title"])
                year = raw.get("year", "")

                match_keys.append(f"title:{normalize_title(title)}:{year}")
                match_keys.append(f"title:{normalize_title(title)}")
            target = next((index[key] for key in match_keys if key in index), None)
            if target is None:
                continue
            patch = {k: v for k, v in raw.items() if k not in {"match", "reason"} and v not in (None, "", [], {})}
            if patch.get("notes") and not patch.get("system_tags"):
                patch["system_tags"] = patch.pop("notes")
            patch.setdefault("system_tags", [])
            patch["system_tags"] = sorted(set((patch.get("system_tags") or []) + ["manual_override"]))
            papers[target] = merge_paper(papers[target], patch)
    return papers


def reset_generated_outputs(workspace: Path) -> None:
    for rel in (
        "data/papers.json",
        "data/papers.jsonl",
        "data/topic_papers.jsonl",
        "data/pending_review_candidates.json",
        "data/rejected_candidates.json",
    ):
        path = workspace / rel
        if path.exists():
            path.unlink()
    tmp = workspace / ".catalog.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)


def write_run_log(workspace: Path, kind: str, payload: dict[str, Any]) -> Path:
    payload = portable_workspace_data(workspace, payload)
    log_dir = logs_dir(workspace)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{kind}_{stamp()}.md"
    lines = [
        f"# PaperCompass {kind} 日志",
        "",
        f"- 时间：{now_iso()}",
    ]
    for key, value in payload.items():
        lines.append(f"- {key}：`{value}`")
    atomic_write_text(path, "\n".join(lines) + "\n")
    worklog = log_dir / "WORKLOG.md"
    append_text_locked(worklog, f"- {now_iso()} {kind}: {json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n")
    return path

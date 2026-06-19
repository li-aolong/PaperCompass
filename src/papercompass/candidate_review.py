from __future__ import annotations

import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import (
    data_dir,
    portable_workspace_data,
    state_dir,
    workspace_label,
)
from .normalize import identity_keys, rank_score
from .roles import CORE_METHOD, normalize_role
from .text import as_list, clean_text, iter_jsonl, normalize_title, read_json, write_json


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def reviews_dir(workspace: Path) -> Path:
    return state_dir(workspace) / "reviews"


def applied_decisions_path(workspace: Path) -> Path:
    return reviews_dir(workspace) / "applied_decisions.jsonl"


def stable_hash(data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def queue_fingerprint(queue_path: Path) -> str:
    """Hash the semantic contents of a review queue.

    The queue filename/run_id changes on every generation, so those are
    intentionally excluded. This hash is used as provenance on applied
    decisions; validation still checks exact queue coverage before apply.
    """
    _, items = load_review_payload(queue_path)
    rows = []
    for item in items:
        rows.append({
            "candidate_key": clean_text(item.get("candidate_key")) or candidate_key(item),
            "title": clean_text(item.get("title")),
            "year": item.get("year"),
            "ids": item.get("ids") if isinstance(item.get("ids"), dict) else {},
            "sources": as_list(item.get("sources")),
        })
    rows.sort(key=lambda row: (row["candidate_key"], row["title"], str(row["year"] or "")))
    return stable_hash(rows)


def workspace_decision_context_hash(workspace: Path) -> str:
    """Hash config that invalidates semantic review decisions.

    Decisions are append-only for auditability, but build should not blindly
    reuse decisions after the topic or source configuration changes.
    """
    parts: dict[str, Any] = {"schema": "papercompass.review_context.v1"}
    for name in ("topic.yaml", "sources.yaml"):
        path = workspace / name
        parts[name] = path.read_text(encoding="utf-8") if path.exists() else ""
    return stable_hash(parts)


def candidate_key(item: dict[str, Any]) -> str:
    for key in identity_keys(item):
        if key:
            return key
    title = normalize_title(item.get("title", ""))
    if title:
        return f"title:{title}:{item.get('year') or ''}"
    return clean_text(item.get("paper_key") or item.get("library_id") or "")


def load_review_payload(path: Path) -> tuple[str, list[dict[str, Any]]]:
    data = read_json(path, None)
    if isinstance(data, dict):
        items = data.get("review_candidates") or data.get("candidates") or data.get("items") or []
        return clean_text(data.get("run_id")), [item for item in items if isinstance(item, dict)]
    if isinstance(data, list):
        return "", [item for item in data if isinstance(item, dict)]
    raise ValueError(f"无法识别复核队列文件：{path}")


def is_weak_candidate(item: dict[str, Any]) -> bool:
    decision = item.get("decision") if isinstance(item.get("decision"), dict) else {}
    tags = set(as_list(item.get("tags")))
    reason = clean_text(item.get("reason") or decision.get("reason"))
    confidence = clean_text(item.get("confidence") or decision.get("confidence"))
    return (
        confidence == "weak"
        or bool(item.get("needs_review") or decision.get("needs_review"))
        or "review:weak_topic_signal" in tags
        or reason in {"weak_topic_signal", "strong_signal_not_title_focused"}
    )


def sort_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(item: dict[str, Any]) -> tuple[int, int, str]:
        year = item.get("year")
        try:
            year_int = int(year or 0)
        except (TypeError, ValueError):
            year_int = 0
        citation = item.get("max_citation") or item.get("citation_count") or item.get("gs_citation") or 0
        try:
            citation_int = int(float(citation or 0))
        except (TypeError, ValueError):
            citation_int = 0
        return (-year_int, -citation_int, clean_text(item.get("title")).lower())

    return sorted(items, key=key)


def compact_candidate(item: dict[str, Any], location: str) -> dict[str, Any]:
    decision = item.get("decision") if isinstance(item.get("decision"), dict) else {}
    source_records = item.get("source_records") if isinstance(item.get("source_records"), list) else []
    return {
        "location": location,
        "candidate_key": clean_text(item.get("candidate_key")) or candidate_key(item),
        "paper_key": clean_text(item.get("paper_key")),
        "title": clean_text(item.get("title")),
        "authors": clean_text(item.get("authors")),
        "year": item.get("year"),
        "venue": clean_text(item.get("venue")),
        "abstract": clean_text(item.get("abstract")),
        "decision": decision,
        "reason": clean_text(item.get("reason") or decision.get("reason")),
        "confidence": clean_text(item.get("confidence") or decision.get("confidence")),
        "needs_review": bool(item.get("needs_review") or decision.get("needs_review")),
        "keyword_hits": as_list(item.get("keyword_hits")),
        "tags": as_list(item.get("tags")),
        "sources": as_list(item.get("sources")),
        "ids": item.get("ids") if isinstance(item.get("ids"), dict) else {},
        "urls": item.get("urls") if isinstance(item.get("urls"), dict) else {},
        "source_records": source_records,
        "topic_relevance_evidence": item.get("topic_relevance_evidence")
        if isinstance(item.get("topic_relevance_evidence"), dict)
        else {},
        "max_citation": item.get("max_citation", 0),
        "topic_signal_hits": as_list(decision.get("topic_signal_hits")),
    }


def render_candidate_list(title: str, items: list[dict[str, Any]], required: bool) -> list[str]:
    lines = [
        f"## {title}",
        "",
        f"- 数量：{len(items)}",
        f"- 复核要求：{'必须全量复核' if required else '默认纳入召回复核，可按数量分批'}",
        "",
    ]
    if not items:
        lines.append("无。")
        lines.append("")
        return lines

    for index, item in enumerate(items, start=1):
        source_records = item.get("source_records") or []
        queries = sorted({clean_text(record.get("query")) for record in source_records if isinstance(record, dict) and clean_text(record.get("query"))})
        raw_paths = sorted({clean_text(record.get("raw_path")) for record in source_records if isinstance(record, dict) and clean_text(record.get("raw_path"))})
        lines.extend([
            f"{index}. {item['title'] or 'Untitled'}",
            f"   - 年份 / Venue：{item.get('year') or 'N/A'} / {item.get('venue') or 'N/A'}",
            f"   - 位置：{item.get('location')}",
            f"   - 原因：{item.get('reason') or 'N/A'}；置信：{item.get('confidence') or 'N/A'}",
            f"   - 命中词：{', '.join(item.get('keyword_hits') or []) or 'N/A'}",
            f"   - 标签：{', '.join(item.get('tags') or []) or 'N/A'}",
            f"   - Query：{', '.join(queries) or 'N/A'}",
            f"   - Raw：{', '.join(raw_paths) or 'N/A'}",
        ])
        if item.get("abstract"):
            lines.append(f"   - 摘要：{item['abstract'][:500]}")
        if item.get("risk_reasons"):
            lines.append(f"   - 风险原因：{json.dumps(item['risk_reasons'], ensure_ascii=False)}")
        lines.append("")
    return lines


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# 弱候选复核队列",
        "",
        f"- 时间：{payload['created_at']}",
        f"- Workspace：`{payload['workspace']}`",
        f"- 待复核弱候选：{payload['pending_weak_count']}",
        "",
        "## 复核原则",
        "",
        "- 所有弱候选都先进入统一复核队列，不直接进入主库。",
        "- `data/papers.*` 只保留 strong 或 trusted 候选。",
        "- 复核通过的弱候选应转化为规则升级、确认导入或新的 raw 记录后重新 build。",
        "- 复核结果不直接改 `data/`，应转化为 `topic.yaml` 规则变更、`overrides/` 修正或新的 raw 导入后重新 build。",
        "",
    ]
    lines.extend(render_candidate_list("待复核弱候选", payload["review_candidates"], required=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def build_weak_candidate_review(workspace: Path) -> dict[str, Any]:
    pending = read_json(data_dir(workspace) / "pending_review_candidates.json", [])
    pending = pending if isinstance(pending, list) else []

    pending_weak = [
        compact_candidate(item, "pending_review")
        for item in pending
        if isinstance(item, dict) and is_weak_candidate(item)
    ]
    pending_weak = sort_candidates(pending_weak)
    review_candidates = deduplicate_review_candidates(pending_weak)

    run_id = stamp()
    out_dir = reviews_dir(workspace)
    json_path = out_dir / f"weak_candidates_{run_id}.json"
    md_path = out_dir / f"weak_candidates_{run_id}.md"
    payload = {
        "run_id": run_id,
        "created_at": now_iso(),
        "workspace": workspace_label(workspace),
        "pending_weak_count": len(pending_weak),
        "review_candidates": review_candidates,
        "pending_weak": pending_weak,
    }
    payload = portable_workspace_data(workspace, payload)
    write_json(json_path, payload)
    write_markdown(md_path, payload)
    return {
        "run_id": run_id,
        "pending_weak_count": len(pending_weak),
        "review_candidate_count": len(review_candidates),
        "json": str(json_path),
        "markdown": str(md_path),
    }


def deduplicate_review_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        key = clean_text(item.get("candidate_key")) or candidate_key(item)
        if not key:
            key = f"title:{normalize_title(item.get('title', ''))}:{item.get('year') or ''}"
        if key not in merged:
            merged[key] = dict(item)
            continue
        current = merged[key]
        current["sources"] = sorted(set(as_list(current.get("sources")) + as_list(item.get("sources"))))
        current["keyword_hits"] = sorted(set(as_list(current.get("keyword_hits")) + as_list(item.get("keyword_hits"))))
        current["tags"] = sorted(set(as_list(current.get("tags")) + as_list(item.get("tags"))))
        current.setdefault("source_records", [])
        current["source_records"].extend(item.get("source_records") or [])
        if item.get("risk_reasons"):
            current.setdefault("risk_reasons", [])
            current["risk_reasons"].extend(item.get("risk_reasons") or [])
    return sort_candidates(list(merged.values()))


def normalize_decision_row(row: dict[str, Any], review_run: str = "") -> dict[str, Any]:
    decision = clean_text(row.get("decision")).lower()
    if decision not in {"accept", "reject", "defer", "anchor"}:
        raise ValueError(f"非法 decision：{row.get('decision')}")
    normalized = {
        "candidate_key": clean_text(row.get("candidate_key")) or candidate_key(row),
        "paper_key": clean_text(row.get("paper_key")),
        "title": clean_text(row.get("title")),
        "year": row.get("year"),
        "decision": decision,
        "reason": clean_text(row.get("reason")),
        "action": clean_text(row.get("action")),
        "review_run": clean_text(row.get("review_run")) or review_run,
        "created_at": clean_text(row.get("created_at")) or now_iso(),
        "ids": row.get("ids") if isinstance(row.get("ids"), dict) else {},
        "urls": row.get("urls") if isinstance(row.get("urls"), dict) else {},
        "sources": as_list(row.get("sources")),
        "paper_role": normalize_role(row.get("paper_role"), default=CORE_METHOD),
    }
    for key in ("queue_hash", "decision_context_hash", "scoring_policy"):
        if clean_text(row.get(key)):
            normalized[key] = clean_text(row.get(key))
    if not normalized["candidate_key"]:
        raise ValueError(f"decision 缺少 candidate_key/title/id：{row}")
    return normalized


def validate_review_decisions(queue_path: Path, decisions_path: Path) -> dict[str, Any]:
    queue_run, queue_items = load_review_payload(queue_path)
    expected: dict[str, dict[str, Any]] = {}
    for item in queue_items:
        key = clean_text(item.get("candidate_key")) or candidate_key(item)
        if key:
            expected[key] = item
    decisions: list[dict[str, Any]] = []
    invalid: list[str] = []
    for line_no, row in enumerate(iter_jsonl(decisions_path), start=1):
        if not isinstance(row, dict):
            invalid.append(f"{line_no}: not an object")
            continue
        try:
            decisions.append(normalize_decision_row(row, review_run=queue_run))
        except ValueError as exc:
            invalid.append(f"{line_no}: {exc}")
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for row in decisions:
        key = row["candidate_key"]
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 2:
            duplicates.append(key)
    decision_keys = set(seen)
    missing = sorted(set(expected) - decision_keys)
    extra = sorted(decision_keys - set(expected))
    counts = {name: 0 for name in ("accept", "reject", "defer", "anchor")}
    for row in decisions:
        counts[row["decision"]] = counts.get(row["decision"], 0) + 1
    valid = not invalid and not missing and not extra and not duplicates
    queue_hash = queue_fingerprint(queue_path)
    return {
        "valid": valid,
        "queue": str(queue_path),
        "decisions": str(decisions_path),
        "queue_run": queue_run,
        "queue_hash": queue_hash,
        "queue_count": len(expected),
        "decision_count": len(decisions),
        "decision_counts": counts,
        "coverage": {
            "covered": len(set(expected).intersection(decision_keys)),
            "missing": len(missing),
            "extra": len(extra),
            "duplicates": len(duplicates),
        },
        "missing_keys": missing[:50],
        "extra_keys": extra[:50],
        "duplicate_keys": duplicates[:50],
        "invalid_rows": invalid[:50],
    }


def apply_review_decisions(workspace: Path, decisions_path: Path, queue_path: Path | None = None) -> dict[str, Any]:
    validation = validate_review_decisions(queue_path, decisions_path) if queue_path else {}
    if validation and not validation.get("valid"):
        raise ValueError("decision 文件未通过校验，不能 apply")
    queue_hash = validation.get("queue_hash") if validation else ""
    context_hash = workspace_decision_context_hash(workspace)
    review_run = validation.get("queue_run") if validation else ""
    rows = []
    for row in iter_jsonl(decisions_path):
        if not isinstance(row, dict):
            continue
        normalized = normalize_decision_row(row, review_run=review_run)
        if queue_hash:
            normalized["queue_hash"] = queue_hash
        normalized["decision_context_hash"] = context_hash
        normalized.setdefault("scoring_policy", "papercompass.review_policy.v1")
        rows.append(normalized)
    out_path = applied_decisions_path(workspace)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "applied_at": now_iso(),
        "workspace": workspace_label(workspace),
        "input": str(decisions_path),
        "output": str(out_path),
        "applied_count": len(rows),
        "validation": validation,
    }
    manifest = portable_workspace_data(workspace, manifest)
    write_json(reviews_dir(workspace) / f"applied_decisions_{stamp()}.json", manifest)
    return manifest

from __future__ import annotations

from collections import Counter
from datetime import datetime
from math import ceil
from pathlib import Path
from typing import Any

from ..config import data_dir, manifests_dir, portable_workspace_data, state_dir, workspace_relative_path
from ..discovery import _is_generic_topic_anchor
from ..text import as_list, clean_text, read_json, write_json


DEFAULT_MIN_RULE_REPAIR_QUEUE = 1000
DEFAULT_GENERIC_DOMINANCE_RATIO = 0.25
DEFAULT_GENERIC_DOMINANCE_MIN = 100


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _candidate_topic_hits(item: dict[str, Any]) -> list[str]:
    hits: list[str] = []
    hits.extend(as_list(item.get("topic_signal_hits")))
    signals = item.get("topic_signals")
    if isinstance(signals, dict):
        hits.extend(as_list(signals.get("topic_signal_hits")))
    evidence = item.get("topic_relevance_evidence")
    if isinstance(evidence, dict):
        hits.extend(as_list(evidence.get("topic_signal_hits")))
    return [clean_text(hit).lower() for hit in hits if clean_text(hit)]


def _candidate_sources(item: dict[str, Any]) -> list[str]:
    sources = as_list(item.get("sources"))
    for record in item.get("source_records") or []:
        if isinstance(record, dict):
            sources.append(clean_text(record.get("source_name") or record.get("source_type")))
    return [source for source in sources if source]


def diagnose_review_queue(
    pending: list[dict[str, Any]],
    *,
    batch_size: int,
    max_batches: int | None,
) -> dict[str, Any]:
    """Diagnose whether the weak queue is reviewable before paying brain cost.

    The gate intentionally measures structural problems: queue far above
    available review budget, and generic source anchors dominating the queue.
    It does not use model judgement and does not encode topic-specific papers.
    """
    pending_count = len([item for item in pending if isinstance(item, dict)])
    safe_batch_size = max(int(batch_size or 25), 1)
    needed_batches = ceil(pending_count / safe_batch_size) if pending_count else 0
    effective_batches = (
        min(needed_batches, max_batches)
        if max_batches is not None
        else needed_batches
    )
    capacity = effective_batches * safe_batch_size if max_batches is not None else pending_count
    uncovered = max(0, pending_count - capacity)

    hit_counter: Counter[str] = Counter()
    generic_hit_counter: Counter[str] = Counter()
    source_counter: Counter[str] = Counter()
    generic_only_count = 0
    with_hits_count = 0
    examples: list[dict[str, Any]] = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        hits = _candidate_topic_hits(item)
        for hit in hits:
            hit_counter[hit] += 1
            if _is_generic_topic_anchor(hit):
                generic_hit_counter[hit] += 1
        if hits:
            with_hits_count += 1
            if all(_is_generic_topic_anchor(hit) for hit in hits):
                generic_only_count += 1
                if len(examples) < 20:
                    examples.append({
                        "title": item.get("title"),
                        "year": item.get("year"),
                        "topic_signal_hits": hits,
                        "sources": _candidate_sources(item),
                    })
        for source in _candidate_sources(item):
            source_counter[source] += 1

    capacity_for_rule = (
        max(DEFAULT_MIN_RULE_REPAIR_QUEUE, safe_batch_size * max_batches * 2)
        if max_batches is not None
        else DEFAULT_MIN_RULE_REPAIR_QUEUE
    )
    generic_only_ratio = (
        round(generic_only_count / pending_count, 4) if pending_count else 0.0
    )
    reasons: list[dict[str, Any]] = []
    if max_batches is not None and needed_batches > max_batches:
        reasons.append({
            "code": "weak_batches_over_budget",
            "severity": "budget",
            "pending_count": pending_count,
            "needed_batches": needed_batches,
            "max_batches": max_batches,
            "uncovered": uncovered,
        })
    if pending_count > capacity_for_rule:
        # A large queue is a *budget* problem (raise --weak-max-batches), not a
        # broken-topic problem. The genuine "rules are broken" signal is
        # generic_anchor_dominates below; only that forces rule_repair.
        reasons.append({
            "code": "weak_queue_too_large",
            "severity": "budget",
            "pending_count": pending_count,
            "rule_repair_threshold": capacity_for_rule,
        })
    if (
        generic_only_count >= DEFAULT_GENERIC_DOMINANCE_MIN
        and generic_only_ratio >= DEFAULT_GENERIC_DOMINANCE_RATIO
    ):
        reasons.append({
            "code": "generic_anchor_dominates",
            "severity": "rule_repair",
            "generic_only_count": generic_only_count,
            "generic_only_ratio": generic_only_ratio,
        })

    if any(reason["severity"] == "rule_repair" for reason in reasons):
        status = "needs_rule_repair"
    elif reasons:
        status = "partial_due_to_budget"
    else:
        status = "ok"

    return {
        "status": status,
        "should_stop": status != "ok",
        "pending_count": pending_count,
        "batch_size": safe_batch_size,
        "max_batches": max_batches,
        "needed_batches": needed_batches,
        "effective_batches": effective_batches,
        "recommended_max_batches": needed_batches,
        "review_capacity": capacity,
        "uncovered_count": uncovered,
        "reasons": reasons,
        "reason_codes": [reason["code"] for reason in reasons],
        "topic_signal_top": hit_counter.most_common(30),
        "generic_topic_signal_top": generic_hit_counter.most_common(30),
        "generic_only_count": generic_only_count,
        "generic_only_ratio": generic_only_ratio,
        "with_topic_hits_count": with_hits_count,
        "source_top": source_counter.most_common(20),
        "generic_only_examples": examples,
    }


def write_review_queue_diagnosis(
    workspace: Path,
    *,
    batch_size: int,
    max_batches: int | None,
) -> dict[str, Any]:
    pending = read_json(data_dir(workspace) / "pending_review_candidates.json", [])
    pending_items = [item for item in pending if isinstance(item, dict)] if isinstance(pending, list) else []
    diagnosis = diagnose_review_queue(
        pending_items,
        batch_size=batch_size,
        max_batches=max_batches,
    )
    run = _stamp()
    manifest_path = manifests_dir(workspace) / f"review_queue_diagnosis_{run}.json"
    report_path = state_dir(workspace) / "reports" / f"review_queue_diagnosis_{run}.md"
    diagnosis["manifest"] = workspace_relative_path(workspace, manifest_path)
    diagnosis["markdown"] = workspace_relative_path(workspace, report_path)
    diagnosis = portable_workspace_data(workspace, diagnosis)
    write_json(manifest_path, diagnosis)
    _write_review_queue_markdown(report_path, diagnosis)
    return diagnosis


def _write_review_queue_markdown(path: Path, diagnosis: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    reason_lines = [
        f"- `{reason.get('code')}`：severity={reason.get('severity')}，"
        f"pending={reason.get('pending_count', diagnosis.get('pending_count'))}"
        for reason in diagnosis.get("reasons") or []
    ]
    top_hits = [
        f"- {hit}：{count}"
        for hit, count in diagnosis.get("topic_signal_top") or []
    ]
    generic_hits = [
        f"- {hit}：{count}"
        for hit, count in diagnosis.get("generic_topic_signal_top") or []
    ]
    lines = [
        "# Review Queue 质量门诊断",
        "",
        f"- 状态：`{diagnosis.get('status')}`",
        f"- Pending：{diagnosis.get('pending_count')}",
        f"- Batch：{diagnosis.get('batch_size')} x {diagnosis.get('max_batches')}",
        f"- 需要 batch：{diagnosis.get('needed_batches')}",
        f"- 实际容量：{diagnosis.get('review_capacity')}",
        f"- 未覆盖候选：{diagnosis.get('uncovered_count')}",
        f"- 建议复核批次:重跑加 `--weak-max-batches {diagnosis.get('recommended_max_batches')}`(或更高)",
        f"- 泛锚点-only 数量:{diagnosis.get('generic_only_count')} "
        f"({diagnosis.get('generic_only_ratio')})",
        "",
        "## 阻断原因",
        "",
        *(reason_lines or ["- 无"]),
        "",
        "## Topic Signal Top",
        "",
        *(top_hits or ["- 无"]),
        "",
        "## 泛锚点 Top",
        "",
        *(generic_hits or ["- 无"]),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")

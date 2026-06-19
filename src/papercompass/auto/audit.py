"""Recall / precision spot check for an auto-built workspace.

Usage from Python:
    from papercompass.auto.audit import audit_workspace
    report = audit_workspace(workspace, brain=brain, sample_size=30)

The recall side is exact: it checks coverage of optional source-backed
anchor/seed rows in anchors.jsonl (or legacy seed_papers.jsonl). The precision
side is brain-judged on a random sample of main-library papers — the brain is
asked, given the topic description, whether each sample is in scope.

This is meant for the human / agent reviewing a finished build, not for the
auto-build pipeline itself (which already has built-in source-backed anchor
coverage, defer ratio, weak triage thresholds).
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from ..anchors import existing_anchors_plan_path, iter_anchor_rows
from ..config import data_dir, load_topic_config
from ..plugins import BrainPlugin
from ..text import normalize_title, read_json
from ..normalize import identity_keys
from .prompts import render_candidate_block
from .state import log_brain_call


PRECISION_PROMPT = """\
For each paper below, decide whether it is on-topic for the local literature
library defined by this topic description. Answer "in_scope" if the paper is
clearly part of the topic; "out_of_scope" if it's clearly unrelated; "boundary"
if it's an ambiguous adjacent area.

Topic: {topic_name}
Description: {topic_description}
Subtopics: {subtopics}
Year floor: {min_year}

Papers ({count}):
{block}

Return JSON only.
"""


def _precision_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "judgements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate_key": {"type": "string"},
                        "title": {"type": "string"},
                        "verdict": {
                            "type": "string",
                            "enum": ["in_scope", "out_of_scope", "boundary"],
                        },
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    }


def _all_known_titles_and_ids(workspace: Path) -> dict[str, set[str]]:
    """Returns key pools by library location."""
    pools: dict[str, set[str]] = {"main": set(), "anchors": set(), "pending": set(), "rejected": set()}
    for label, fname in (
        ("main", "papers.json"),
        ("anchors", "anchor_papers.json"),
        ("pending", "pending_review_candidates.json"),
        ("rejected", "rejected_candidates.json"),
    ):
        data = read_json(data_dir(workspace) / fname, [])
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            t = normalize_title(item.get("title", ""))
            if t:
                pools[label].add(t)
            for k in identity_keys(item):
                pools[label].add(k)
    return pools


def seed_recall(workspace: Path) -> dict[str, Any]:
    seeds_path = existing_anchors_plan_path(workspace)
    if not seeds_path.exists():
        return {"status": "no_seeds", "total": 0, "recall_main": 0.0, "missing": []}
    seeds = iter_anchor_rows(workspace)
    pools = _all_known_titles_and_ids(workspace)
    in_main = 0
    in_anchors = 0
    in_pending = 0
    in_rejected = 0
    missing: list[dict[str, Any]] = []
    for seed in seeds:
        title_key = normalize_title(seed.get("title", ""))
        ids = list(identity_keys(seed))
        for k in ("arxiv_id", "doi"):
            v = (seed.get(k) or "").strip()
            if v and k == "arxiv_id":
                ids.append(f"arxiv:{v.lower()}")
            if v and k == "doi":
                ids.append(f"doi:{v.lower()}")
        candidates = {title_key} | set(ids) - {""}
        if candidates & pools["main"]:
            in_main += 1
        elif candidates & pools["anchors"]:
            in_anchors += 1
        elif candidates & pools["pending"]:
            in_pending += 1
        elif candidates & pools["rejected"]:
            in_rejected += 1
        else:
            missing.append(seed)
    total = len(seeds) or 1
    return {
        "status": "checked",
        "total": len(seeds),
        "in_main": in_main,
        "in_anchors": in_anchors,
        "in_pending": in_pending,
        "in_rejected": in_rejected,
        "missing_count": len(missing),
        "recall_main": round(in_main / total, 3),
        "recall_anywhere": round((in_main + in_anchors + in_pending + in_rejected) / total, 3),
        "missing": missing,
    }


def precision_sample(
    workspace: Path,
    *,
    brain: BrainPlugin,
    sample_size: int = 30,
    seed: int = 0,
) -> dict[str, Any]:
    topic = load_topic_config(workspace)
    papers = read_json(data_dir(workspace) / "papers.json", [])
    if not isinstance(papers, list) or not papers:
        return {"status": "empty", "sample_size": 0}
    rng = random.Random(seed)
    sample = rng.sample(papers, min(sample_size, len(papers)))
    rows = []
    for p in sample:
        rows.append(
            {
                "candidate_key": p.get("paper_key", "") or normalize_title(p.get("title", "")),
                "title": p.get("title", ""),
                "year": p.get("year"),
                "venue": p.get("venue", ""),
                "abstract": (p.get("abstract", "") or "")[:600],
                "topic_signal_hits": p.get("topic_signals", {}).get("topic_signal_hits", []),
                "ids": p.get("ids", {}),
            }
        )
    # Batch the audit: a 40-sample one-shot prompt overruns reasoning
    # models (deepseek-v4-pro returned 359 chars / parsed=False on its
    # first try). 10 papers per batch keeps each brain call ≤4-5KB output
    # and lets unparsed-batch failures be local rather than catastrophic.
    judgements: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    batch_size = 10
    batch_count = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        block = render_candidate_block(chunk)
        prompt = PRECISION_PROMPT.format(
            topic_name=topic.get("name") or topic.get("topic_id"),
            topic_description=topic.get("description") or "",
            subtopics=", ".join(topic.get("subtopics") or []),
            min_year=topic.get("min_year") or "",
            count=len(chunk),
            block=block,
        )
        resp = brain.ask(prompt, schema=_precision_schema(), timeout=600)
        if not isinstance(resp.parsed, dict):
            # one retry with stricter formatting hint
            resp = brain.ask(
                prompt
                + "\n\n# IMPORTANT\nReturn the full JSON object exactly. "
                "Do not call tools, do not summarize, do not omit fields.",
                schema=_precision_schema(),
                timeout=600,
            )
        log_brain_call(
            workspace,
            stage="audit_precision",
            plugin=resp.plugin,
            prompt_tokens=len(prompt),
            response_text=resp.text,
            parsed_ok=isinstance(resp.parsed, dict),
            duration_seconds=resp.duration_seconds,
            extra={"batch": batch_count, "batch_size": len(chunk), **(resp.extra if isinstance(resp.extra, dict) else {})},
        )
        # idle-keepalive stderr line: prevents background-task wrappers
        # (which monitor stdout/stderr) from killing long-running brain
        # calls on codex/opencode CLIs that block output during reasoning.
        import sys as _sys
        from datetime import datetime as _dt_now
        _sys.stderr.write(
            f"papercompass auto: {_dt_now.now().isoformat(timespec='seconds')} "
            f"[audit_precision] batch {batch_count+1} done in "
            f"{resp.duration_seconds:.1f}s\n"
        )
        _sys.stderr.flush()
        batch_count += 1
        if isinstance(resp.parsed, dict):
            for j in resp.parsed.get("judgements") or []:
                if not isinstance(j, dict):
                    continue
                key = (j.get("candidate_key") or "").strip()
                if key and key in seen_keys:
                    continue
                if key:
                    seen_keys.add(key)
                judgements.append(j)

    counts = {"in_scope": 0, "out_of_scope": 0, "boundary": 0}
    for j in judgements:
        v = (j.get("verdict") or "").strip()
        if v in counts:
            counts[v] += 1
    n = len(judgements) or 1
    return {
        "status": "judged",
        "sample_size": len(rows),
        "judgements": judgements,
        "counts": counts,
        "precision_in_scope": round(counts["in_scope"] / n, 3),
        "precision_in_scope_or_boundary": round(
            (counts["in_scope"] + counts["boundary"]) / n, 3
        ),
    }


def audit_workspace(
    workspace: Path,
    *,
    brain: BrainPlugin | None = None,
    sample_size: int = 30,
) -> dict[str, Any]:
    recall = seed_recall(workspace)
    precision: dict[str, Any] = {"status": "skipped", "reason": "no brain provided"}
    if brain is not None:
        precision = precision_sample(workspace, brain=brain, sample_size=sample_size)
    out_path = workspace / ".papercompass" / "auto" / "audit_recall_precision.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"recall": recall, "precision": precision}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"recall": recall, "precision": precision, "report": str(out_path)}

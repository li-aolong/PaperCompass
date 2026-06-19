"""Individual stages of the auto-build pipeline.

Each stage is a small, idempotent step. The orchestrator composes them based
on the workspace's checkpoint state and configured budget. All stages return
a dict that is recorded into the state file for audit / resume.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

import time
from datetime import datetime as _dt

from .. import build as build_mod
from ..anchors import existing_anchors_plan_path, iter_anchor_rows
from ..build import build_workspace, candidate_match_keys, record_agent_run_step
from ..candidate_review import (
    apply_review_decisions,
    build_weak_candidate_review,
    candidate_key as cand_key,
    validate_review_decisions,
)
from ..catalog import build_catalog
from ..config import (
    data_dir,
    ensure_workspace_dirs,
    load_topic_config,
    raw_dir,
    state_dir,
)
from ..discovery import make_coverage_report, run_discovery
from ..normalize import identity_keys
from ..plugins import BrainPlugin
from ..plugins.brain import BrainInvocationError
from ..qa import build_quality_report
from ..roles import BACKGROUND_ANCHOR, BOUNDARY_NEGATIVE, CORE_METHOD, NEGATIVE_ROLES, normalize_role, seed_required, seed_verified_source_backed
from ..scope import publication_scope_from_topic, render_publication_scope
from ..text import iter_jsonl, normalize_title, read_json
from .brain_score import score_candidate_batch
from .embed import score_candidates_against_topic
from .fusion import (
    BoundaryThresholds,
    FusionThresholds,
    FusionWeights,
    fuse_and_verdict_with_policy,
    resolve_boundary,
)
from .lint import (
    filter_hallucinated_decisions,
    format_issues_for_prompt,
    issues_summary,
    lint_plan_output,
)
from .metadata import build_anchor_stats, metadata_score
from .plan import inject_verified_seeds_to_raw, render_plan, write_plan
from .prompts import (
    PLAN_PROMPT,
    plan_schema,
    render_candidate_block,
)
from .quality_gate import write_review_queue_diagnosis
from .state import AutoState, log_brain_call


# --------------------------------------------------------------------------- #
# Stage: plan_direction
# --------------------------------------------------------------------------- #


def stage_plan_direction(
    workspace: Path,
    direction: str,
    *,
    min_year: int | None,
    brain: BrainPlugin,
    state: AutoState,
    topic_id_override: str | None = None,
    prior_markdown: str | None = None,
    seed_cap: int | None = None,
) -> dict[str, Any]:
    if state.stage_done("plan_direction") and (workspace / "topic.yaml").exists():
        topic = load_topic_config(workspace)
        if topic_id_override and topic.get("topic_id") != topic_id_override:
            raise RuntimeError(
                "cached topic_id 与 workspace 命名不一致："
                f"topic.yaml={topic.get('topic_id')} workspace={topic_id_override}。"
                "请使用 --fresh 重新生成，或换一个规范 workspace。"
            )
        return {"status": "cached", "topic_id": topic.get("topic_id")}

    state.begin_stage("plan_direction", direction=direction, min_year=min_year)
    if prior_markdown and prior_markdown.strip():
        trimmed = prior_markdown.strip()
        if len(trimmed) > 24000:
            trimmed = trimmed[:24000] + "\n\n…(prior markdown truncated)…"
        prior_section = (
            "\n\nPRIOR MANUAL REVIEW (verbatim, do NOT paraphrase):\n"
            "Use this as the authoritative source for judge_examples, "
            "publication_scope, and search_hints. If a paper is named in this "
            "prior review, use it only as a judge_example or search hint unless "
            "the programmatic source layer later verifies it. Do NOT invent "
            "arxiv IDs, DOI strings, years, or titles.\n"
            f"---BEGIN PRIOR MARKDOWN---\n{trimmed}\n---END PRIOR MARKDOWN---\n"
        )
    else:
        prior_section = ""
    prompt = PLAN_PROMPT.format(
        direction=direction,
        min_year=min_year if min_year else "(unspecified)",
        prior_markdown_section=prior_section,
    )
    schema = plan_schema()
    resp = brain.ask(prompt, schema=schema, timeout=900)
    log_brain_call(
        workspace,
        stage="plan_direction",
        plugin=resp.plugin,
        prompt_tokens=len(prompt),
        response_text=resp.text,
        parsed_ok=isinstance(resp.parsed, dict),
        duration_seconds=resp.duration_seconds,
        extra=dict(resp.extra) if isinstance(resp.extra, dict) else None,
    )
    if not isinstance(resp.parsed, dict):
        # Some brains (deepseek reasoning models, gemini under load) give
        # truncated or malformed output on the first call. Retry once
        # before failing — same prompt, fresh subprocess invocation.
        resp_retry = brain.ask(
            prompt
            + "\n\n# IMPORTANT\nReturn the full JSON object exactly. Do not "
            "call tools, do not summarize, do not omit fields.",
            schema=schema,
            timeout=900,
        )
        log_brain_call(
            workspace,
            stage="plan_direction",
            plugin=resp_retry.plugin,
            prompt_tokens=len(prompt),
            response_text=resp_retry.text,
            parsed_ok=isinstance(resp_retry.parsed, dict),
            duration_seconds=resp_retry.duration_seconds,
            extra={"unparsed_retry": True, **(resp_retry.extra if isinstance(resp_retry.extra, dict) else {})},
        )
        if isinstance(resp_retry.parsed, dict):
            resp = resp_retry
        else:
            state.end_stage("plan_direction", status="failed", reason="brain output not parseable as JSON")
            raise RuntimeError("plan_direction: brain output was not valid JSON; see iterations.jsonl")

    # Structural lint of the brain's plan. Critical issues (no strong_terms,
    # too few seeds, etc.) trigger one retry with the lint feedback inlined
    # in the prompt; warnings are recorded in state but don't block.
    lint_issues = lint_plan_output(resp.parsed, direction)
    critical = [i for i in lint_issues if i.is_critical()]
    if critical:
        retry_prompt = (
            prompt
            + "\n\n# Linter feedback on previous attempt\n"
            + format_issues_for_prompt(lint_issues)
        )
        resp_retry = brain.ask(retry_prompt, schema=schema, timeout=900)
        log_brain_call(
            workspace,
            stage="plan_direction",
            plugin=resp_retry.plugin,
            prompt_tokens=len(retry_prompt),
            response_text=resp_retry.text,
            parsed_ok=isinstance(resp_retry.parsed, dict),
            duration_seconds=resp_retry.duration_seconds,
            extra={"lint_retry": True, **(resp_retry.extra if isinstance(resp_retry.extra, dict) else {})},
        )
        if isinstance(resp_retry.parsed, dict):
            lint_issues_retry = lint_plan_output(resp_retry.parsed, direction)
            critical_retry = [i for i in lint_issues_retry if i.is_critical()]
            if not critical_retry:
                resp = resp_retry
                lint_issues = lint_issues_retry
            else:
                state.end_stage(
                    "plan_direction",
                    status="failed",
                    reason="critical lint issues persisted after retry",
                    lint=issues_summary(lint_issues_retry),
                )
                raise RuntimeError(
                    f"plan_direction lint critical: {[i.code for i in critical_retry]}"
                )
        else:
            state.end_stage(
                "plan_direction",
                status="failed",
                reason="brain retry output not parseable as JSON",
            )
            raise RuntimeError("plan_direction retry: brain output was not valid JSON")

    plan = resp.parsed
    if min_year and not plan.get("min_year"):
        plan["min_year"] = min_year
    # Bootstrap recall anchors from OpenAlex. These are source-backed records,
    # not brain-recalled paper titles. Failure is silent: a build with no
    # anchors is valid, and QA will not treat "no seeds" as a hallucinated gap.
    from .seed_search import search_seed_candidates, source_backed_seed_anchors

    hints_for_search = (plan.get("search_hints") or [])[:2]
    search_seeds = search_seed_candidates(
        direction,
        strong_terms=hints_for_search,
        min_year=plan.get("min_year"),
        max_results=30,
    )
    if seed_cap is not None:
        effective_cap = max(1, int(seed_cap))
    else:
        effective_cap = 15
    plan["seed_papers"] = source_backed_seed_anchors(
        search_seeds,
        cap=effective_cap,
    )

    topic_yaml, sources_yaml, seeds = render_plan(
        plan,
        direction,
        topic_id_override=topic_id_override,
    )
    if topic_id_override:
        plan["topic_id"] = topic_yaml.get("topic_id")
    ensure_workspace_dirs(workspace)
    write_plan(workspace, topic_yaml, sources_yaml, seeds)
    injected_count = inject_verified_seeds_to_raw(workspace, plan.get("seed_papers") or [])
    raw_plan_path = state_dir(workspace) / "auto" / "plan_response.json"
    raw_plan_path.parent.mkdir(parents=True, exist_ok=True)
    raw_plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    record_agent_run_step(
        workspace,
        phase="plan_direction",
        status="completed",
        summary=(
            f"auto-build plan: {len(topic_yaml.get('search_hints', []))} hints, "
            f"{len((topic_yaml.get('judge_examples') or {}).get('in_scope', []))} in-anchors, "
            f"{len(seeds)} seeds"
        ),
        files=["topic.yaml", "sources.yaml", ".papercompass/plans/anchors.jsonl"],
    )

    state.end_stage(
        "plan_direction",
        status="completed",
        topic_id=topic_yaml.get("topic_id"),
        hints_count=len(topic_yaml.get("search_hints", [])),
        seed_count=len(seeds),
        plugin=resp.plugin,
        lint=issues_summary(lint_issues),
    )
    return {
        "status": "completed",
        "topic_id": topic_yaml.get("topic_id"),
        "search_hints": topic_yaml.get("search_hints", []),
        "seed_count": len(seeds),
    }


# --------------------------------------------------------------------------- #
# Stage: discover
# --------------------------------------------------------------------------- #


def stage_discover(
    workspace: Path,
    *,
    min_year: int | None,
    max_remote_calls: int,
    sources: list[str] | None,
    refresh: bool,
    state: AutoState,
    iteration: int = 1,
) -> dict[str, Any]:
    stage = f"discover_iter{iteration}"
    if state.stage_done(stage):
        return {"status": "cached"}
    state.begin_stage(stage, max_remote_calls=max_remote_calls, sources=sources)
    result = run_discovery(
        workspace,
        sources=sources,
        min_year=min_year,
        max_year=None,
        refresh=refresh,
        build=True,
        catalog=True,
        paperlists_venues=None,
        timeout=35,
        max_remote_calls=max_remote_calls,
    )
    src_results = result.get("source_results") or []
    seen = sum((r.get("seen") or 0) for r in src_results if isinstance(r, dict))
    kept = sum((r.get("kept") or 0) for r in src_results if isinstance(r, dict))
    errors = sum(len(r.get("errors", []) or []) for r in src_results if isinstance(r, dict))
    state.end_stage(
        stage,
        status="completed",
        summary={
            "seen": seen,
            "kept": kept,
            "remote_calls_used": result.get("remote_calls_used"),
            "remote_calls_limit": result.get("remote_calls_limit"),
            "paper_count": result.get("paper_count"),
            "source_errors": errors,
        },
    )
    return {"status": "completed", "discovery": result}


# --------------------------------------------------------------------------- #
# Stage: seed_check + repair
# --------------------------------------------------------------------------- #


def _all_known_titles_and_ids(workspace: Path) -> set[str]:
    pool: set[str] = set()
    for path in (
        data_dir(workspace) / "papers.json",
        data_dir(workspace) / "anchor_papers.json",
        data_dir(workspace) / "pending_review_candidates.json",
        data_dir(workspace) / "rejected_candidates.json",
    ):
        data = read_json(path, [])
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            t = normalize_title(item.get("title", ""))
            if t:
                pool.add(t)
            for k in identity_keys(item):
                pool.add(k)
    raw_root = raw_dir(workspace)
    if raw_root.exists():
        for path in raw_root.glob("**/*.jsonl"):
            for row in iter_jsonl(path):
                if not isinstance(row, dict):
                    continue
                inner = row.get("raw") if isinstance(row.get("raw"), dict) else row
                if isinstance(inner, dict):
                    t = normalize_title(inner.get("title", ""))
                    if t:
                        pool.add(t)
                    for k in identity_keys(inner):
                        pool.add(k)
    return pool


def stage_seed_check(workspace: Path, *, state: AutoState) -> dict[str, Any]:
    stage = "seed_check"
    state.begin_stage(stage)
    seeds_path = existing_anchors_plan_path(workspace)
    if not seeds_path.exists():
        state.end_stage(stage, status="completed", missing=0, total=0, note="no seeds configured")
        return {"status": "no_seeds", "missing": []}
    seeds = iter_anchor_rows(workspace)
    pool = _all_known_titles_and_ids(workspace)
    missing: list[dict[str, Any]] = []
    for idx, seed in enumerate(seeds):
        if not seed_required(seed) or not seed_verified_source_backed(seed):
            continue
        title_key = normalize_title(seed.get("title", ""))
        ids = identity_keys(seed)
        # build local-style id keys for arxiv/doi if explicitly listed
        for k in ("arxiv_id", "doi"):
            v = (seed.get(k) or "").strip()
            if v and k == "arxiv_id":
                ids.append(f"arxiv:{v.lower()}")
            if v and k == "doi":
                ids.append(f"doi:{v.lower()}")
        if title_key and title_key in pool:
            continue
        if any(k in pool for k in ids if k):
            continue
        seed_with_idx = {"seed_index": idx, **seed}
        missing.append(seed_with_idx)
    state.end_stage(stage, status="completed", total=len(seeds), missing=len(missing))
    return {"status": "checked", "missing": missing, "total": len(seeds)}


def stage_seed_repair(
    workspace: Path,
    missing: list[dict[str, Any]],
    *,
    brain: BrainPlugin,
    state: AutoState,
) -> dict[str, Any]:
    _ = brain
    stage = "seed_repair"
    if not missing:
        state.end_stage(stage, status="skipped", note="no missing seeds")
        return {"status": "skipped"}
    state.begin_stage(stage, missing=len(missing))
    actions: list[dict[str, Any]] = []
    deterministic_actions: list[dict[str, Any]] = []
    skipped_without_direct_id: list[dict[str, Any]] = []
    for seed in missing:
        title = (seed.get("title") or "").strip()
        has_direct_id = any((seed.get(key) or "").strip() for key in ("arxiv_id", "doi", "url"))
        if title and has_direct_id and seed_verified_source_backed(seed):
            deterministic_actions.append({
                "seed_index": seed.get("seed_index"),
                "title": title,
                "year": seed.get("year"),
                "decision": "add_paper",
                "arxiv_id": (seed.get("arxiv_id") or "").strip(),
                "doi": (seed.get("doi") or "").strip(),
                "url": (seed.get("url") or "").strip(),
                "reason": "deterministic_seed_id_repair",
                "why_seed": seed.get("why_seed") or "",
                "paper_role": normalize_role(seed.get("paper_role"), default=CORE_METHOD),
            })
        else:
            skipped_without_direct_id.append(seed)
    actions.extend(deterministic_actions)

    added_papers = 0
    seed_rows: list[dict[str, Any]] = []
    missing_by_index = {seed.get("seed_index"): seed for seed in missing if isinstance(seed, dict)}
    for action in actions:
        decision = (action.get("decision") or "").strip()
        title = (action.get("title") or "").strip()
        if decision == "add_paper":
            if not title:
                continue
            source_seed = missing_by_index.get(action.get("seed_index"), {})
            if not seed_verified_source_backed(source_seed):
                continue
            raw: dict[str, Any] = {"title": title}
            if action.get("year"):
                raw["year"] = action["year"]
            if action.get("arxiv_id"):
                raw["arxiv_id"] = action["arxiv_id"]
            if action.get("doi"):
                raw["doi"] = action["doi"]
            if action.get("url"):
                raw["url"] = action["url"]
            if action.get("why_seed"):
                raw["notes"] = [str(action["why_seed"])]
            raw["paper_role"] = normalize_role(action.get("paper_role") or source_seed.get("paper_role"), default=CORE_METHOD)
            if seed_required(source_seed):
                raw["required"] = True
            seed_rows.append(
                {
                    "source_name": "auto_seed_repair",
                    "source_type": "manual",
                    "query": "auto_seed_repair",
                    "fetched_at": _dt.now().isoformat(timespec="seconds"),
                    "source_item_id": raw.get("arxiv_id") or raw.get("doi") or "",
                    "raw": raw,
                }
            )
            added_papers += 1
    if seed_rows:
        manual_dir = raw_dir(workspace) / "manual"
        manual_dir.mkdir(parents=True, exist_ok=True)
        out_path = manual_dir / f"{_dt.now().strftime('%Y%m%d_%H%M%S')}_auto_seed_repair_batch.jsonl"
        with out_path.open("w", encoding="utf-8") as handle:
            for row in seed_rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    state.end_stage(
        stage,
        status="completed",
        added_papers=added_papers,
        deterministic_added=len(deterministic_actions),
        skipped_without_direct_id=len(skipped_without_direct_id),
    )
    return {
        "status": "completed",
        "added_papers": added_papers,
        "deterministic_added": len(deterministic_actions),
        "skipped_without_direct_id": skipped_without_direct_id,
    }


def _compact_for_review(batch: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cand in batch:
        if not isinstance(cand, dict):
            continue
        out.append(
            {
                "candidate_key": cand.get("candidate_key") or cand_key(cand),
                "title": cand.get("title", ""),
                "year": cand.get("year"),
                "venue": cand.get("venue", ""),
                "abstract": cand.get("abstract", ""),
                "ids": cand.get("ids", {}),
                "topic_signal_hits": cand.get("topic_signal_hits")
                or cand.get("topic_signals", {}).get("topic_signal_hits", []),
            }
        )
    return out


def _chunked(items: list[Any], n: int) -> Iterable[list[Any]]:
    if n <= 0:
        n = 25
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _candidate_key_set(batch: Iterable[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for cand in batch:
        if not isinstance(cand, dict):
            continue
        key = (cand.get("candidate_key") or "").strip()
        if key:
            keys.add(key)
    return keys


def _score_key_set(rows: Iterable[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for row in rows:
        key = (row.get("candidate_key") or "").strip()
        if key:
            keys.add(key)
    return keys


def _required_seed_role_index(workspace: Path) -> dict[str, str]:
    seeds_path = existing_anchors_plan_path(workspace)
    if not seeds_path.exists():
        return {}
    index: dict[str, str] = {}
    for seed in iter_anchor_rows(workspace):
        if not isinstance(seed, dict) or not seed_required(seed):
            continue
        role = normalize_role(seed.get("paper_role"), default=CORE_METHOD)
        if role in NEGATIVE_ROLES:
            continue
        for key in candidate_match_keys(seed) + identity_keys(seed):
            if key:
                index[key] = role
    return index


def _required_seed_role_for_candidate(
    candidate: dict[str, Any],
    seed_role_index: dict[str, str],
) -> str:
    if not seed_role_index:
        return ""
    for key in candidate_match_keys(candidate) + identity_keys(candidate):
        role = seed_role_index.get(key)
        if role:
            return role
    return ""


def _required_seed_role_for_review_row(
    row: dict[str, Any],
    pending_by_key: dict[str, dict[str, Any]],
    seed_role_index: dict[str, str],
) -> str:
    raw_role = (row.get("required_seed_role") or "").strip()
    if raw_role:
        role = normalize_role(raw_role, default="")
        if role and role not in NEGATIVE_ROLES:
            return role
    key = (row.get("candidate_key") or "").strip()
    candidate = pending_by_key.get(key) or row
    return _required_seed_role_for_candidate(candidate, seed_role_index)


def _missing_score_candidates(
    batch: Iterable[dict[str, Any]],
    *,
    existing_keys: set[str],
    returned_keys: set[str],
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    covered = existing_keys | returned_keys
    for cand in batch:
        if not isinstance(cand, dict):
            continue
        key = (cand.get("candidate_key") or "").strip()
        if key and key not in covered:
            missing.append(cand)
    return missing


def _review_decision_for_score(verdict: str, paper_role: str, brain_score: int | None, fused_score: float) -> tuple[str, str]:
    role = normalize_role(paper_role, default=CORE_METHOD)
    if role == BOUNDARY_NEGATIVE:
        return "reject", "keep_rejected"
    if role == BACKGROUND_ANCHOR and (brain_score is not None and brain_score >= 35 or fused_score >= 40):
        return "anchor", "add_to_anchor"
    if verdict == "in_scope":
        return "accept", "add_to_main"
    if verdict == "out_of_scope":
        return "reject", "keep_rejected"
    return "defer", "keep_pending"


# --------------------------------------------------------------------------- #
# Stage: build + qa + catalog
# --------------------------------------------------------------------------- #


def stage_build(workspace: Path, *, state: AutoState, label: str = "build") -> dict[str, Any]:
    state.begin_stage(label)
    result = build_workspace(workspace)
    coverage_report = make_coverage_report(workspace)
    state.end_stage(
        label,
        status="completed",
        papers=result.get("paper_count"),
        pending=result.get("pending_count"),
        rejected=result.get("rejected_count"),
        coverage_papers=coverage_report.get("paper_count"),
    )
    return {"status": "completed", **result, "coverage_report": coverage_report}


def stage_qa(workspace: Path, *, state: AutoState, label: str = "qa") -> dict[str, Any]:
    state.begin_stage(label)
    report = build_quality_report(workspace, strict=False)
    state.end_stage(
        label,
        status="completed",
        result_status=report.get("status"),
        critical=report.get("critical"),
        warnings=report.get("warnings"),
        counts=report.get("counts"),
    )
    return {"status": "completed", "report": report}


def stage_review_queue_gate(
    workspace: Path,
    *,
    state: AutoState,
    batch_size: int,
    max_batches: int | None,
) -> dict[str, Any]:
    state.begin_stage("review_queue_gate")
    diagnosis = write_review_queue_diagnosis(
        workspace,
        batch_size=batch_size,
        max_batches=max_batches,
    )
    state.end_stage(
        "review_queue_gate",
        status="completed",
        result_status=diagnosis.get("status"),
        pending_count=diagnosis.get("pending_count"),
        needed_batches=diagnosis.get("needed_batches"),
        effective_batches=diagnosis.get("effective_batches"),
        uncovered_count=diagnosis.get("uncovered_count"),
        reasons=diagnosis.get("reason_codes"),
        manifest=diagnosis.get("manifest"),
        markdown=diagnosis.get("markdown"),
    )
    return {"status": "completed", "diagnosis": diagnosis}


# --------------------------------------------------------------------------- #
# Stage: score_papers (v3 main precision pipeline)
# --------------------------------------------------------------------------- #


def stage_score_papers(
    workspace: Path,
    *,
    brain: BrainPlugin,
    state: AutoState,
    batch_size: int = 25,
    max_batches: int | None = None,
    weights: FusionWeights | None = None,
    thresholds: FusionThresholds | None = None,
) -> dict[str, Any]:
    """v3 fusion pipeline: every pending candidate gets emb + brain + meta
    scores → fused → in_scope / boundary / out_of_scope verdict. The
    decisions file written by this stage drives apply_review_decisions
    (in_scope → main, out_of_scope → rejected, boundary stays pending for
    stage_resolve_boundary).

    No keyword rules. The fusion weights/thresholds are configurable
    (defaults from FusionWeights/FusionThresholds), and `papercompass
    calibrate` can grid-search them on seed-recall + audit-precision.
    """
    if state.stage_done("score_papers"):
        return {"status": "cached"}
    state.begin_stage("score_papers")

    queue_meta = build_weak_candidate_review(workspace)
    queue_path = Path(queue_meta["json"]) if queue_meta.get("json") else None
    if not queue_path or not queue_path.exists():
        state.end_stage("score_papers", status="skipped", note="no queue produced")
        return {"status": "skipped"}
    queue_data = read_json(queue_path, {})
    candidates = queue_data.get("review_candidates") if isinstance(queue_data, dict) else []
    if not candidates:
        state.end_stage("score_papers", status="skipped", note="empty queue")
        return {"status": "skipped"}
    seed_role_index = _required_seed_role_index(workspace)
    required_seed_roles_by_key: dict[str, str] = {}
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        key = (cand.get("candidate_key") or "").strip()
        role = _required_seed_role_for_candidate(cand, seed_role_index)
        if key and role:
            required_seed_roles_by_key[key] = role

    topic = load_topic_config(workspace)
    direction_raw = topic.get("direction_raw") or topic.get("name") or topic.get("topic_id") or ""
    judge_examples = topic.get("judge_examples") or {"in_scope": [], "out_of_scope": []}
    publication_scope_text = render_publication_scope(publication_scope_from_topic(topic))
    keyword_text = topic.get("search_keyword_text") or ""

    # Anchor pool for metadata score: source-backed anchors from the plan plus
    # already-accepted main library papers (if any from a prior partial run /
    # manual import).
    main_papers = read_json(data_dir(workspace) / "papers.json", []) or []
    seed_rows = []
    seeds_path = existing_anchors_plan_path(workspace)
    if seeds_path.exists():
        seed_rows = iter_anchor_rows(workspace)
    anchors = list(main_papers) + seed_rows
    anchor_stats = build_anchor_stats(anchors)

    # ─── embedding channel (one-shot, all candidates at once) ────────────
    emb_scores = score_candidates_against_topic(candidates, keyword_text)

    # ─── brain channel (batched) + metadata channel (per-paper) ──────────
    decisions_path = queue_path.with_name(
        queue_path.stem.replace("weak_candidates_", "score_decisions_") + ".jsonl"
    )
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    # Partial cache lets the stage resume from the last completed batch when
    # a long brain run is interrupted (codex / opencode mid-stage death,
    # SIGTERM from a wrapping task scheduler, etc.). Stable workspace-level
    # path so a re-run finds the cache even though build_weak_candidate_review
    # generates a fresh queue file each call. candidate_key is stable across
    # runs, so cached scores keyed by it remain valid.
    partial_path = workspace / ".papercompass" / "auto" / "score_papers_partial.jsonl"
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    if decisions_path.exists() and decisions_path.stat().st_size > 0:
        archive_dir = decisions_path.parent / "archived"
        archive_dir.mkdir(exist_ok=True)
        archive_target = archive_dir / (
            decisions_path.stem
            + f".pre_{_dt.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        )
        decisions_path.rename(archive_target)
        # The aggregated file is the authoritative completion marker; archiving
        # it here means we want a clean re-aggregation. Drop any partial cache
        # too — it was tied to that completion.
        if partial_path.exists():
            partial_path.unlink()

    brain_scores: dict[str, int] = {}
    brain_reasons: dict[str, str] = {}
    brain_roles: dict[str, str] = {}
    brain_errors = 0
    batches_done = 0
    if partial_path.exists():
        loaded = 0
        for line in partial_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (row.get("candidate_key") or "").strip()
            if not key:
                continue
            brain_scores[key] = row.get("score")
            brain_reasons[key] = row.get("reason") or ""
            brain_roles[key] = normalize_role(row.get("paper_role"), default=CORE_METHOD)
            loaded += 1
        if loaded:
            state._log_progress(
                f"[score_papers] resumed from {partial_path.name}: "
                f"{loaded} cached brain scores"
            )

    # Embedding-based pre-rank: brain only sees the top-K by emb similarity.
    # The remaining candidates get fused on meta + brain (which may be None
    # for the tail). When embedding is unavailable (sentence-transformers
    # missing or empty target), score_candidates_against_topic returns
    # all-None — fall back to arrival order for the brain budget rather
    # than sorting by None.
    rerank_top_k = (
        (max_batches * batch_size)
        if max_batches is not None
        else len(candidates)
    )
    if any(s is not None for s in emb_scores):
        indices_by_emb = sorted(
            range(len(candidates)),
            key=lambda i: (emb_scores[i] if i < len(emb_scores) and emb_scores[i] is not None else -1.0),
            reverse=True,
        )
        protected = [
            i for i, c in enumerate(candidates)
            if isinstance(c, dict)
            and (c.get("candidate_key") or "").strip() in required_seed_roles_by_key
        ]
        protected_set = set(protected)
        top_k_indices = (protected + [i for i in indices_by_emb if i not in protected_set])[:rerank_top_k]
    else:
        protected = [
            i for i, c in enumerate(candidates)
            if isinstance(c, dict)
            and (c.get("candidate_key") or "").strip() in required_seed_roles_by_key
        ]
        protected_set = set(protected)
        top_k_indices = (
            protected
            + [i for i in range(len(candidates)) if i not in protected_set]
        )[: min(rerank_top_k, len(candidates))]
    top_k_candidates = [candidates[i] for i in top_k_indices]

    for batch_idx, batch in enumerate(_chunked(top_k_candidates, batch_size)):
        if max_batches is not None and batch_idx >= max_batches:
            break
        batches_done = batch_idx + 1
        # Skip batches whose every key is already present in the partial cache.
        # Keys with empty candidate_key never qualify for caching; if any key
        # in the batch is missing, the brain has to re-score the WHOLE batch
        # (we don't try to interleave per-key, prompt design is per-batch).
        batch_keys = [
            (c.get("candidate_key") or "").strip()
            for c in batch if isinstance(c, dict)
        ]
        non_empty_keys = [k for k in batch_keys if k]
        if non_empty_keys and all(k in brain_scores for k in non_empty_keys):
            state._log_progress(
                f"[score_papers] batch {batch_idx+1} skipped (cached, "
                f"{len(non_empty_keys)} keys already scored)"
            )
            continue
        compact = _compact_for_review(batch)
        result = score_candidate_batch(
            brain,
            compact,
            direction_raw=direction_raw,
            judge_examples=judge_examples,
            publication_scope=publication_scope_text,
            timeout=900,
        )
        log_brain_call(
            workspace,
            stage="score_papers",
            plugin=result["plugin"],
            prompt_tokens=0,
            response_text=result["raw_text"],
            parsed_ok=bool(result["scores"]),
            duration_seconds=result["duration"],
            extra={"batch": batch_idx, "batch_size": len(batch), "error": (result.get("error") or "")[:300], **(result.get("usage") or {})},
        )
        # Flush a stderr line per batch so that wrapping task / log
        # tail-followers see continuous progress and don't idle-kill the
        # process (codex/opencode CLI subprocesses can block stdout for
        # tens of seconds while the model thinks).
        state._log_progress(
            f"[score_papers] batch {batch_idx+1}/{batches_done} brain done "
            f"in {result['duration']:.1f}s, scores={len(result['scores'])}"
        )
        if result.get("error"):
            brain_errors += 1
        batch_score_rows = list(result["scores"])
        missing_for_retry = _missing_score_candidates(
            batch,
            existing_keys=set(brain_scores),
            returned_keys=_score_key_set(batch_score_rows),
        )
        if missing_for_retry and not result.get("error"):
            retry_result = score_candidate_batch(
                brain,
                _compact_for_review(missing_for_retry),
                direction_raw=direction_raw,
                judge_examples=judge_examples,
                publication_scope=publication_scope_text,
                timeout=900,
            )
            log_brain_call(
                workspace,
                stage="score_papers",
                plugin=retry_result["plugin"],
                prompt_tokens=0,
                response_text=retry_result["raw_text"],
                parsed_ok=bool(retry_result["scores"]),
                duration_seconds=retry_result["duration"],
                extra={
                    "batch": batch_idx,
                    "batch_size": len(missing_for_retry),
                    "retry_missing": True,
                    "error": (retry_result.get("error") or "")[:300],
                    **(retry_result.get("usage") or {}),
                },
            )
            state._log_progress(
                f"[score_papers] batch {batch_idx+1}/{batches_done} retry "
                f"for {len(missing_for_retry)} missing keys done in "
                f"{retry_result['duration']:.1f}s, scores={len(retry_result['scores'])}"
            )
            if retry_result.get("error"):
                brain_errors += 1
            batch_score_rows.extend(retry_result["scores"])
        # Append THIS batch's scores to the partial cache and update the
        # in-memory dicts atomically per batch. fsync after the write so a
        # SIGKILL doesn't lose the batch we just paid the brain for.
        with partial_path.open("a", encoding="utf-8") as cache_f:
            for row in batch_score_rows:
                key = (row.get("candidate_key") or "").strip()
                if not key:
                    continue
                brain_scores[key] = row["score"]
                brain_reasons[key] = row.get("reason") or ""
                brain_roles[key] = normalize_role(row.get("paper_role"), default=CORE_METHOD)
                cache_f.write(
                    json.dumps(
                        {"candidate_key": key,
                         "score": row["score"],
                         "paper_role": brain_roles[key],
                         "reason": row.get("reason") or ""},
                        ensure_ascii=False,
                    ) + "\n"
                )
            cache_f.flush()
            try:
                os.fsync(cache_f.fileno())
            except OSError:
                pass

    # Aggregate per-candidate scores into a decisions file
    counts = {"in_scope": 0, "boundary": 0, "out_of_scope": 0}
    boundary_rows: list[dict[str, Any]] = []
    with decisions_path.open("w", encoding="utf-8") as handle:
        for idx, cand in enumerate(candidates):
            if not isinstance(cand, dict):
                continue
            key = (cand.get("candidate_key") or "").strip()
            if not key:
                continue
            emb = emb_scores[idx] if idx < len(emb_scores) else None
            brain_val = brain_scores.get(key)
            required_seed_role = required_seed_roles_by_key.get(key, "")
            paper_role = required_seed_role or brain_roles.get(key) or normalize_role(cand.get("paper_role"), default=CORE_METHOD)
            meta = metadata_score(cand, anchor_stats)
            fused = fuse_and_verdict_with_policy(emb, brain_val, meta, weights, thresholds)
            row = {
                "candidate_key": key,
                "title": cand.get("title", ""),
                "year": cand.get("year"),
                "score": fused["score"],
                "verdict": fused["verdict"],
                "paper_role": paper_role,
                "required_seed_role": required_seed_role,
                "policy": fused.get("policy", "fused"),
                "emb_score": emb,
                "brain_score": brain_val,
                "meta_score": meta,
                "reason": brain_reasons.get(key, ""),
            }
            counts[fused["verdict"]] = counts.get(fused["verdict"], 0) + 1
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            if fused["verdict"] == "boundary":
                boundary_rows.append(row)

    review_decisions_path = decisions_path.with_name(
        decisions_path.stem.replace("score_decisions_", "review_decisions_") + ".jsonl"
    )
    with review_decisions_path.open("w", encoding="utf-8") as handle:
        for line in decisions_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            verdict = r["verdict"]
            required_seed_role = (r.get("required_seed_role") or "").strip()
            if required_seed_role == BACKGROUND_ANCHOR:
                decision, action = "anchor", "required_seed_anchor"
            elif required_seed_role:
                decision, action = "accept", "required_seed_main"
            else:
                decision, action = _review_decision_for_score(
                    verdict,
                    r.get("paper_role") or CORE_METHOD,
                    r.get("brain_score"),
                    float(r.get("score") or 0.0),
                )
            handle.write(
                json.dumps(
                    {
                        "candidate_key": r["candidate_key"],
                        "title": r["title"],
                        "year": r["year"],
                        "decision": decision,
                        "paper_role": r.get("paper_role") or CORE_METHOD,
                        "reason": (
                            f"required_seed_role={required_seed_role}"
                            if required_seed_role
                            else r["reason"] or f"score={r['score']} (emb={r['emb_score']} brain={r['brain_score']} meta={r['meta_score']})"
                        ),
                        "action": action,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    validation = validate_review_decisions(queue_path, review_decisions_path)
    apply_result: dict[str, Any] = {"appended": 0, "skipped_invalid": True}
    if validation.get("valid"):
        apply_result = apply_review_decisions(
            workspace,
            review_decisions_path,
            queue_path=queue_path,
        )

    # decisions_path is now the authoritative file; partial cache becomes
    # redundant. Keep it only on a brain_errors-heavy run so the user can
    # inspect where the mid-stage failure happened.
    if partial_path.exists() and brain_errors == 0:
        partial_path.unlink()

    reviewed_score_keys = _candidate_key_set(top_k_candidates[: batches_done * batch_size])
    brain_missing_scores = len([k for k in reviewed_score_keys if k not in brain_scores])
    truncated = (max_batches is not None) and (
        batches_done * batch_size < len(candidates)
    )
    uncovered_capped = max(0, len(candidates) - batches_done * batch_size) if truncated else 0
    state.end_stage(
        "score_papers",
        status="completed",
        queue=str(queue_path),
        decisions=str(decisions_path),
        review_decisions=str(review_decisions_path),
        counts=counts,
        valid=bool(validation.get("valid")),
        applied=apply_result.get("appended", 0),
        batches=batches_done,
        brain_errors=brain_errors,
        brain_missing_scores=brain_missing_scores,
        truncated=truncated,
        truncation_reason="max_batches_reached" if truncated else None,
        uncovered_capped=uncovered_capped,
    )
    return {
        "status": "completed",
        "decisions": str(decisions_path),
        "counts": counts,
        "boundary_count": len(boundary_rows),
        "applied": apply_result,
        "truncated": truncated,
        "uncovered_capped": uncovered_capped,
        "brain_missing_scores": brain_missing_scores,
    }


def stage_resolve_boundary(
    workspace: Path,
    *,
    brain: BrainPlugin,
    state: AutoState,
    second_brain: BrainPlugin | None = None,
    batch_size: int = 25,
    max_batches: int | None = 4,
    thresholds: BoundaryThresholds | None = None,
) -> dict[str, Any]:
    """Force every boundary paper to in_scope or out_of_scope using a second
    brain pass and metadata fallback. Replaces the old "user审 boundary"
    expectation — the system never punts ambiguity to the user.

    Strategy:
    1. Re-score boundary papers with `second_brain` (or `brain` if no
       second brain available) using the same prompt + anchors. A score
       ≥ brain_promote flips to in_scope; mid-brain + meta ≥
       meta_promote_floor also promotes; otherwise drop.
    2. No-brain papers (truncated by max_batches twice) drop to
       out_of_scope — meta alone is too noisy to anchor precision.

    Writes a follow-up decisions file and applies it.
    """
    if state.stage_done("resolve_boundary"):
        return {"status": "cached"}
    state.begin_stage("resolve_boundary")

    pending_path = data_dir(workspace) / "pending_review_candidates.json"
    pending = read_json(pending_path, [])
    if not isinstance(pending, list):
        state.end_stage("resolve_boundary", status="skipped", note="no pending list")
        return {"status": "skipped"}

    # Identify boundary papers from the latest score_decisions file
    review_dir = workspace / ".papercompass" / "reviews"
    score_files = sorted(review_dir.glob("score_decisions_*.jsonl"), reverse=True)
    if not score_files:
        state.end_stage("resolve_boundary", status="skipped", note="no score_decisions found")
        return {"status": "skipped"}
    score_lines = [json.loads(l) for l in score_files[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    boundary = [r for r in score_lines if r.get("verdict") == "boundary"]
    if not boundary:
        state.end_stage("resolve_boundary", status="skipped", note="no boundary papers")
        return {"status": "skipped", "boundary_count": 0}

    pending_by_key = {
        (p.get("candidate_key") or ""): p
        for p in pending
        if isinstance(p, dict)
    }
    seed_role_index = _required_seed_role_index(workspace)

    # Anchor stats from current main lib + seeds (post-score_papers state)
    main_papers = read_json(data_dir(workspace) / "papers.json", []) or []
    seed_rows = []
    seeds_path = existing_anchors_plan_path(workspace)
    if seeds_path.exists():
        seed_rows = iter_anchor_rows(workspace)
    anchor_stats = build_anchor_stats(list(main_papers) + seed_rows)

    topic = load_topic_config(workspace)
    direction_raw = topic.get("direction_raw") or topic.get("name") or topic.get("topic_id") or ""
    judge_examples = topic.get("judge_examples") or {"in_scope": [], "out_of_scope": []}
    publication_scope_text = render_publication_scope(publication_scope_from_topic(topic))

    judge_brain = second_brain or brain

    # Assemble boundary candidates with full info from pending_by_key
    boundary_candidates = []
    for r in boundary:
        key = r["candidate_key"]
        cand = pending_by_key.get(key)
        if not cand:
            continue
        boundary_candidates.append(cand)

    second_scores: dict[str, int] = {}
    batches_done = 0
    brain_errors = 0
    # Resume cache for boundary scoring — stable workspace-level path keyed
    # off candidate_key so a queue regeneration doesn't orphan completed
    # scores.
    resolve_partial_path = workspace / ".papercompass" / "auto" / "boundary_partial.jsonl"
    resolve_partial_path.parent.mkdir(parents=True, exist_ok=True)
    if resolve_partial_path.exists():
        loaded = 0
        for line in resolve_partial_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (row.get("candidate_key") or "").strip()
            if key:
                second_scores[key] = row.get("score")
                loaded += 1
        if loaded:
            state._log_progress(
                f"[resolve_boundary] resumed from {resolve_partial_path.name}: "
                f"{loaded} cached second-brain scores"
            )
    for batch_idx, batch in enumerate(_chunked(boundary_candidates, batch_size)):
        if max_batches is not None and batch_idx >= max_batches:
            break
        batches_done = batch_idx + 1
        batch_keys = [
            (c.get("candidate_key") or "").strip()
            for c in batch if isinstance(c, dict)
        ]
        non_empty_keys = [k for k in batch_keys if k]
        if non_empty_keys and all(k in second_scores for k in non_empty_keys):
            state._log_progress(
                f"[resolve_boundary] batch {batch_idx+1} skipped (cached, "
                f"{len(non_empty_keys)} keys already scored)"
            )
            continue
        compact = _compact_for_review(batch)
        result = score_candidate_batch(
            judge_brain,
            compact,
            direction_raw=direction_raw,
            judge_examples=judge_examples,
            publication_scope=publication_scope_text,
            timeout=900,
        )
        log_brain_call(
            workspace,
            stage="resolve_boundary",
            plugin=result["plugin"],
            prompt_tokens=0,
            response_text=result["raw_text"],
            parsed_ok=bool(result["scores"]),
            duration_seconds=result["duration"],
            extra={"batch": batch_idx, "batch_size": len(batch), "error": (result.get("error") or "")[:300], **(result.get("usage") or {})},
        )
        state._log_progress(
            f"[resolve_boundary] batch {batch_idx+1}/{batches_done} brain done "
            f"in {result['duration']:.1f}s, scores={len(result['scores'])}"
        )
        if result.get("error"):
            brain_errors += 1
        batch_score_rows = list(result["scores"])
        missing_for_retry = _missing_score_candidates(
            batch,
            existing_keys=set(second_scores),
            returned_keys=_score_key_set(batch_score_rows),
        )
        if missing_for_retry and not result.get("error"):
            retry_result = score_candidate_batch(
                judge_brain,
                _compact_for_review(missing_for_retry),
                direction_raw=direction_raw,
                judge_examples=judge_examples,
                publication_scope=publication_scope_text,
                timeout=900,
            )
            log_brain_call(
                workspace,
                stage="resolve_boundary",
                plugin=retry_result["plugin"],
                prompt_tokens=0,
                response_text=retry_result["raw_text"],
                parsed_ok=bool(retry_result["scores"]),
                duration_seconds=retry_result["duration"],
                extra={
                    "batch": batch_idx,
                    "batch_size": len(missing_for_retry),
                    "retry_missing": True,
                    "error": (retry_result.get("error") or "")[:300],
                    **(retry_result.get("usage") or {}),
                },
            )
            state._log_progress(
                f"[resolve_boundary] batch {batch_idx+1}/{batches_done} retry "
                f"for {len(missing_for_retry)} missing keys done in "
                f"{retry_result['duration']:.1f}s, scores={len(retry_result['scores'])}"
            )
            if retry_result.get("error"):
                brain_errors += 1
            batch_score_rows.extend(retry_result["scores"])
        with resolve_partial_path.open("a", encoding="utf-8") as cache_f:
            for row in batch_score_rows:
                key = (row.get("candidate_key") or "").strip()
                if not key:
                    continue
                second_scores[key] = row["score"]
                cache_f.write(
                    json.dumps(
                        {"candidate_key": key, "score": row["score"]},
                        ensure_ascii=False,
                    ) + "\n"
                )
            cache_f.flush()
            try:
                os.fsync(cache_f.fileno())
            except OSError:
                pass

    # Compute final verdicts for boundary papers
    boundary_resolution: dict[str, str] = {}  # candidate_key → "in_scope" / "out_of_scope"
    boundary_meta: dict[str, dict] = {}
    final_counts = {"in_scope": 0, "out_of_scope": 0}
    decisions_path = score_files[0].with_name(
        score_files[0].stem.replace("score_decisions_", "boundary_resolution_") + ".jsonl"
    )
    review_decisions_path = score_files[0].with_name(
        score_files[0].stem.replace("score_decisions_", "review_decisions_resolved_") + ".jsonl"
    )
    with decisions_path.open("w", encoding="utf-8") as f_dec:
        for r in boundary:
            key = r["candidate_key"]
            second = second_scores.get(key)
            cand = pending_by_key.get(key, {})
            meta_val = metadata_score(cand, anchor_stats) if cand else 0.0
            verdict = resolve_boundary(r, second, meta_val, thresholds)
            boundary_resolution[key] = verdict
            boundary_meta[key] = {"second_brain_score": second, "meta_score": meta_val}
            final_counts[verdict] += 1
            f_dec.write(
                json.dumps(
                    {
                        "candidate_key": key,
                        "title": r.get("title", ""),
                        "year": r.get("year"),
                        "first_score": r.get("score"),
                        "second_brain_score": second,
                        "meta_score": meta_val,
                        "final_verdict": verdict,
                        "paper_role": r.get("paper_role") or CORE_METHOD,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    # Write a full-coverage review decisions file that mirrors the original
    # score_decisions but flips boundary papers to their resolved verdict.
    with review_decisions_path.open("w", encoding="utf-8") as f_leg:
        for r in score_lines:
            key = r["candidate_key"]
            required_seed_role = _required_seed_role_for_review_row(
                r,
                pending_by_key,
                seed_role_index,
            )
            if r["verdict"] == "boundary":
                final_v = boundary_resolution.get(key, "out_of_scope")
                meta = boundary_meta.get(key, {})
                reason = (
                    f"boundary_resolved second_brain={meta.get('second_brain_score')} "
                    f"meta={meta.get('meta_score')}"
                )
            else:
                final_v = r["verdict"]
                reason = r.get("reason") or f"score_papers verdict={final_v}"
            paper_role = required_seed_role or r.get("paper_role") or CORE_METHOD
            if required_seed_role == BACKGROUND_ANCHOR:
                decision, action = "anchor", "required_seed_anchor"
                reason = f"required_seed_role={required_seed_role}"
            elif required_seed_role:
                decision, action = "accept", "required_seed_main"
                reason = f"required_seed_role={required_seed_role}"
            else:
                decision, action = _review_decision_for_score(
                    final_v,
                    paper_role,
                    r.get("brain_score"),
                    float(r.get("score") or 0.0),
                )
            f_leg.write(
                json.dumps(
                    {
                        "candidate_key": key,
                        "title": r.get("title", ""),
                        "year": r.get("year"),
                        "decision": decision,
                        "paper_role": paper_role,
                        "reason": reason,
                        "action": action,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    # Apply the resolution decisions: this needs a queue file — the original
    # weak_candidates_*.json from score_papers stage.
    apply_result: dict[str, Any] = {"appended": 0, "skipped_invalid": True}
    weak_queue_files = sorted(review_dir.glob("weak_candidates_*.json"), reverse=True)
    if weak_queue_files:
        validation = validate_review_decisions(weak_queue_files[0], review_decisions_path)
        if validation.get("valid"):
            apply_result = apply_review_decisions(
                workspace, review_decisions_path, queue_path=weak_queue_files[0]
            )

    if resolve_partial_path.exists() and brain_errors == 0:
        resolve_partial_path.unlink()

    reviewed_boundary_keys = _candidate_key_set(
        boundary_candidates[: batches_done * batch_size]
    )
    brain_missing_scores = len([k for k in reviewed_boundary_keys if k not in second_scores])
    truncated = (max_batches is not None) and (
        batches_done * batch_size < len(boundary_candidates)
    )
    uncovered_capped = max(0, len(boundary_candidates) - batches_done * batch_size) if truncated else 0
    state.end_stage(
        "resolve_boundary",
        status="completed",
        boundary_total=len(boundary),
        final_counts=final_counts,
        batches=batches_done,
        brain_errors=brain_errors,
        brain_missing_scores=brain_missing_scores,
        decisions=str(decisions_path),
        review_decisions=str(review_decisions_path),
        truncated=truncated,
        truncation_reason="max_batches_reached" if truncated else None,
        uncovered_capped=uncovered_capped,
    )
    return {
        "status": "completed",
        "boundary_total": len(boundary),
        "final_counts": final_counts,
        "decisions": str(decisions_path),
        "truncated": truncated,
        "uncovered_capped": uncovered_capped,
        "brain_missing_scores": brain_missing_scores,
    }


def stage_catalog(workspace: Path, *, state: AutoState) -> dict[str, Any]:
    state.begin_stage("catalog")
    res = build_catalog(workspace)
    state.end_stage("catalog", status="completed", paper_count=res.get("paper_count"))
    return res

"""End-to-end auto-build orchestrator.

Public entry: `run_auto_build(workspace, direction, ...)`. A user (or another
agent CLI) calls this with a one-sentence research direction; the orchestrator
delegates the semantic decisions to a brain plugin (codex / gemini / claude)
and runs the deterministic discovery / build / catalog / qa pipeline around
those decisions.

Design constraints (from the implicit-cot postmortem):

- topic.yaml ↔ sources.yaml alignment is enforced in code (every strong term
  becomes a source query). The brain only contributes terms.
- Source-backed anchor recall is checked after the first discover; missing
  anchors with direct IDs can be patched deterministically.
- Weak queue size has a triage threshold. Above it, the brain proposes rule
  tightening before per-candidate review.
- Defer ratio is bounded; defer-heavy responses don't count as "review done".
- Brain never touches .raw/. All semantic outcomes flow through topic.yaml,
  applied_decisions.jsonl or add-paper.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..anchors import anchors_plan_path
from ..build import record_agent_run_step
from ..config import (
    data_dir,
    ensure_workspace_dirs,
    load_topic_config,
    portable_workspace_data,
    workspace_label,
    workspace_relative_path,
)
from ..plugins import BrainPlugin, select_brain
from ..text import read_json
from ..workspace_contract import workspace_contract_summary
from .fusion import FusionThresholds, FusionWeights
from .stages import (
    stage_build,
    stage_catalog,
    stage_discover,
    stage_plan_direction,
    stage_qa,
    stage_resolve_boundary,
    stage_review_queue_gate,
    stage_score_papers,
    stage_seed_check,
    stage_seed_repair,
)
from .state import AutoState


# Bump when a change to discovery, filter, fusion, or build logic invalidates
# previously generated artifacts in older workspaces. Increments force any
# workspace from a prior schema to require --fresh.
WORKSPACE_FINGERPRINT_VERSION = 1


def _workspace_fingerprint(
    direction: str,
    min_year: int | None,
    sources: list[str] | None,
) -> dict[str, Any]:
    """Inputs that, if changed, invalidate the workspace's fetched/scored
    state. Brain identity is intentionally NOT part of the fingerprint —
    re-scoring with a different brain on the same fetched data is a
    legitimate workflow.
    """
    return {
        "schema_version": WORKSPACE_FINGERPRINT_VERSION,
        "direction": (direction or "").strip(),
        "min_year": min_year,
        "sources": sorted(sources) if sources else None,
    }


_GENERATED_PATHS = (
    "data",
    ".raw",
    "catalog",
    ".papercompass",
    "topic.yaml",
    "sources.yaml",
)


def _wipe_workspace_artifacts(workspace: Path) -> list[str]:
    """Remove generated build artifacts. Returns relative paths actually
    removed so the caller can log the wipe.
    """
    removed: list[str] = []
    for rel in _GENERATED_PATHS:
        path = workspace / rel
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(rel)
    return removed


def _fingerprint_path(workspace: Path) -> Path:
    return workspace / ".papercompass" / "auto" / "workspace_fingerprint.json"


def _check_workspace_fingerprint(
    workspace: Path,
    fp: dict[str, Any],
    fresh: bool,
) -> None:
    """Reject reuse of a workspace whose previous build inputs differ from
    the current run, unless `fresh=True` is set (in which case all generated
    artifacts are wiped first).

    The check addresses a previously-silent contamination path: applied_
    decisions.jsonl and .raw/ caches are append-only / cache-keyed, so a
    second auto-build with a different direction on the same workspace
    would mix old papers and old verdicts into the new library.
    """
    fp_path = _fingerprint_path(workspace)
    has_artifacts = any((workspace / rel).exists() for rel in _GENERATED_PATHS)
    if fresh:
        removed = _wipe_workspace_artifacts(workspace)
        if removed:
            print(f"papercompass auto: --fresh wiped {removed}")
        fp_path.parent.mkdir(parents=True, exist_ok=True)
        fp_path.write_text(
            json.dumps(fp, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return
    if fp_path.exists():
        existing = json.loads(fp_path.read_text(encoding="utf-8"))
        if existing == fp:
            return
        diffs = {
            k: {"prev": existing.get(k), "now": fp.get(k)}
            for k in set(existing) | set(fp)
            if existing.get(k) != fp.get(k)
        }
        raise RuntimeError(
            f"workspace {workspace} was previously built with a different "
            f"configuration: {diffs}. Pass --fresh to wipe and rebuild, or "
            "use a different --workspace."
        )
    if has_artifacts:
        raise RuntimeError(
            f"workspace {workspace} has generated artifacts but no "
            "workspace_fingerprint.json. Pass --fresh to "
            "wipe and rebuild, or use a different --workspace."
        )
    fp_path.parent.mkdir(parents=True, exist_ok=True)
    fp_path.write_text(
        json.dumps(fp, ensure_ascii=False, indent=2), encoding="utf-8"
    )


HARD_DELIVERY_WARNINGS = {
    "score_papers_decisions_missing",
    "score_papers_has_deferred",
    "no_brain_boundary_scores",
    "core_seed_coverage_missing",
    "anchor_seed_coverage_missing",
    "core_seed_misplaced",
    "anchor_seed_misplaced",
    "seed_coverage_missing",
    "query_terms_uncovered",
    "source_coverage_has_risk",
    "source_coverage_partial",
    "recall_pool_underpowered",
    "weak_batches_over_budget",
    "weak_queue_too_large",
    "generic_anchor_dominates",
}

HARD_ENVIRONMENT_WARNINGS = {
    "embedding_required_missing",
    "embedding_missing_with_capped_brain_budget",
}

RULE_REPAIR_WARNINGS = {
    "no_brain_boundary_scores",
    "core_seed_misplaced",
    "anchor_seed_misplaced",
    "weak_queue_too_large",
    "generic_anchor_dominates",
}

SEED_OR_QUERY_REPAIR_WARNINGS = {
    "core_seed_coverage_missing",
    "anchor_seed_coverage_missing",
    "seed_coverage_missing",
    "query_terms_uncovered",
}

BUDGET_WARNINGS = {
    "score_papers_decisions_missing",
    "score_papers_has_deferred",
    "recall_pool_underpowered",
    "weak_batches_over_budget",
}

SOURCE_RETRY_WARNINGS = {
    "source_coverage_has_risk",
    "source_coverage_partial",
}

EMBED_INSTALL_HINT = "uv sync --extra embed"


def _delivery_assessment(
    qa_report: dict[str, Any],
    truncations: list[dict[str, Any]],
    environment_warnings: list[str] | None = None,
    brain_missing_scores: list[dict[str, Any]] | None = None,
    extra_hard_reasons: list[str] | None = None,
    forced_status: str | None = None,
    forced_exit_code: int | None = None,
) -> dict[str, Any]:
    """Map diagnostic QA into a downstream handoff decision.

    QA warnings are useful for humans, but automated consumers need a hard
    answer: is the catalog authoritative enough for default retrieval?
    """
    critical = list(qa_report.get("critical") or [])
    warnings = list(qa_report.get("warnings") or [])
    hard_warnings = [w for w in warnings if w in HARD_DELIVERY_WARNINGS]
    environment_warnings = list(environment_warnings or [])
    hard_environment_warnings = [
        w for w in environment_warnings if w in HARD_ENVIRONMENT_WARNINGS
    ]
    truncation_reasons = [
        f"{t.get('stage')}:{t.get('uncovered', 0)}"
        for t in truncations
        if int(t.get("uncovered") or 0) > 0
    ]
    brain_missing_reasons = [
        f"{item.get('stage')}:{item.get('missing', 0)}"
        for item in (brain_missing_scores or [])
        if int(item.get("missing") or 0) > 0
    ]
    reasons = (
        [f"critical:{item}" for item in critical]
        + [f"warning:{item}" for item in hard_warnings]
        + [f"environment:{item}" for item in hard_environment_warnings]
        + [f"truncation:{item}" for item in truncation_reasons]
        + [f"brain_missing_scores:{item}" for item in brain_missing_reasons]
        + [f"gate:{item}" for item in (extra_hard_reasons or [])]
    )
    if critical:
        status = "failed"
        exit_code = 2
    elif forced_status:
        status = forced_status
        exit_code = forced_exit_code if forced_exit_code is not None else 5
    elif truncation_reasons:
        status = "partial_due_to_budget"
        exit_code = 4
    elif hard_warnings:
        hard_set = set(hard_warnings)
        if hard_set & RULE_REPAIR_WARNINGS:
            status = "needs_rule_repair"
            exit_code = 5
        elif hard_set & SEED_OR_QUERY_REPAIR_WARNINGS:
            status = "needs_seed_or_query_repair"
            exit_code = 3
        elif hard_set & SOURCE_RETRY_WARNINGS:
            status = "needs_source_retry"
            exit_code = 4
        elif hard_set & BUDGET_WARNINGS:
            status = "partial_due_to_budget"
            exit_code = 4
        else:
            status = "usable_with_caveats"
            exit_code = 3
    elif brain_missing_reasons:
        status = "needs_brain_score_repair"
        exit_code = 4
    elif hard_environment_warnings:
        if "embedding_required_missing" in hard_environment_warnings:
            status = "missing_required_embedding"
        else:
            status = "partial_due_to_budget"
        exit_code = 4
    elif qa_report.get("status") == "passed":
        status = "passed_authoritative"
        exit_code = 0
    elif qa_report.get("status") == "warning":
        status = "usable_with_caveats"
        exit_code = 0
    else:
        status = "usable_with_caveats"
        exit_code = 3
    return {
        "deliverable_status": status,
        "safe_for_default_llm_retrieval": status == "passed_authoritative",
        "exit_code": exit_code,
        "hard_reasons": reasons,
        "hard_warnings": hard_warnings,
        "hard_environment_warnings": hard_environment_warnings,
    }


@dataclass
class AutoBuildResult:
    workspace: Path
    direction: str
    brain: str
    topic_id: str
    final_status: str
    papers: int
    pending: int
    rejected: int
    iterations: int
    seed_total: int
    seed_missing: int
    qa_status: str
    qa_critical: list[str]
    qa_warnings: list[str]
    delivery_status: str = ""
    safe_for_default_llm_retrieval: bool = False
    exit_code: int = 0
    artifacts: dict[str, str] = field(default_factory=dict)
    channels_active: dict[str, bool] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    environment_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "direction": self.direction,
            "brain": self.brain,
            "topic_id": self.topic_id,
            "final_status": self.final_status,
            "papers": self.papers,
            "pending": self.pending,
            "rejected": self.rejected,
            "iterations": self.iterations,
            "seed_total": self.seed_total,
            "seed_missing": self.seed_missing,
            "qa_status": self.qa_status,
            "qa_critical": self.qa_critical,
            "qa_warnings": self.qa_warnings,
            "environment_warnings": self.environment_warnings,
            "delivery_status": self.delivery_status or self.final_status,
            "safe_for_default_llm_retrieval": self.safe_for_default_llm_retrieval,
            "exit_code": self.exit_code,
            "channels_active": self.channels_active,
            "environment": self.environment,
            "artifacts": self.artifacts,
        }


def _with_added_warnings(qa_report: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    report = dict(qa_report)
    merged = list(report.get("warnings") or [])
    for warning in warnings:
        if warning and warning not in merged:
            merged.append(warning)
    report["warnings"] = merged
    if merged and report.get("status") == "passed":
        report["status"] = "warning"
    return report


def _write_queue_gate_summary(
    *,
    workspace: Path,
    direction: str,
    brain_plugin: BrainPlugin,
    second_brain_plugin: BrainPlugin | None,
    topic: dict[str, Any],
    state: AutoState,
    qa_report: dict[str, Any],
    queue_diagnosis: dict[str, Any],
    embedding_available: bool,
    environment_warnings: list[str],
    weak_batch_size: int,
    weak_max_batches: int | None,
    boundary_max_batches: int | None,
    seed_total: int,
    allow_no_embedding: bool = False,
) -> AutoBuildResult:
    reason_codes = list(queue_diagnosis.get("reason_codes") or [])
    qa_for_delivery = _with_added_warnings(qa_report, reason_codes)
    counts = qa_for_delivery.get("counts") or {}
    seed_check_final = qa_for_delivery.get("seed_coverage") or {}
    seed_missing = int(seed_check_final.get("missing_count") or 0)
    status = str(queue_diagnosis.get("status") or "needs_rule_repair")
    exit_code = 5 if status == "needs_rule_repair" else 4
    delivery = _delivery_assessment(
        qa_for_delivery,
        [],
        environment_warnings,
        [],
        extra_hard_reasons=reason_codes,
        forced_status=status,
        forced_exit_code=exit_code,
    )
    effective_batches = int(queue_diagnosis.get("effective_batches") or 0)
    effective_boundary_batches = (
        boundary_max_batches if boundary_max_batches is not None else effective_batches
    )
    environment = {
        "embedding_available": embedding_available,
        "embedding_required": not allow_no_embedding,
        "allow_no_embedding": allow_no_embedding,
        "warnings": environment_warnings,
        "install_hints": (
            {"embedding": EMBED_INSTALL_HINT}
            if not embedding_available
            else {}
        ),
        "weak_batch_size": weak_batch_size,
        "weak_batches_needed": queue_diagnosis.get("needed_batches"),
        "weak_batches_effective": effective_batches,
        "weak_max_batches": weak_max_batches,
        "boundary_max_batches": boundary_max_batches,
        "boundary_batches_effective": effective_boundary_batches,
    }
    artifacts = {
        "state": workspace_relative_path(workspace, state.path),
        "iterations_log": workspace_relative_path(
            workspace,
            workspace / ".papercompass" / "auto" / "iterations.jsonl",
        ),
        "qa_manifest": qa_for_delivery.get("manifest", ""),
        "qa_markdown": qa_for_delivery.get("markdown", ""),
        "main_library": workspace_relative_path(workspace, data_dir(workspace) / "papers.jsonl"),
        "pending": workspace_relative_path(
            workspace,
            data_dir(workspace) / "pending_review_candidates.json",
        ),
        "catalog_manifest": workspace_relative_path(workspace, workspace / "catalog" / "manifest.json"),
        "review_queue_diagnosis": queue_diagnosis.get("manifest", ""),
        "review_queue_diagnosis_markdown": queue_diagnosis.get("markdown", ""),
    }
    artifacts = portable_workspace_data(workspace, artifacts)
    summary_path = workspace / ".papercompass" / "auto" / "final_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": "papercompass.handoff.v1",
        "status": delivery["deliverable_status"],
        "deliverable_status": delivery["deliverable_status"],
        "safe_for_default_llm_retrieval": delivery["safe_for_default_llm_retrieval"],
        "exit_code": delivery["exit_code"],
        "workspace": workspace_label(workspace),
        "workspace_contract": workspace_contract_summary(workspace, topic),
        "direction": direction,
        "brain": brain_plugin.name,
        "topic_id": topic.get("topic_id"),
        "qa_status": qa_for_delivery.get("status"),
        "counts": counts,
        "quality": {
            "qa_status": qa_for_delivery.get("status"),
            "critical": qa_for_delivery.get("critical") or [],
            "warnings": list(qa_for_delivery.get("warnings") or []) + environment_warnings,
            "qa_warnings": qa_for_delivery.get("warnings") or [],
            "environment_warnings": environment_warnings,
            "hard_reasons": delivery["hard_reasons"],
        },
        "coverage": {
            "truncations": [],
            "brain_missing_scores": [],
            "brain_missing_score_count": 0,
            "source_risk_count": (qa_for_delivery.get("source_coverage") or {}).get("problem_count", 0),
            "unscored_count": queue_diagnosis.get("uncovered_count", 0),
            "unresolved_boundary_count": (qa_for_delivery.get("score_papers") or {}).get("deferred_count", 0),
            "recall_pool": qa_for_delivery.get("recall_pool") or {},
            "review_queue": queue_diagnosis,
        },
        "models": {
            "build_brain": brain_plugin.name,
            "second_brain": second_brain_plugin.name if second_brain_plugin else "",
            "cross_model": bool(second_brain_plugin and second_brain_plugin.name != brain_plugin.name),
        },
        "artifacts": artifacts,
        "channels_active": {"embedding": embedding_available, "brain": False, "metadata": False},
        "environment": environment,
        "seed_total": seed_total,
        "seed_missing": seed_missing,
        "iterations": 0,
        "truncations": [],
        "brain_usage": {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cost_usd": 0.0,
            "metadata": {
                "models": [],
                "model_sources": [],
                "reasoning_efforts": [],
                "reasoning_effort_sources": [],
            },
            "by_stage": {},
        },
    }
    summary = portable_workspace_data(workspace, summary)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    record_agent_run_step(
        workspace,
        phase="auto-build-review-queue-gate",
        status=delivery["deliverable_status"],
        summary=(
            f"review queue gate stopped before score_papers: "
            f"status={delivery['deliverable_status']}; pending={counts.get('pending')}; "
            f"reasons={','.join(reason_codes) or 'none'}"
        ),
        files=[
            "data/pending_review_candidates.json",
            ".papercompass/auto/final_summary.json",
            queue_diagnosis.get("manifest", ""),
        ],
    )
    return AutoBuildResult(
        workspace=workspace,
        direction=direction,
        brain=brain_plugin.name,
        topic_id=topic.get("topic_id") or "",
        final_status=delivery["deliverable_status"],
        papers=int(counts.get("papers") or 0),
        pending=int(counts.get("pending") or 0),
        rejected=int(counts.get("rejected") or 0),
        iterations=0,
        seed_total=int(seed_total or 0),
        seed_missing=seed_missing,
        qa_status=qa_for_delivery.get("status") or "unknown",
        qa_critical=qa_for_delivery.get("critical") or [],
        qa_warnings=qa_for_delivery.get("warnings") or [],
        delivery_status=delivery["deliverable_status"],
        safe_for_default_llm_retrieval=delivery["safe_for_default_llm_retrieval"],
        exit_code=int(delivery["exit_code"]),
        artifacts=artifacts,
        channels_active={"embedding": embedding_available, "brain": False, "metadata": False},
        environment=environment,
        environment_warnings=environment_warnings,
    )


def run_auto_build(
    workspace: Path,
    direction: str,
    *,
    brain: BrainPlugin | str | None = None,
    second_brain: BrainPlugin | str | None = None,
    min_year: int | None = None,
    max_remote_calls: int = 120,
    refresh: bool = False,
    sources: list[str] | None = None,
    weak_batch_size: int = 25,
    weak_max_batches: int | None = 20,
    boundary_max_batches: int | None = None,
    plan_only: bool = False,
    verbose: bool = False,
    fresh: bool = False,
    topic_id_override: str | None = None,
    allow_no_embedding: bool = False,
    prior_markdown: str | None = None,
    seed_cap: int | None = None,
) -> AutoBuildResult:
    if isinstance(brain, str) or brain is None:
        brain_plugin = select_brain(preference=brain)
    else:
        brain_plugin = brain
    if second_brain is None:
        env_second = os.environ.get("PAPERCOMPASS_SECOND_BRAIN", "").strip()
        second_brain = env_second or None

    if second_brain is None:
        second_brain_plugin = None
    elif isinstance(second_brain, str):
        second_brain_plugin = select_brain(preference=second_brain)
    else:
        second_brain_plugin = second_brain

    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    fp = _workspace_fingerprint(direction, min_year, sources)
    _check_workspace_fingerprint(workspace, fp, fresh=fresh)
    ensure_workspace_dirs(workspace)
    state = AutoState(workspace, verbose=verbose)
    state.set("direction", direction)
    state.set("brain", brain_plugin.name)
    state.set("min_year", min_year)

    # 1) plan
    plan_result = stage_plan_direction(
        workspace,
        direction,
        min_year=min_year,
        brain=brain_plugin,
        state=state,
        topic_id_override=topic_id_override,
        prior_markdown=prior_markdown,
        seed_cap=seed_cap,
    )
    topic = load_topic_config(workspace)

    if plan_only:
        return AutoBuildResult(
            workspace=workspace,
            direction=direction,
            brain=brain_plugin.name,
            topic_id=topic.get("topic_id") or "",
            final_status="plan_only",
            papers=0,
            pending=0,
            rejected=0,
            iterations=0,
            seed_total=plan_result.get("seed_count", 0) or 0,
            seed_missing=0,
            qa_status="not_run",
            qa_critical=[],
            qa_warnings=[],
            artifacts={
                "topic_yaml": workspace_relative_path(workspace, workspace / "topic.yaml"),
                "sources_yaml": workspace_relative_path(workspace, workspace / "sources.yaml"),
                "seeds": workspace_relative_path(
                    workspace,
                    anchors_plan_path(workspace),
                ),
                "plan_response": workspace_relative_path(
                    workspace,
                    workspace / ".papercompass" / "auto" / "plan_response.json",
                ),
                "state": workspace_relative_path(workspace, state.path),
            },
        )

    # Preflight channels. The embedding dependency remains optional at install
    # time so lightweight CLI commands work everywhere, but formal auto-build
    # handoff requires it by default. Users can pass --allow-no-embedding for
    # smoke / budget runs, and the handoff summary records that choice.
    from .embed import is_available as _emb_available

    embedding_available = _emb_available()
    environment_warnings: list[str] = []
    if not embedding_available:
        environment_warnings.append("embedding_channel_disabled")
        if not allow_no_embedding:
            environment_warnings.append("embedding_required_missing")
        state.event(
            "preflight_warning",
            warning="embedding_channel_disabled",
            install_hint=EMBED_INSTALL_HINT,
            embedding_required=not allow_no_embedding,
            allow_no_embedding=allow_no_embedding,
        )
        if not allow_no_embedding:
            state.event(
                "preflight_warning",
                warning="embedding_required_missing",
                install_hint=EMBED_INSTALL_HINT,
            )
        if verbose:
            state._log_progress(
                f"[preflight] embedding disabled; run `{EMBED_INSTALL_HINT}` "
                "to enable semantic reranking"
                + (" or pass --allow-no-embedding for a non-authoritative run" if not allow_no_embedding else "")
            )

    # 2) discover (round 1)
    stage_discover(
        workspace,
        min_year=min_year or topic.get("min_year"),
        max_remote_calls=max_remote_calls,
        sources=sources,
        refresh=refresh,
        state=state,
        iteration=1,
    )

    # 3) seed check
    seed_status = stage_seed_check(workspace, state=state)
    seed_total = seed_status.get("total", 0)
    if seed_status.get("status") == "checked" and seed_status.get("missing"):
        stage_seed_repair(workspace, seed_status["missing"], brain=brain_plugin, state=state)
        # re-run discover after repair to pick up new queries / added papers
        stage_discover(
            workspace,
            min_year=min_year or topic.get("min_year"),
            max_remote_calls=max(max_remote_calls // 2, 20),
            sources=sources,
            refresh=False,
            state=state,
            iteration=2,
        )
        seed_status = stage_seed_check(workspace, state=state)

    # 4) qa pass 1
    qa_pass1 = stage_qa(workspace, state=state, label="qa_pass1")

    # 5) Fusion scoring. Every pending candidate goes through three
    # independent channels:
    #   - embedding similarity (sentence-transformer)
    #   - LLM judge (brain pass with in/out anchors)
    #   - metadata score (multi-source / citation / venue / freshness)
    # The fused score yields in_scope / boundary / out_of_scope. boundary
    # papers are forced to in/out by stage_resolve_boundary using a second
    # brain pass + metadata fallback. No keyword Boolean rules. No user
    # ambiguity.
    iterations = 1
    pending_after = read_json(
        data_dir(workspace) / "pending_review_candidates.json", []
    )
    pending_count = len(pending_after) if isinstance(pending_after, list) else 0
    needed_batches = (pending_count + weak_batch_size - 1) // max(weak_batch_size, 1)
    effective_max_batches = (
        min(needed_batches, weak_max_batches)
        if weak_max_batches is not None
        else needed_batches
    )
    if (
        not embedding_available
        and weak_max_batches is not None
        and effective_max_batches < needed_batches
        and "embedding_missing_with_capped_brain_budget" not in environment_warnings
    ):
        environment_warnings.append("embedding_missing_with_capped_brain_budget")
        state.event(
            "preflight_warning",
            warning="embedding_missing_with_capped_brain_budget",
            install_hint=EMBED_INSTALL_HINT,
            pending_count=pending_count,
            needed_batches=needed_batches,
            effective_max_batches=effective_max_batches,
        )
    queue_gate = stage_review_queue_gate(
        workspace,
        state=state,
        batch_size=weak_batch_size,
        max_batches=weak_max_batches,
    )
    queue_diagnosis = queue_gate.get("diagnosis") or {}
    if queue_diagnosis.get("should_stop"):
        return _write_queue_gate_summary(
            workspace=workspace,
            direction=direction,
            brain_plugin=brain_plugin,
            second_brain_plugin=second_brain_plugin,
            topic=topic,
            state=state,
            qa_report=qa_pass1["report"],
            queue_diagnosis=queue_diagnosis,
            embedding_available=embedding_available,
            environment_warnings=environment_warnings,
            weak_batch_size=weak_batch_size,
            weak_max_batches=weak_max_batches,
            boundary_max_batches=boundary_max_batches,
            seed_total=seed_total,
            allow_no_embedding=allow_no_embedding,
        )
    if (
        not embedding_available
        and weak_max_batches is not None
        and effective_max_batches < needed_batches
        and "embedding_missing_with_capped_brain_budget" not in environment_warnings
    ):
        environment_warnings.append("embedding_missing_with_capped_brain_budget")
        state.event(
            "preflight_warning",
            warning="embedding_missing_with_capped_brain_budget",
            install_hint=EMBED_INSTALL_HINT,
            pending_count=pending_count,
            needed_batches=needed_batches,
            effective_max_batches=effective_max_batches,
        )
    stage_score_papers(
        workspace,
        brain=brain_plugin,
        state=state,
        batch_size=weak_batch_size,
        max_batches=effective_max_batches if effective_max_batches > 0 else None,
    )
    stage_build(workspace, state=state, label="build_after_score")

    # Resolve boundaries before final build — never punt to user.
    # When --second-brain is supplied, cross-model second-pass scoring
    # is the most reliable way to flip ambiguous boundary papers.
    effective_boundary_max_batches = (
        boundary_max_batches
        if boundary_max_batches is not None
        else effective_max_batches
    )
    stage_resolve_boundary(
        workspace,
        brain=brain_plugin,
        state=state,
        second_brain=second_brain_plugin,
        batch_size=weak_batch_size,
        max_batches=(
            max(2, effective_boundary_max_batches)
            if effective_boundary_max_batches is not None
            else None
        ),
    )
    stage_build(workspace, state=state, label="build_after_resolve_boundary")

    # catalog rebuild + final qa
    stage_catalog(workspace, state=state)
    final_qa = stage_qa(workspace, state=state, label="qa_final")
    qa_report = final_qa["report"]

    counts = qa_report.get("counts") or {}
    seed_check_final = qa_report.get("seed_coverage") or {}
    seed_missing = seed_check_final.get("missing_count", 0)

    record_agent_run_step(
        workspace,
        phase="auto-build-final",
        status="completed",
        summary=(
            f"auto-build: brain={brain_plugin.name}; papers={counts.get('papers')}; "
            f"pending={counts.get('pending')}; rejected={counts.get('rejected')}; "
            f"seeds_missing={seed_missing}/{seed_total}"
        ),
        files=[
            "topic.yaml",
            "sources.yaml",
            "data/papers.jsonl",
            "data/pending_review_candidates.json",
            "catalog/manifest.json",
            ".papercompass/auto/state.json",
        ],
    )

    artifacts = {
        "state": workspace_relative_path(workspace, state.path),
        "iterations_log": workspace_relative_path(
            workspace,
            workspace / ".papercompass" / "auto" / "iterations.jsonl",
        ),
        "qa_manifest": qa_report.get("manifest", ""),
        "qa_markdown": qa_report.get("markdown", ""),
        "main_library": workspace_relative_path(workspace, data_dir(workspace) / "papers.jsonl"),
        "pending": workspace_relative_path(
            workspace,
            data_dir(workspace) / "pending_review_candidates.json",
        ),
        "catalog_manifest": workspace_relative_path(workspace, workspace / "catalog" / "manifest.json"),
    }
    # If discovery hit source-level errors, surface them before writing the
    # handoff summary so downstream agents see the recall risk in one place.
    discover_seen = (state.get("stages", {}).get("discover_iter1", {}) or {}).get(
        "summary", {}
    )
    if discover_seen and (discover_seen.get("source_errors") or 0) > 0:
        artifacts["discovery_warning"] = (
            f"discover_iter1 had {discover_seen['source_errors']} source-level errors; "
            f"remote_calls_used={discover_seen.get('remote_calls_used')} of "
            f"{discover_seen.get('remote_calls_limit')}"
        )

    # Collect explicit truncation signals from per-stage state. Critical
    # for distinguishing "stage capped early" (system gave up) from
    # "stage failed" (semantic/QA error). final_summary surfaces both.
    truncations: list[dict[str, Any]] = []
    for stage_name, stage_data in (state.get("stages", {}) or {}).items():
        if isinstance(stage_data, dict) and stage_data.get("truncated"):
            truncations.append({
                "stage": stage_name,
                "reason": stage_data.get("truncation_reason") or "unspecified",
                "uncovered": stage_data.get("uncovered_capped", 0),
                "batches": stage_data.get("batches"),
            })
    brain_missing_scores: list[dict[str, Any]] = []
    for stage_name, stage_data in (state.get("stages", {}) or {}).items():
        missing = (
            int(stage_data.get("brain_missing_scores") or 0)
            if isinstance(stage_data, dict)
            else 0
        )
        if missing > 0:
            brain_missing_scores.append({"stage": stage_name, "missing": missing})

    # Sum brain usage / cost from iterations.jsonl. Plugins that report
    # token / cost (DeepSeekAPIPlugin) populate these via log_brain_call's
    # `extra` field; CLI plugins that don't simply contribute zeros.
    iter_path = workspace / ".papercompass" / "auto" / "iterations.jsonl"
    usage_by_stage: dict[str, dict[str, float | int]] = {}
    model_names: set[str] = set()
    model_sources: set[str] = set()
    reasoning_efforts: set[str] = set()
    reasoning_effort_sources: set[str] = set()
    total_input = 0
    total_output = 0
    total_cost = 0.0
    if iter_path.exists():
        try:
            for line in iter_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                stg = row.get("stage", "?")
                inp = int(row.get("input_tokens") or 0)
                out = int(row.get("output_tokens") or 0)
                cost = float(row.get("cost_usd") or 0)
                if row.get("model"):
                    model_names.add(str(row.get("model")))
                if row.get("model_source"):
                    model_sources.add(str(row.get("model_source")))
                if row.get("reasoning_effort"):
                    reasoning_efforts.add(str(row.get("reasoning_effort")))
                if row.get("reasoning_effort_source"):
                    reasoning_effort_sources.add(str(row.get("reasoning_effort_source")))
                bucket = usage_by_stage.setdefault(
                    stg, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
                )
                bucket["calls"] += 1
                bucket["input_tokens"] += inp
                bucket["output_tokens"] += out
                bucket["cost_usd"] += cost
                total_input += inp
                total_output += out
                total_cost += cost
        except Exception:
            pass

    # Record which fusion channels actually ran. embedding is the only optional
    # dependency; brain is required; metadata always available. Reader can tell
    # whether a low-precision build was caused by missing signal vs bad scoring.
    channels_active = {
        "embedding": embedding_available,
        "brain": True,
        "metadata": True,
    }
    environment = {
        "embedding_available": embedding_available,
        "embedding_required": not allow_no_embedding,
        "allow_no_embedding": allow_no_embedding,
        "warnings": environment_warnings,
        "install_hints": (
            {"embedding": EMBED_INSTALL_HINT}
            if not embedding_available
            else {}
        ),
        "weak_batch_size": weak_batch_size,
        "weak_batches_needed": needed_batches,
        "weak_batches_effective": effective_max_batches,
        "weak_max_batches": weak_max_batches,
        "boundary_max_batches": boundary_max_batches,
        "boundary_batches_effective": effective_boundary_max_batches,
    }

    delivery = _delivery_assessment(
        qa_report,
        truncations,
        environment_warnings,
        brain_missing_scores,
    )

    summary_path = workspace / ".papercompass" / "auto" / "final_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": "papercompass.handoff.v1",
        "status": delivery["deliverable_status"],
        "deliverable_status": delivery["deliverable_status"],
        "safe_for_default_llm_retrieval": delivery["safe_for_default_llm_retrieval"],
        "exit_code": delivery["exit_code"],
        "workspace": workspace_label(workspace),
        "workspace_contract": workspace_contract_summary(workspace, topic),
        "direction": direction,
        "brain": brain_plugin.name,
        "topic_id": topic.get("topic_id"),
        "qa_status": qa_report.get("status"),
        "counts": counts,
        "quality": {
            "qa_status": qa_report.get("status"),
            "critical": qa_report.get("critical") or [],
            "warnings": list(qa_report.get("warnings") or []) + environment_warnings,
            "qa_warnings": qa_report.get("warnings") or [],
            "environment_warnings": environment_warnings,
            "hard_reasons": delivery["hard_reasons"],
        },
        "coverage": {
            "truncations": truncations,
            "brain_missing_scores": brain_missing_scores,
            "brain_missing_score_count": sum(
                int(item.get("missing") or 0) for item in brain_missing_scores
            ),
            "source_risk_count": (qa_report.get("source_coverage") or {}).get("problem_count", 0),
            "unscored_count": sum(int(t.get("uncovered") or 0) for t in truncations),
            "unresolved_boundary_count": (qa_report.get("score_papers") or {}).get("deferred_count", 0),
            "recall_pool": qa_report.get("recall_pool") or {},
        },
        "models": {
            "build_brain": brain_plugin.name,
            "second_brain": second_brain_plugin.name if second_brain_plugin else "",
            "cross_model": bool(second_brain_plugin and second_brain_plugin.name != brain_plugin.name),
        },
        "artifacts": artifacts,
        "channels_active": channels_active,
        "environment": environment,
        "seed_total": seed_total,
        "seed_missing": seed_missing,
        "iterations": iterations,
        "truncations": truncations,
        "brain_usage": {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cost_usd": round(total_cost, 6),
            "metadata": {
                "models": sorted(model_names),
                "model_sources": sorted(model_sources),
                "reasoning_efforts": sorted(reasoning_efforts),
                "reasoning_effort_sources": sorted(reasoning_effort_sources),
            },
            "by_stage": {
                stg: {
                    **{k: int(v) if k != "cost_usd" else round(float(v), 6) for k, v in usage.items()}
                }
                for stg, usage in usage_by_stage.items()
            },
        },
    }
    summary = portable_workspace_data(workspace, summary)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return AutoBuildResult(
        workspace=workspace,
        direction=direction,
        brain=brain_plugin.name,
        topic_id=topic.get("topic_id") or "",
        final_status=qa_report.get("status") or "unknown",
        papers=int(counts.get("papers") or 0),
        pending=int(counts.get("pending") or 0),
        rejected=int(counts.get("rejected") or 0),
        iterations=iterations,
        seed_total=int(seed_total or 0),
        seed_missing=int(seed_missing or 0),
        qa_status=qa_report.get("status") or "unknown",
        qa_critical=qa_report.get("critical") or [],
        qa_warnings=qa_report.get("warnings") or [],
        delivery_status=delivery["deliverable_status"],
        safe_for_default_llm_retrieval=delivery["safe_for_default_llm_retrieval"],
        exit_code=int(delivery["exit_code"]),
        artifacts=artifacts,
        channels_active=channels_active,
        environment=environment,
        environment_warnings=environment_warnings,
    )

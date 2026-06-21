from __future__ import annotations

import contextlib
from datetime import datetime
from pathlib import Path
import shutil
from typing import Any

from .build import build_workspace
from .catalog import build_catalog
from .confirmation import (
    ConfirmationTokenError,
    legacy_user_confirmation_allowed,
    sha256_file,
    sha256_json,
    update_confirmation_inputs,
    validate_confirmation_token,
)
from .config import data_dir, ensure_workspace_dirs, manifests_dir, portable_workspace_data, state_dir, workspace_label, workspace_relative_path
from .discovery import run_discovery
from .metrics import record_run_metric
from .normalize import identity_keys
from .qa import build_quality_report
from .text import clean_text, iter_jsonl, read_json, workspace_lock, write_json, write_jsonl


class UpdateConfirmationRequired(RuntimeError):
    """Raised before mutating a workspace when the caller skipped confirmation."""


def update_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def update_dir(workspace: Path) -> Path:
    return state_dir(workspace) / "updates"


def checkpoints_dir(workspace: Path) -> Path:
    return state_dir(workspace) / "checkpoints"


def identity_checkpoint_path(workspace: Path) -> Path:
    return checkpoints_dir(workspace) / "identity_index.jsonl"


def discovery_checkpoint_path(workspace: Path) -> Path:
    return checkpoints_dir(workspace) / "discovery.json"


def load_identity_checkpoint(workspace: Path) -> set[str]:
    path = identity_checkpoint_path(workspace)
    if not path.exists():
        return set()
    keys: set[str] = set()
    for row in iter_jsonl(path):
        if isinstance(row, dict) and row.get("key"):
            keys.add(str(row["key"]))
    return keys


def build_identity_rows(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        keys = identity_keys(paper)
        if not keys:
            continue
        primary = keys[0]
        for key in keys:
            rows.append({
                "key": key,
                "primary_key": primary,
                "title": paper.get("title", ""),
                "year": paper.get("year"),
            })
    return rows


def _artifact_state(workspace: Path) -> dict[str, Any]:
    latest_manifest = manifests_dir(workspace) / "latest.json"
    catalog_manifest = workspace / "catalog" / "manifest.json"
    papers_jsonl = data_dir(workspace) / "papers.jsonl"
    return {
        "latest_manifest_hash": sha256_file(latest_manifest),
        "catalog_manifest_hash": sha256_file(catalog_manifest),
        "papers_jsonl_hash": sha256_file(papers_jsonl),
    }


UPDATE_TRANSACTION_ARTIFACTS = (".raw", "data", "catalog")
STAGED_WORKSPACE_COPY_PATHS = (
    "topic.yaml",
    "sources.yaml",
    "overrides",
    ".raw",
    "data",
    "catalog",
    ".papercompass/reviews",
    ".papercompass/cache",
    ".papercompass/plans",
)
STAGED_AUDIT_DIRS = (
    ".papercompass/manifests",
    ".papercompass/reports",
)
AUDIT_ARTIFACTS_RETAINED = (
    ".papercompass/updates",
    ".papercompass/logs",
    ".papercompass/manifests",
    ".papercompass/metrics",
    ".papercompass/reviews",
)


def _unique_child(parent: Path, name: str) -> Path:
    candidate = parent / name
    if not candidate.exists():
        return candidate
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return parent / f"{name}.{stamp}"


def _snapshot_update_artifacts(workspace: Path, out_dir: Path) -> dict[str, Any]:
    backup_dir = out_dir / "backup_before"
    backup_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for name in UPDATE_TRANSACTION_ARTIFACTS:
        src = workspace / name
        dst = backup_dir / name
        existed = src.exists()
        if existed:
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        rows.append({
            "name": name,
            "existed": existed,
            "backup": workspace_relative_path(workspace, dst) if existed else "",
        })
    return {
        "mode": "staged_workspace_publish_with_backup_restore",
        "backup_dir": workspace_relative_path(workspace, backup_dir),
        "backup_retained": True,
        "rollback_scope": list(UPDATE_TRANSACTION_ARTIFACTS),
        "artifacts": rows,
    }


def _checkpoint_transaction_paths(workspace: Path) -> tuple[Path, ...]:
    return (
        discovery_checkpoint_path(workspace),
        identity_checkpoint_path(workspace),
    )


def _snapshot_update_checkpoints(workspace: Path, out_dir: Path) -> dict[str, Any]:
    backup_dir = out_dir / "checkpoint_backup_before"
    backup_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for path in _checkpoint_transaction_paths(workspace):
        backup = backup_dir / path.name
        existed = path.exists()
        if existed:
            shutil.copy2(path, backup)
        rows.append({
            "name": path.name,
            "path": workspace_relative_path(workspace, path),
            "existed": existed,
            "backup": workspace_relative_path(workspace, backup) if existed else "",
        })
    return {
        "mode": "checkpoint_backup_restore",
        "backup_dir": workspace_relative_path(workspace, backup_dir),
        "checkpoints": rows,
    }


def _restore_update_checkpoints(workspace: Path, out_dir: Path) -> dict[str, Any]:
    backup_dir = out_dir / "checkpoint_backup_before"
    failed_dir = out_dir / "failed_checkpoints"
    restored: list[dict[str, Any]] = []
    moved_current: list[dict[str, Any]] = []
    for path in _checkpoint_transaction_paths(workspace):
        backup = backup_dir / path.name
        if path.exists():
            moved_to = Path(_move_artifact(path, failed_dir / path.name))
            moved_current.append({
                "name": path.name,
                "to": workspace_relative_path(workspace, moved_to),
            })
        if backup.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            backup.replace(path)
            restored.append({
                "name": path.name,
                "from": workspace_relative_path(workspace, backup),
                "to": workspace_relative_path(workspace, path),
            })
    return {
        "restored": restored,
        "moved_current_to_failed_checkpoints": moved_current,
        "failed_checkpoints_dir": workspace_relative_path(workspace, failed_dir),
    }


def _copy_path(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _prepare_staged_workspace(workspace: Path, out_dir: Path) -> Path:
    staged = out_dir / "staged_workspace"
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True, exist_ok=True)
    for rel in STAGED_WORKSPACE_COPY_PATHS:
        _copy_path(workspace / rel, staged / rel)
    ensure_workspace_dirs(staged)
    return staged


def _copy_staged_audit_artifacts(workspace: Path, staged: Path) -> list[str]:
    copied: list[str] = []
    for rel in STAGED_AUDIT_DIRS:
        src = staged / rel
        if not src.exists():
            continue
        dst = workspace / rel
        shutil.copytree(src, dst, dirs_exist_ok=True)
        copied.append(rel)
    return copied


def _publish_staged_artifacts(workspace: Path, staged: Path, out_dir: Path) -> dict[str, Any]:
    previous_dir = out_dir / "previous_artifacts"
    moved_current: list[dict[str, str]] = []
    published: list[dict[str, str]] = []
    try:
        for name in UPDATE_TRANSACTION_ARTIFACTS:
            current = workspace / name
            staged_artifact = staged / name
            if current.exists():
                moved_to = Path(_move_artifact(current, previous_dir / name))
                moved_current.append({
                    "name": name,
                    "to": workspace_relative_path(workspace, moved_to),
                })
            if staged_artifact.exists():
                staged_artifact.replace(workspace / name)
                published.append({
                    "name": name,
                    "from": workspace_relative_path(workspace, staged_artifact),
                    "to": workspace_relative_path(workspace, workspace / name),
                })
        copied_audit = _copy_staged_audit_artifacts(workspace, staged)
    except BaseException:
        _restore_update_artifacts(workspace, out_dir)
        raise
    if previous_dir.exists():
        shutil.rmtree(previous_dir)
    return {
        "mode": "staged_workspace_publish",
        "staged_workspace": workspace_relative_path(workspace, staged),
        "published": published,
        "moved_previous_artifacts": moved_current,
        "copied_audit_artifacts": copied_audit,
    }


def _cleanup_success_backup(workspace: Path, out_dir: Path) -> dict[str, Any]:
    backup_dir = out_dir / "backup_before"
    if not backup_dir.exists():
        return {"status": "not_found", "backup_retained": False}
    shutil.rmtree(backup_dir)
    return {
        "status": "removed",
        "backup_dir": workspace_relative_path(workspace, backup_dir),
        "backup_retained": False,
    }


def _cleanup_success_staging(workspace: Path, out_dir: Path) -> dict[str, Any]:
    staged = out_dir / "staged_workspace"
    if not staged.exists():
        return {"status": "not_found", "staged_workspace_retained": False}
    shutil.rmtree(staged)
    return {
        "status": "removed",
        "staged_workspace": workspace_relative_path(workspace, staged),
        "staged_workspace_retained": False,
    }


def _move_artifact(path: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    final_target = target if not target.exists() else _unique_child(target.parent, target.name)
    path.replace(final_target)
    return str(final_target)


def _restore_update_artifacts(workspace: Path, out_dir: Path) -> dict[str, Any]:
    backup_dir = out_dir / "backup_before"
    failed_dir = out_dir / "failed_artifacts"
    restored: list[dict[str, Any]] = []
    moved_current: list[dict[str, Any]] = []
    for name in UPDATE_TRANSACTION_ARTIFACTS:
        current = workspace / name
        backup = backup_dir / name
        if current.exists():
            moved_to = Path(_move_artifact(current, failed_dir / name))
            moved_current.append({
                "name": name,
                "to": workspace_relative_path(workspace, moved_to),
            })
        if backup.exists():
            backup.replace(workspace / name)
            restored.append({
                "name": name,
                "from": workspace_relative_path(workspace, backup),
                "to": workspace_relative_path(workspace, workspace / name),
            })
    return {
        "restored": restored,
        "moved_current_to_failed_artifacts": moved_current,
        "failed_artifacts_dir": workspace_relative_path(workspace, failed_dir),
    }


def _source_query_checkpoints(workspace: Path, *, update: str, inputs: dict[str, Any]) -> dict[str, Any]:
    coverage = read_json(manifests_dir(workspace) / "source_coverage.json", {})
    sources: dict[str, dict[str, Any]] = {}
    if not isinstance(coverage, dict):
        coverage = {}
    for item in coverage.values():
        if not isinstance(item, dict):
            continue
        source = clean_text(item.get("source"))
        if not source:
            continue
        query_payload = {
            "query": item.get("query"),
            "year": item.get("year"),
            "bucket": item.get("bucket"),
            "mode": item.get("mode"),
            "sort": item.get("sort"),
            "page_index": item.get("page_index"),
        }
        query_key = "query:" + sha256_json(query_payload)[:16]
        source_rows = sources.setdefault(source, {})
        source_rows[query_key] = {
            "last_success_at": datetime.now().isoformat(timespec="seconds"),
            "status": item.get("status") or item.get("execution_status"),
            "complete": item.get("complete"),
            "bucket_complete": item.get("bucket_complete"),
            "cursor": item.get("next_token") or item.get("next_offset") or item.get("cursor"),
            "last_run_id": item.get("last_run_id"),
            "kept_count": item.get("kept_count"),
            "result_count": item.get("result_count") or item.get("processed_count"),
        }
    return {
        "schema_version": "papercompass.discovery_checkpoints.v1",
        "last_success_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "checkpointed_full_rebuild_with_identity_delta",
        "update_id": update,
        "inputs": inputs,
        "sources": sources,
    }


@contextlib.contextmanager
def workspace_update_lock(workspace: Path):
    with workspace_lock(workspace):
        yield


def _qa_status(status: str) -> str:
    if status == "failed":
        return "qa_failed"
    if status == "warning":
        return "qa_warning"
    return "updated"


def run_workspace_update(
    workspace: Path,
    *,
    sources: list[str] | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    paperlists_venues: list[str] | None = None,
    refresh: bool = False,
    refresh_coverage: bool = False,
    timeout: int = 35,
    max_remote_calls: int | None = None,
    user_confirmed: bool = False,
    confirmed_token: str | None = None,
    mode: str = "delta",
) -> dict[str, Any]:
    run = update_id()
    run_started_at = datetime.now().isoformat(timespec="seconds")
    with workspace_update_lock(workspace):
        inputs = update_confirmation_inputs(
            workspace=workspace,
            sources=sources,
            min_year=min_year,
            max_year=max_year,
            paperlists_venues=paperlists_venues,
            refresh=refresh,
            refresh_coverage=refresh_coverage,
            timeout=timeout,
            max_remote_calls=max_remote_calls,
            mode=mode,
        )
        if confirmed_token:
            try:
                validate_confirmation_token(
                    workspace,
                    confirmed_token,
                    command="update",
                    inputs=inputs,
                )
            except ConfirmationTokenError as exc:
                raise UpdateConfirmationRequired(str(exc)) from exc
        elif not (user_confirmed and legacy_user_confirmation_allowed()):
            raise UpdateConfirmationRequired(
                "papercompass update 需要有效 --confirmed-token；请先运行 "
                "`papercompass update --prepare ...`，让用户确认本次会修改 "
                "workspace 的 .raw/data/catalog/QA manifests 后再执行。"
            )
        if mode not in {"delta", "full"}:
            raise ValueError("update mode must be 'delta' or 'full'")
        ensure_workspace_dirs(workspace)
        out_dir = update_dir(workspace) / f"update_{run}"
        out_dir.mkdir(parents=True, exist_ok=True)
        previous_identity = load_identity_checkpoint(workspace) if mode == "delta" else set()
        artifact_before = _artifact_state(workspace)
        artifact_transaction = _snapshot_update_artifacts(workspace, out_dir)
        staged_workspace = _prepare_staged_workspace(workspace, out_dir)
        artifact_transaction["staged_workspace"] = workspace_relative_path(workspace, staged_workspace)
        plan = {
            "schema_version": "papercompass.update_plan.v1",
            "update_id": run,
            "mode": "staged_checkpointed_full_rebuild_with_identity_delta" if mode == "delta" else "staged_conservative_full_refresh",
            "inputs": inputs,
            "previous_identity_count": len(previous_identity),
            "artifact_transaction": artifact_transaction,
        }
        write_json(out_dir / "plan.json", plan)
        try:
            discovery = run_discovery(
                staged_workspace,
                sources=sources,
                min_year=min_year,
                max_year=max_year,
                paperlists_venues=paperlists_venues,
                refresh=refresh,
                build=False,
                catalog=False,
                timeout=timeout,
                max_remote_calls=max_remote_calls,
            )
            build = build_workspace(staged_workspace)
            catalog = build_catalog(staged_workspace)
            qa = build_quality_report(staged_workspace, refresh_coverage=refresh_coverage)
        except BaseException:
            raise
        status = _qa_status(str(qa.get("status") or ""))
        papers = build.get("papers") if isinstance(build.get("papers"), list) else []
        if not papers:
            from .text import read_json

            loaded = read_json(data_dir(staged_workspace) / "papers.json", [])
            papers = loaded if isinstance(loaded, list) else []
        identity_rows = build_identity_rows(papers)
        current_identity = {row["key"] for row in identity_rows}
        new_identity = sorted(current_identity - previous_identity) if mode == "delta" else sorted(current_identity)
        identity_delta_rows = [row for row in identity_rows if row["key"] in set(new_identity)]
        delta_path = out_dir / "identity_delta.jsonl"
        write_jsonl(delta_path, identity_delta_rows)
        checkpoint_committed = False
        rollback: dict[str, Any] = {}
        publish: dict[str, Any] = {}
        if qa.get("status") != "failed":
            artifact_transaction["checkpoint_backup"] = _snapshot_update_checkpoints(workspace, out_dir)
            publish_completed = False
            checkpoint_writes_started = False
            try:
                publish = _publish_staged_artifacts(workspace, staged_workspace, out_dir)
                publish_completed = True
                artifact_transaction["publish"] = publish
                checkpoint_writes_started = True
                write_json(discovery_checkpoint_path(workspace), _source_query_checkpoints(workspace, update=run, inputs=inputs))
                write_jsonl(identity_checkpoint_path(workspace), identity_rows)
            except BaseException as exc:
                rollback = {
                    "reason": "checkpoint_publish_failed" if publish_completed else "publish_failed",
                    "error": str(exc),
                    "artifacts": _restore_update_artifacts(workspace, out_dir) if publish_completed else {},
                    "checkpoints": _restore_update_checkpoints(workspace, out_dir) if checkpoint_writes_started else {},
                    "staged_workspace": workspace_relative_path(workspace, staged_workspace),
                    "staged_workspace_retained": staged_workspace.exists(),
                }
                artifact_transaction["checkpoint_publish_rollback"] = rollback
                artifact_transaction["backup_retained"] = True
                artifact_transaction["staged_workspace_retained"] = staged_workspace.exists()
                with contextlib.suppress(Exception):
                    write_json(
                        out_dir / "commit_checkpoint_failed.json",
                        portable_workspace_data(
                            workspace,
                            {
                                "schema_version": "papercompass.update_commit.v1",
                                "update_id": run,
                                "phase": "rolled_back",
                                "qa_status": qa.get("status"),
                                "checkpoint_committed": False,
                                "artifact_transaction": artifact_transaction,
                                "rollback_scope": list(UPDATE_TRANSACTION_ARTIFACTS),
                                "rollback": rollback,
                                "publish": publish,
                            },
                        ),
                    )
                raise
            checkpoint_committed = True
            backup_cleanup = _cleanup_success_backup(workspace, out_dir)
            staging_cleanup = _cleanup_success_staging(workspace, out_dir)
            artifact_transaction["backup_cleanup"] = backup_cleanup
            artifact_transaction["staging_cleanup"] = staging_cleanup
            artifact_transaction["backup_retained"] = bool(backup_cleanup.get("backup_retained"))
            artifact_transaction["staged_workspace_retained"] = bool(staging_cleanup.get("staged_workspace_retained"))
        else:
            backup_cleanup = _cleanup_success_backup(workspace, out_dir)
            artifact_transaction["backup_cleanup"] = backup_cleanup
            artifact_transaction["backup_retained"] = bool(backup_cleanup.get("backup_retained"))
            artifact_transaction["staged_workspace_retained"] = staged_workspace.exists()
            rollback = {
                "reason": "qa_failed_before_publish",
                "restored": [],
                "moved_current_to_failed_artifacts": [],
                "staged_workspace": workspace_relative_path(workspace, staged_workspace),
                "staged_workspace_retained": staged_workspace.exists(),
            }
        artifact_after = _artifact_state(workspace)
        commit = {
            "schema_version": "papercompass.update_commit.v1",
            "update_id": run,
            "phase": "committed" if checkpoint_committed else "rolled_back",
            "qa_status": qa.get("status"),
            "checkpoint_committed": checkpoint_committed,
            "artifact_transaction": artifact_transaction,
            "rollback_scope": list(UPDATE_TRANSACTION_ARTIFACTS),
            "audit_artifacts_retained": [
                workspace_relative_path(workspace, out_dir),
                *AUDIT_ARTIFACTS_RETAINED,
            ],
            "rollback": rollback,
            "publish": publish,
            "artifact_before": artifact_before,
            "artifact_after": artifact_after,
            "raw_segments_written": [
                row.get("raw_output")
                for result in (discovery.get("source_results") or [])
                for row in ([result] if isinstance(result, dict) else [])
                if row.get("raw_output")
            ],
        }
        commit_path = out_dir / "commit.json"
        write_json(commit_path, portable_workspace_data(workspace, commit))
        summary = {
            "schema_version": "papercompass.update.v1",
            "update_id": run,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "workspace": workspace_label(workspace),
            "mode": "staged_checkpointed_full_rebuild_with_identity_delta" if mode == "delta" else "staged_conservative_full_refresh",
            "status": status,
            "inputs": {
                "sources": sources,
                "min_year": min_year,
                "max_year": max_year,
                "paperlists_venues": paperlists_venues,
                "refresh": refresh,
                "refresh_coverage": refresh_coverage,
                "timeout": timeout,
                "max_remote_calls": max_remote_calls,
                "mode": mode,
            },
            "delta": {
                "previous_identity_count": len(previous_identity),
                "current_identity_count": len(current_identity),
                "new_identity_count": len(new_identity),
                "identity_delta": workspace_relative_path(workspace, delta_path),
                "checkpoint_committed": checkpoint_committed,
                "artifact_transaction": artifact_transaction,
                "rollback": rollback,
                "publish": publish,
                "catalog_rebuild": "full",
                "commit": workspace_relative_path(workspace, commit_path),
            },
            "discovery": discovery,
            "build": build,
            "catalog": catalog,
            "qa": {
                "status": qa.get("status"),
                "critical": qa.get("critical", []),
                "warnings": qa.get("warnings", []),
                "counts": qa.get("counts", {}),
                "manifest": qa.get("manifest", ""),
                "markdown": qa.get("markdown", ""),
            },
        }
        summary_path = out_dir / "summary.json"
        latest_path = update_dir(workspace) / "latest.json"
        summary = portable_workspace_data(workspace, summary)
        summary["summary"] = workspace_relative_path(workspace, summary_path)
        write_json(summary_path, summary)
        write_json(latest_path, summary)
        record_run_metric(workspace, {
            "run_id": run,
            "command": "update",
            "started_at": run_started_at,
            "finished_at": summary.get("updated_at"),
            "remote_calls_used": (discovery or {}).get("remote_calls_used"),
            "remote_calls_limit": (discovery or {}).get("remote_calls_limit"),
            "source_error_count": sum(
                len(result.get("errors") or [])
                for result in ((discovery or {}).get("source_results") or [])
                if isinstance(result, dict)
            ),
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "llm_cost_usd": 0.0,
            "qa_status": qa.get("status"),
            "prefilter_candidate_count": (qa.get("prefilter_efficiency") or {}).get("candidate_count"),
            "prefilter_llm_review_ratio": (qa.get("prefilter_efficiency") or {}).get("llm_review_ratio"),
            "review_cache_hit_rate": None,
        })
        return summary

from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
import re
import time
import zipfile
from typing import Any

from .config import data_dir, manifests_dir, state_dir, workspace_label, workspace_relative_path
from .confirmation import parse_timestamp, utc_now
from .metrics import run_metrics_path
from .qa import build_manifest_integrity_report, prefilter_efficiency_report, source_preflight_report
from .auto.review_cache import candidate_fingerprint, load_review_cache, topic_context_hash
from .config import load_topic_config
from .text import clean_text, iter_jsonl, read_json, workspace_lock


ARCHIVE_READ_LIMIT_BYTES = 2 * 1024 * 1024
DOCTOR_FIX_MIN_AGE_SECONDS = 300
ARCHIVE_DENY_PARTS = {
    ".papercompass",
    ".raw",
    ".claude",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    "gpt-pro意见",
    "方案与进度",
    "papercompass.egg-info",
}
ARCHIVE_DENY_SUFFIXES = (".pyc", ".pyo", ".key", ".pem", ".egg-info")
SECRET_SCAN_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".jsonl",
    ".env",
    ".ini",
    ".cfg",
    ".conf",
    ".lock",
}
SECRET_SCAN_NAMES = {".env"}
SECRET_PATTERNS = [
    re.compile(rb"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+=]{12,}['\"]?"),
    re.compile(rb"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(rb"OPENALEX_API_KEY\s*[:=]\s*['\"]?[^'\"\s]{8,}['\"]?", re.IGNORECASE),
]
HIGH_ENTROPY_RE = re.compile(rb"[A-Za-z0-9_\-+/=]{32,}")
PLACEHOLDER_SECRET_VALUES = {
    "your_api_key_here",
    "api_key_here",
    "example_api_key",
    "example-token",
    "dummy-token",
}


def _is_doctor_trash_path(workspace: Path, path: Path) -> bool:
    try:
        path.relative_to(workspace / ".papercompass" / "trash")
    except ValueError:
        return False
    return True


def _secret_value(match: bytes) -> tuple[str, bool]:
    text = match.decode("utf-8", errors="ignore").strip()
    quoted = re.findall(r"['\"]([^'\"]+)['\"]", text)
    value = quoted[-1].strip() if quoted else re.split(r"[:=]", text, maxsplit=1)[-1].strip().strip("'\"")
    return value, bool(quoted)


def _is_placeholder_secret(match: bytes) -> bool:
    raw_value, _quoted = _secret_value(match)
    value = raw_value.lower()
    return (
        value in PLACEHOLDER_SECRET_VALUES
        or value.startswith("your_")
        or value.startswith("example_")
        or value.startswith("dummy")
        or value.startswith("fake")
        or value in {"none", "null", "true", "false"}
        or bool(re.fullmatch(r"[A-Z][A-Z0-9_]{4,}", raw_value))
    )


def _is_allowed_secret_match(match: bytes, *, path: str) -> bool:
    if _is_placeholder_secret(match):
        return True
    value, quoted = _secret_value(match)
    suffix = Path(path).suffix.lower()
    if suffix == ".py" and not quoted:
        return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", value))
    return False


def _looks_like_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    total = len(text)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _is_allowlisted_entropy_token(token: bytes, *, path: str) -> bool:
    text = token.decode("utf-8", errors="ignore").strip()
    lower = text.lower()
    if not text or _is_placeholder_secret(token):
        return True
    if path.endswith("uv.lock"):
        return True
    suffix = Path(path).suffix.lower()
    if suffix in {".py", ".md", ".txt"} and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{31,}", text):
        return True
    if "/" in text and all(part.replace("-", "").isalpha() for part in text.split("/") if part):
        return True
    if "=" in text:
        left = text.split("=", 1)[0]
        if re.fullmatch(r"[A-Z][A-Z0-9_]{4,}", left):
            return True
    if "sha256" in lower or lower.startswith("hash") or lower.startswith("digest"):
        return True
    if re.fullmatch(r"[a-f0-9]{32,128}", lower):
        return True
    if any(marker in lower for marker in ("example", "dummy", "placeholder", "fake", "test-token")):
        return True
    if path.endswith("uv.lock") and (lower.startswith("sha256-") or re.fullmatch(r"[a-z0-9+/=_-]{43,}", lower)):
        return True
    return _shannon_entropy(text) < 4.2


def _should_secret_scan(name: str, data: bytes | None = None) -> bool:
    path = Path(name)
    suffix = path.suffix.lower()
    if suffix in SECRET_SCAN_SUFFIXES or path.name in SECRET_SCAN_NAMES:
        return True
    return suffix == "" and data is not None and _looks_like_text(data)


def _is_bad_archive_entry(name: str) -> bool:
    parts = Path(name).parts
    part_set = set(parts)
    return (
        _is_unsafe_archive_path(name)
        or
        bool(part_set & ARCHIVE_DENY_PARTS)
        or any(part.endswith(".egg-info") for part in parts)
        or name.endswith(ARCHIVE_DENY_SUFFIXES)
    )


def _is_unsafe_archive_path(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    path = Path(normalized)
    if not normalized or path.is_absolute():
        return True
    if re.match(r"^[A-Za-z]:/", normalized):
        return True
    return any(part in {"..", ""} for part in path.parts)


def _read_list(path: Path) -> list[Any]:
    value = read_json(path, [])
    return value if isinstance(value, list) else []


def _bad_jsonl_lines(paths: list[Path]) -> list[dict[str, Any]]:
    bad: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            list(iter_jsonl(path))
        except ValueError as exc:
            bad.append({"path": str(path), "error": str(exc)})
    return bad[:50]


def _move_to_doctor_trash(workspace: Path, path: Path, *, stamp: str) -> dict[str, str]:
    trash_root = state_dir(workspace) / "trash" / f"doctor_{stamp}"
    try:
        rel = path.relative_to(workspace)
    except ValueError:
        rel = Path(path.name)
    target = trash_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target = target.with_name(f"{target.name}.{stamp}")
    path.replace(target)
    return {
        "from": workspace_relative_path(workspace, path),
        "to": workspace_relative_path(workspace, target),
    }


def _is_old_enough_for_fix(path: Path, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        age = now - path.stat().st_mtime
    except OSError:
        return False
    return age >= DOCTOR_FIX_MIN_AGE_SECONDS


def _expired_confirmation_tokens(workspace: Path) -> list[Path]:
    root = state_dir(workspace) / "confirmations"
    if not root.exists():
        return []
    expired: list[Path] = []
    for path in root.glob("pcfm_*.json"):
        payload = read_json(path, {})
        expires_at = parse_timestamp(payload.get("expires_at")) if isinstance(payload, dict) else None
        if expires_at is not None and expires_at <= utc_now():
            expired.append(path)
    return expired


def _successful_update_backup_dirs(
    workspace: Path,
    *,
    keep_success_backups: int = 0,
) -> list[Path]:
    updates_root = state_dir(workspace) / "updates"
    if not updates_root.exists():
        return []
    candidates: list[Path] = []
    for run_dir in sorted(updates_root.glob("update_*"), key=lambda p: p.stat().st_mtime, reverse=True):
        backup = run_dir / "backup_before"
        if not backup.exists():
            continue
        commit = read_json(run_dir / "commit.json", {})
        summary = read_json(run_dir / "summary.json", {})
        committed = (
            isinstance(commit, dict)
            and commit.get("phase") == "committed"
            and bool(commit.get("checkpoint_committed"))
        )
        if not committed and isinstance(summary, dict):
            delta = summary.get("delta") if isinstance(summary.get("delta"), dict) else {}
            committed = bool(delta.get("checkpoint_committed"))
        if committed:
            candidates.append(backup)
    return candidates[max(0, keep_success_backups):]


def review_cache_staleness_report(workspace: Path) -> dict[str, Any]:
    cache = load_review_cache(workspace, strict=False)
    candidates: list[dict[str, Any]] = []
    for name in ("pending_review_candidates.json", "papers.json", "anchor_papers.json"):
        rows = read_json(data_dir(workspace) / name, [])
        if isinstance(rows, list):
            candidates.extend(row for row in rows if isinstance(row, dict))
    current_fingerprints = {candidate_fingerprint(row) for row in candidates}
    current_topic_hash = topic_context_hash(load_topic_config(workspace))
    cached_keys = set(cache)
    current_match_keys = {
        key
        for key, row in cache.items()
        if row.get("candidate_fingerprint") in current_fingerprints
        and row.get("topic_context_hash") == current_topic_hash
    }
    stale_keys = cached_keys - current_match_keys
    return {
        "entries": len(cache),
        "current_candidate_count": len(candidates),
        "current_match_count": len(current_match_keys),
        "stale_count": len(stale_keys),
        "stale_ratio": round(len(stale_keys) / len(cached_keys), 3) if cached_keys else 0.0,
        "path": workspace_relative_path(workspace, state_dir(workspace) / "cache" / "review" / "brain_scores.v2.jsonl"),
    }


def doctor_workspace(
    workspace: Path,
    *,
    fix: bool = False,
    prune_updates: bool = False,
    keep_success_backups: int = 0,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    if fix:
        with workspace_lock(workspace):
            return _doctor_workspace_unlocked(
                workspace,
                fix=True,
                prune_updates=prune_updates,
                keep_success_backups=keep_success_backups,
            )
    return _doctor_workspace_unlocked(
        workspace,
        fix=False,
        prune_updates=prune_updates,
        keep_success_backups=keep_success_backups,
    )


def _doctor_workspace_unlocked(
    workspace: Path,
    *,
    fix: bool = False,
    prune_updates: bool = False,
    keep_success_backups: int = 0,
) -> dict[str, Any]:
    tmp_orphans = [
        workspace_relative_path(workspace, path)
        for path in workspace.rglob("*.tmp")
        if ".git" not in path.parts and not _is_doctor_trash_path(workspace, path)
    ][:50]
    catalog_orphans = [
        workspace_relative_path(workspace, path)
        for path in workspace.glob(".catalog.*")
    ][:50]
    jsonl_paths = [
        *data_dir(workspace).glob("*.jsonl"),
        *state_dir(workspace).glob("**/*.jsonl"),
        *workspace.glob(".raw/**/*.jsonl"),
    ]
    bad_jsonl = _bad_jsonl_lines(jsonl_paths)
    pending_count = len(_read_list(data_dir(workspace) / "pending_review_candidates.json"))
    latest_update = read_json(state_dir(workspace) / "updates" / "latest.json", {})
    latest_auto = read_json(state_dir(workspace) / "auto" / "final_summary.json", {})
    prefilter = prefilter_efficiency_report(workspace)
    source_preflight = source_preflight_report(workspace)
    build_integrity = build_manifest_integrity_report(workspace)
    review_cache = review_cache_staleness_report(workspace)
    discovery_checkpoint = read_json(state_dir(workspace) / "checkpoints" / "discovery.json", {})
    identity_checkpoint_exists = (state_dir(workspace) / "checkpoints" / "identity_index.jsonl").exists()
    metrics_exists = run_metrics_path(workspace).exists()
    fixes: dict[str, list[dict[str, str]]] = {
        "orphan_tmp_files": [],
        "orphan_catalog_swap_dirs": [],
        "expired_confirmation_tokens": [],
        "update_success_backups": [],
        "skipped_recent_orphans": [],
    }
    if fix:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for rel in list(tmp_orphans):
            path = workspace / rel
            if path.exists() and path.is_file():
                if _is_old_enough_for_fix(path):
                    fixes["orphan_tmp_files"].append(_move_to_doctor_trash(workspace, path, stamp=stamp))
                else:
                    fixes["skipped_recent_orphans"].append({
                        "path": workspace_relative_path(workspace, path),
                        "reason": "mtime_younger_than_fix_min_age",
                    })
        for rel in list(catalog_orphans):
            path = workspace / rel
            if path.exists():
                if _is_old_enough_for_fix(path):
                    fixes["orphan_catalog_swap_dirs"].append(_move_to_doctor_trash(workspace, path, stamp=stamp))
                else:
                    fixes["skipped_recent_orphans"].append({
                        "path": workspace_relative_path(workspace, path),
                        "reason": "mtime_younger_than_fix_min_age",
                    })
        for path in _expired_confirmation_tokens(workspace):
            if path.exists():
                fixes["expired_confirmation_tokens"].append(_move_to_doctor_trash(workspace, path, stamp=stamp))
        if prune_updates:
            for path in _successful_update_backup_dirs(
                workspace,
                keep_success_backups=keep_success_backups,
            ):
                if path.exists():
                    if _is_old_enough_for_fix(path):
                        fixes["update_success_backups"].append(_move_to_doctor_trash(workspace, path, stamp=stamp))
                    else:
                        fixes["skipped_recent_orphans"].append({
                            "path": workspace_relative_path(workspace, path),
                            "reason": "mtime_younger_than_fix_min_age",
                        })
        if any(fixes.values()):
            tmp_orphans = [
                workspace_relative_path(workspace, path)
                for path in workspace.rglob("*.tmp")
                if ".git" not in path.parts and not _is_doctor_trash_path(workspace, path)
            ][:50]
            catalog_orphans = [
                workspace_relative_path(workspace, path)
                for path in workspace.glob(".catalog.*")
            ][:50]
    issues: list[str] = []
    warnings: list[str] = []
    if tmp_orphans:
        warnings.append("orphan_tmp_files")
    if catalog_orphans:
        warnings.append("orphan_catalog_swap_dirs")
    if bad_jsonl:
        issues.append("bad_jsonl_lines")
    if pending_count:
        warnings.append("pending_review_unresolved")
    if build_integrity.get("mismatch_count", 0):
        issues.append("build_manifest_integrity_mismatch")
    if prefilter.get("status") == "missing":
        warnings.append("prefilter_missing")
    if source_preflight.get("blocked_count", 0):
        warnings.append("source_preflight_blocked")
    status = "failed" if issues else ("warning" if warnings else "passed")
    return {
        "schema_version": "papercompass.doctor_workspace.v1",
        "workspace": workspace_label(workspace),
        "status": status,
        "issues": issues,
        "warnings": warnings,
        "orphan_tmp_files": tmp_orphans,
        "orphan_catalog_swap_dirs": catalog_orphans,
        "bad_jsonl": bad_jsonl,
        "pending_review_count": pending_count,
        "build_manifest_integrity": build_integrity,
        "prefilter_efficiency": prefilter,
        "source_preflight": source_preflight,
        "review_cache": review_cache,
        "checkpoints": {
            "discovery": discovery_checkpoint if isinstance(discovery_checkpoint, dict) else {},
            "identity_index_exists": identity_checkpoint_exists,
        },
        "latest_update": latest_update if isinstance(latest_update, dict) else {},
        "latest_auto_summary": latest_auto if isinstance(latest_auto, dict) else {},
        "metrics": {
            "path": workspace_relative_path(workspace, run_metrics_path(workspace)),
            "exists": metrics_exists,
        },
        "fix": {
            "enabled": fix,
            "min_age_seconds": DOCTOR_FIX_MIN_AGE_SECONDS,
            "prune_updates": prune_updates,
            "keep_success_backups": keep_success_backups,
            "moved_to_trash": fixes,
        },
    }


def doctor_archive(archive_path: Path, *, strict: bool = False) -> dict[str, Any]:
    archive_path = archive_path.expanduser().resolve()
    bad_entries: list[str] = []
    secret_hits: list[dict[str, Any]] = []
    large_text_skipped: list[dict[str, Any]] = []
    suffix_counts: Counter[str] = Counter()
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        for info in infos:
            name = info.filename
            suffix = Path(name).suffix
            if suffix:
                suffix_counts[suffix] += 1
            if _is_bad_archive_entry(name):
                bad_entries.append(name)
                continue
            if info.file_size > ARCHIVE_READ_LIMIT_BYTES:
                if suffix.lower() in SECRET_SCAN_SUFFIXES or Path(name).name in SECRET_SCAN_NAMES or suffix == "":
                    large_text_skipped.append({"path": name, "size": info.file_size})
                continue
            data = archive.read(name)
            if not _should_secret_scan(name, data):
                continue
            for pattern in SECRET_PATTERNS:
                match = pattern.search(data)
                if match and not _is_allowed_secret_match(match.group(0), path=name):
                    secret_hits.append({"path": name, "pattern": pattern.pattern.decode("utf-8", errors="replace")})
                    break
            else:
                for match in HIGH_ENTROPY_RE.finditer(data):
                    token = match.group(0)
                    if _is_allowlisted_entropy_token(token, path=name):
                        continue
                    secret_hits.append({"path": name, "pattern": "high_entropy_token"})
                    break
    status = "failed" if bad_entries or secret_hits or (strict and large_text_skipped) else "passed"
    return {
        "schema_version": "papercompass.doctor_archive.v1",
        "archive": str(archive_path),
        "status": status,
        "strict": strict,
        "read_limit_bytes": ARCHIVE_READ_LIMIT_BYTES,
        "entry_count": len(names),
        "bad_entry_count": len(bad_entries),
        "bad_entries": bad_entries[:100],
        "secret_hit_count": len(secret_hits),
        "secret_hits": secret_hits[:50],
        "large_text_skipped_count": len(large_text_skipped),
        "large_text_skipped": large_text_skipped[:50],
        "suffix_counts": dict(suffix_counts),
    }


def monitor_metrics(workspace: Path, *, limit: int = 20) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    metrics_path = run_metrics_path(workspace)
    rows = []
    if metrics_path.exists():
        rows = [row for row in iter_jsonl(metrics_path) if isinstance(row, dict)]
    return {
        "schema_version": "papercompass.monitor_metrics.v1",
        "workspace": workspace_label(workspace),
        "metrics_path": workspace_relative_path(workspace, metrics_path),
        "row_count": len(rows),
        "rows": rows[-max(0, limit):],
    }


def monitor_cost(workspace: Path) -> dict[str, Any]:
    rows = monitor_metrics(workspace, limit=10_000)["rows"]
    total_input = sum(int(row.get("llm_input_tokens") or 0) for row in rows)
    total_output = sum(int(row.get("llm_output_tokens") or 0) for row in rows)
    total_cost = sum(float(row.get("llm_cost_usd") or 0.0) for row in rows)
    by_command: dict[str, dict[str, Any]] = {}
    for row in rows:
        command = clean_text(row.get("command")) or "unknown"
        bucket = by_command.setdefault(command, {"runs": 0, "llm_input_tokens": 0, "llm_output_tokens": 0, "llm_cost_usd": 0.0})
        bucket["runs"] += 1
        bucket["llm_input_tokens"] += int(row.get("llm_input_tokens") or 0)
        bucket["llm_output_tokens"] += int(row.get("llm_output_tokens") or 0)
        bucket["llm_cost_usd"] = round(bucket["llm_cost_usd"] + float(row.get("llm_cost_usd") or 0.0), 6)
    return {
        "schema_version": "papercompass.monitor_cost.v1",
        "workspace": workspace_label(workspace),
        "run_count": len(rows),
        "llm_input_tokens": total_input,
        "llm_output_tokens": total_output,
        "llm_cost_usd": round(total_cost, 6),
        "by_command": by_command,
    }


def monitor_trends(
    workspace: Path,
    *,
    limit: int = 100,
    llm_cost_limit: float | None = None,
) -> dict[str, Any]:
    rows = monitor_metrics(workspace, limit=limit)["rows"]
    run_count = len(rows)
    total_input = sum(int(row.get("llm_input_tokens") or 0) for row in rows)
    total_output = sum(int(row.get("llm_output_tokens") or 0) for row in rows)
    total_cost = round(sum(float(row.get("llm_cost_usd") or 0.0) for row in rows), 6)
    remote_used = sum(int(row.get("remote_calls_used") or 0) for row in rows)
    remote_limit = sum(
        int(row.get("remote_calls_limit") or 0)
        for row in rows
        if row.get("remote_calls_limit") is not None
    )
    cache_hits = sum(int(row.get("review_cache_hit_count") or 0) for row in rows)
    cache_misses = sum(int(row.get("review_cache_miss_count") or 0) for row in rows)
    cache_total = cache_hits + cache_misses
    cache_hit_rate = round(cache_hits / cache_total, 3) if cache_total else None
    prefilter_ratios = [
        float(row.get("prefilter_llm_review_ratio"))
        for row in rows
        if row.get("prefilter_llm_review_ratio") is not None
    ]
    avg_prefilter_ratio = (
        round(sum(prefilter_ratios) / len(prefilter_ratios), 3)
        if prefilter_ratios
        else None
    )
    source_error_count = sum(int(row.get("source_error_count") or 0) for row in rows)
    recent_source_error_count = sum(int(row.get("source_error_count") or 0) for row in rows[-3:])
    source_error_runs = sum(1 for row in rows if int(row.get("source_error_count") or 0) > 0)
    source_failure_rate = round(source_error_runs / run_count, 3) if run_count else 0.0
    qa_status_counts: dict[str, int] = {}
    for row in rows:
        status = clean_text(row.get("qa_status")) or "unknown"
        qa_status_counts[status] = qa_status_counts.get(status, 0) + 1
    alerts: list[dict[str, Any]] = []
    if llm_cost_limit is not None and total_cost > llm_cost_limit:
        alerts.append({
            "code": "llm_cost_budget_exceeded",
            "value": total_cost,
            "threshold": llm_cost_limit,
        })
    if cache_hit_rate is not None and cache_total >= 5 and cache_hit_rate < 0.2:
        alerts.append({
            "code": "review_cache_hit_rate_low",
            "value": cache_hit_rate,
            "threshold": 0.2,
        })
    if avg_prefilter_ratio is not None and avg_prefilter_ratio > 0.6:
        alerts.append({
            "code": "prefilter_llm_review_ratio_high",
            "value": avg_prefilter_ratio,
            "threshold": 0.6,
        })
    if remote_limit and remote_used / remote_limit >= 0.8:
        alerts.append({
            "code": "remote_calls_near_limit",
            "value": round(remote_used / remote_limit, 3),
            "threshold": 0.8,
        })
    if recent_source_error_count:
        alerts.append({
            "code": "source_errors_recent",
            "value": recent_source_error_count,
            "threshold": 0,
        })
    return {
        "schema_version": "papercompass.monitor_trends.v1",
        "workspace": workspace_label(workspace),
        "window": {"limit": limit, "run_count": run_count},
        "llm": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cost_usd": total_cost,
            "cost_limit_usd": llm_cost_limit,
        },
        "remote_calls": {
            "used": remote_used,
            "limit": remote_limit or None,
            "usage_ratio": round(remote_used / remote_limit, 3) if remote_limit else None,
        },
        "review_cache": {
            "hit_count": cache_hits,
            "miss_count": cache_misses,
            "hit_rate": cache_hit_rate,
        },
        "prefilter": {
            "avg_llm_review_ratio": avg_prefilter_ratio,
            "sample_count": len(prefilter_ratios),
        },
        "sources": {
            "source_error_count": source_error_count,
            "recent_source_error_count": recent_source_error_count,
            "source_error_runs": source_error_runs,
            "source_failure_rate": source_failure_rate,
        },
        "qa_status_counts": qa_status_counts,
        "alerts": alerts,
        "alert_count": len(alerts),
    }


def monitor_summary(workspace: Path) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    latest_update = read_json(state_dir(workspace) / "updates" / "latest.json", {})
    auto_summary = read_json(state_dir(workspace) / "auto" / "final_summary.json", {})
    metrics_path = run_metrics_path(workspace)
    metric_rows = []
    if metrics_path.exists():
        metric_rows = [row for row in iter_jsonl(metrics_path) if isinstance(row, dict)]
    latest_metric = metric_rows[-1] if metric_rows else {}
    qa_files = sorted(manifests_dir(workspace).glob("quality_gates_*.json"))
    latest_qa = read_json(qa_files[-1], {}) if qa_files else {}
    return {
        "schema_version": "papercompass.monitor_summary.v1",
        "workspace": workspace_label(workspace),
        "latest_update": latest_update if isinstance(latest_update, dict) else {},
        "latest_auto_summary": auto_summary if isinstance(auto_summary, dict) else {},
        "latest_qa": latest_qa if isinstance(latest_qa, dict) else {},
        "latest_metric": latest_metric,
        "metrics_path": workspace_relative_path(workspace, metrics_path),
    }

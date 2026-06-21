from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .config import state_dir, workspace_relative_path
from .text import append_jsonl_locked


def metrics_dir(workspace: Path) -> Path:
    return state_dir(workspace) / "metrics"


def run_metrics_path(workspace: Path) -> Path:
    return metrics_dir(workspace) / "runs.jsonl"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_seconds(started_at: Any, finished_at: Any) -> float | None:
    start = _parse_iso(started_at)
    finish = _parse_iso(finished_at)
    if not start or not finish:
        return None
    return round(max(0.0, (finish - start).total_seconds()), 3)


def record_run_metric(workspace: Path, row: dict[str, Any]) -> dict[str, Any]:
    started_at = row.get("started_at") or now_iso()
    finished_at = row.get("finished_at") or now_iso()
    hit_count = int(row.get("review_cache_hit_count") or 0)
    miss_count = int(row.get("review_cache_miss_count") or 0)
    total_cache = hit_count + miss_count
    hit_rate = row.get("review_cache_hit_rate")
    if hit_rate is None and total_cache:
        hit_rate = round(hit_count / total_cache, 3)
    payload = {
        "schema_version": "papercompass.run_metrics.v1",
        "recorded_at": now_iso(),
        **row,
        "run_id": row.get("run_id", ""),
        "command": row.get("command", ""),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": row.get("duration_seconds")
        if row.get("duration_seconds") is not None
        else _duration_seconds(started_at, finished_at),
        "remote_calls_used": int(row.get("remote_calls_used") or 0),
        "remote_calls_limit": row.get("remote_calls_limit"),
        "llm_input_tokens": int(row.get("llm_input_tokens") or 0),
        "llm_output_tokens": int(row.get("llm_output_tokens") or 0),
        "llm_cost_usd": float(row.get("llm_cost_usd") or 0.0),
        "review_cache_hit_count": hit_count,
        "review_cache_miss_count": miss_count,
        "review_cache_hit_rate": hit_rate,
        "prefilter_candidate_count": row.get("prefilter_candidate_count"),
        "prefilter_llm_review_ratio": row.get("prefilter_llm_review_ratio"),
        "qa_status": row.get("qa_status", ""),
    }
    append_jsonl_locked(run_metrics_path(workspace), [payload])
    return {
        "path": workspace_relative_path(workspace, run_metrics_path(workspace)),
        "row": payload,
    }

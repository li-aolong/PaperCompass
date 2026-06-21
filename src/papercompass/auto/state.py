from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import portable_workspace_data, state_dir
from ..text import append_jsonl_locked, write_json


def auto_dir(workspace: Path) -> Path:
    return state_dir(workspace) / "auto"


def state_path(workspace: Path) -> Path:
    return auto_dir(workspace) / "state.json"


def iteration_log_path(workspace: Path) -> Path:
    return auto_dir(workspace) / "iterations.jsonl"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AutoState:
    """Lightweight checkpoint store for the auto-build pipeline. The state file
    is the source of truth for what stages have completed, so a re-run with
    --resume can skip already-done work."""

    def __init__(self, workspace: Path, *, verbose: bool = False) -> None:
        self.workspace = workspace
        self.path = state_path(workspace)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = self._load()
        self.verbose = verbose

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"created_at": now_iso(), "stages": {}, "events": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"created_at": now_iso(), "stages": {}, "events": []}

    def save(self) -> None:
        write_json(self.path, portable_workspace_data(self.workspace, self.data))

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def stage_status(self, stage: str) -> str:
        return (self.data.get("stages", {}).get(stage) or {}).get("status", "pending")

    def stage_done(self, stage: str) -> bool:
        return self.stage_status(stage) == "completed"

    def begin_stage(self, stage: str, **meta: Any) -> None:
        self.data.setdefault("stages", {})[stage] = {
            "status": "in_progress",
            "started_at": now_iso(),
            **meta,
        }
        self.save()
        if self.verbose:
            self._log_progress(f"[{stage}] in_progress")

    def _log_progress(self, line: str) -> None:
        import sys

        sys.stderr.write(f"papercompass auto: {now_iso()} {line}\n")
        sys.stderr.flush()

    def end_stage(self, stage: str, status: str = "completed", **meta: Any) -> None:
        existing = self.data.setdefault("stages", {}).setdefault(stage, {})
        existing.update(
            {
                "status": status,
                "ended_at": now_iso(),
                **meta,
            }
        )
        self.save()
        if self.verbose:
            summary_bits = []
            for k in ("counts", "papers", "missing", "applied", "added_neg", "result_status"):
                if existing.get(k) is not None:
                    summary_bits.append(f"{k}={existing[k]}")
            tail = " ".join(summary_bits[:4])
            self._log_progress(f"[{stage}] {status}{(' | ' + tail) if tail else ''}")

    def event(self, kind: str, **payload: Any) -> None:
        evt = {"kind": kind, "at": now_iso(), **payload}
        self.data.setdefault("events", []).append(evt)
        self.save()


def log_brain_call(
    workspace: Path,
    *,
    stage: str,
    plugin: str,
    prompt_tokens: int,
    response_text: str,
    parsed_ok: bool,
    duration_seconds: float,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append-only structured log of every brain call so we can audit cost,
    duration and error rate after a run."""
    iteration_log_path(workspace).parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": now_iso(),
        "monotonic": time.monotonic(),
        "stage": stage,
        "plugin": plugin,
        "prompt_chars": prompt_tokens,
        "response_chars": len(response_text or ""),
        "parsed_ok": parsed_ok,
        "duration_seconds": round(duration_seconds, 3),
    }
    if extra:
        entry.update(extra)
    append_jsonl_locked(iteration_log_path(workspace), [entry])

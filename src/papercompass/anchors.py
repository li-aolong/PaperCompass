"""Paths and readers for source-backed recall anchors.

The canonical file is ``.papercompass/plans/anchors.jsonl``. The historical
``seed_papers.jsonl`` name is still written/read for compatibility with older
workspaces and tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import state_dir
from .text import iter_jsonl


ANCHORS_FILENAME = "anchors.jsonl"
LEGACY_SEEDS_FILENAME = "seed_papers.jsonl"


def anchors_plan_path(workspace: Path) -> Path:
    return state_dir(workspace) / "plans" / ANCHORS_FILENAME


def legacy_seeds_plan_path(workspace: Path) -> Path:
    return state_dir(workspace) / "plans" / LEGACY_SEEDS_FILENAME


def existing_anchors_plan_path(workspace: Path) -> Path:
    anchors = anchors_plan_path(workspace)
    if anchors.exists():
        return anchors
    return legacy_seeds_plan_path(workspace)


def iter_anchor_rows(workspace: Path) -> list[dict[str, Any]]:
    path = existing_anchors_plan_path(workspace)
    if not path.exists():
        return []
    return [row for row in iter_jsonl(path) if isinstance(row, dict)]


def write_anchor_rows(workspace: Path, anchors: list[dict[str, Any]]) -> tuple[Path, Path]:
    text = "\n".join(json.dumps(row, ensure_ascii=False) for row in anchors) + ("\n" if anchors else "")
    canonical = anchors_plan_path(workspace)
    legacy = legacy_seeds_plan_path(workspace)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(text, encoding="utf-8")
    legacy.write_text(text, encoding="utf-8")
    return canonical, legacy

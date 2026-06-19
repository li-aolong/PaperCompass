from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR_NAME = ".papercompass"
WORKSPACE_PORTABLE_TOP_LEVELS = {
    STATE_DIR_NAME,
    ".raw",
    "catalog",
    "data",
    "overrides",
    "topic.yaml",
    "sources.yaml",
}


def load_yaml(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return default if data is None else data


def write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def state_dir(workspace: Path) -> Path:
    return workspace / STATE_DIR_NAME


def raw_dir(workspace: Path) -> Path:
    return workspace / ".raw"


def data_dir(workspace: Path) -> Path:
    return workspace / "data"


def catalog_dir(workspace: Path) -> Path:
    return workspace / "catalog"


def overrides_dir(workspace: Path) -> Path:
    return workspace / "overrides"


def cache_dir(workspace: Path) -> Path:
    return state_dir(workspace) / "cache"


def logs_dir(workspace: Path) -> Path:
    return state_dir(workspace) / "logs"


def manifests_dir(workspace: Path) -> Path:
    return state_dir(workspace) / "manifests"


def workspace_label(workspace: Path) -> str:
    """Stable display name for reports that should survive path sync."""
    return workspace.name or "."


def workspace_relative_path(workspace: Path, path: Path | str | None) -> str:
    """Return a portable path for artifacts stored inside a workspace.

    Generated manifests are often read after the same workspace has been synced
    to another machine. Absolute paths like /Users/... or /home/... make those
    artifacts look stale, so anything under the workspace is serialized as a
    POSIX-style path relative to the workspace root.
    """
    if path in (None, ""):
        return ""
    text = str(path)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        return candidate.as_posix() if isinstance(path, Path) else text
    try:
        workspace_root = workspace.expanduser().resolve()
        resolved = candidate.resolve()
        if resolved == workspace_root:
            return "."
        return resolved.relative_to(workspace_root).as_posix()
    except (OSError, ValueError):
        pass
    parts = candidate.parts
    if workspace.name and workspace.name in parts:
        index = len(parts) - 1 - list(reversed(parts)).index(workspace.name)
        suffix = parts[index + 1:]
        if not suffix:
            return "."
        if suffix[0] in WORKSPACE_PORTABLE_TOP_LEVELS:
            return Path(*suffix).as_posix()
    return text


def portable_workspace_data(workspace: Path, value: Any) -> Any:
    """Recursively convert workspace-local paths in JSON-like data."""
    if isinstance(value, Path):
        return workspace_relative_path(workspace, value)
    if isinstance(value, str):
        return workspace_relative_path(workspace, value)
    if isinstance(value, list):
        return [portable_workspace_data(workspace, item) for item in value]
    if isinstance(value, tuple):
        return [portable_workspace_data(workspace, item) for item in value]
    if isinstance(value, dict):
        return {
            key: portable_workspace_data(workspace, item)
            for key, item in value.items()
        }
    return value


def workspace_dirs(workspace: Path) -> dict[str, Path]:
    return {
        "raw": raw_dir(workspace),
        "data": data_dir(workspace),
        "catalog": catalog_dir(workspace),
        "state": state_dir(workspace),
        "cache": cache_dir(workspace),
        "logs": logs_dir(workspace),
        "manifests": manifests_dir(workspace),
    }


def ensure_workspace_dirs(workspace: Path) -> None:
    for path in workspace_dirs(workspace).values():
        path.mkdir(parents=True, exist_ok=True)
    for subdir in (
        "arxiv",
        "openalex",
        "paperlists",
        "crossref",
        "dblp",
        "acl_anthology",
        "europepmc",
        "pubmed",
        "openreview",
        "semanticscholar",
    ):
        (raw_dir(workspace) / subdir).mkdir(parents=True, exist_ok=True)


def load_topic_config(workspace: Path) -> dict[str, Any]:
    topic = load_yaml(workspace / "topic.yaml", {})
    topic.setdefault("topic_id", workspace.name)
    topic.setdefault("min_year", None)
    topic.setdefault("search_hints", [])
    topic.setdefault("discriminator_terms", [])
    topic.setdefault("search_keyword_text", "")
    topic.setdefault("judge_examples", {"in_scope": [], "out_of_scope": []})
    topic.setdefault("tag_rules", [])
    return topic


def load_sources_config(workspace: Path) -> dict[str, Any]:
    sources = load_yaml(workspace / "sources.yaml", {})
    sources.setdefault("sources", {})
    return sources


def init_workspace(workspace: Path, topic_id: str, template: Path | None = None, force: bool = False) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    ensure_workspace_dirs(workspace)

    if template:
        template = template.resolve()
        for name in ("topic.yaml", "sources.yaml"):
            src = template / name
            dst = workspace / name
            if src.exists() and (force or not dst.exists()):
                shutil.copy2(src, dst)

    topic_path = workspace / "topic.yaml"
    sources_path = workspace / "sources.yaml"
    if force or not topic_path.exists():
        write_yaml(topic_path, {
            "topic_id": topic_id,
            "name": topic_id,
            "description": "",
            "min_year": None,
            "search_hints": [],
            "discriminator_terms": [],
            "search_keyword_text": "",
            "judge_examples": {"in_scope": [], "out_of_scope": []},
        })
    if force or not sources_path.exists():
        write_yaml(sources_path, {
            "sources": {
                "arxiv": {"enabled": False, "type": "arxiv", "queries": [], "max_results": 25},
            }
        })

    return {
        "workspace": str(workspace),
        "topic": str(topic_path),
        "sources": str(sources_path),
    }


def resolve_template(template: str | None) -> Path | None:
    if not template:
        return None
    path = Path(template)
    if path.exists():
        return path
    built_in = PROJECT_ROOT / template
    if built_in.exists():
        return built_in
    built_in_examples = PROJECT_ROOT / "examples" / template
    if built_in_examples.exists():
        return built_in_examples
    raise FileNotFoundError(f"未找到模板目录：{template}")

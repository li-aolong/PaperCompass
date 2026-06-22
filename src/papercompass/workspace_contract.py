from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc
from typing import Any

from .config import PROJECT_ROOT, cache_dir, raw_dir, workspace_label
from .text import clean_text, slugify


LIBRARY_NAME_RE = re.compile(
    r"^(?P<topic_id>[a-z0-9\u4e00-\u9fff]+(?:-[a-z0-9\u4e00-\u9fff]+)*)"
    r"--(?P<min_year>(?:19|20)\d{2})plus"
    r"(?:--(?P<model_variant>[a-z0-9]+(?:-[a-z0-9]+)*))?"
    r"(?:--run-(?P<run_date>\d{8}))?$"
)


@dataclass(frozen=True)
class LibraryNameParts:
    name: str
    topic_id: str
    min_year: int
    model_variant: str = ""
    run_date: str = ""


@dataclass(frozen=True)
class LibraryNameValidation:
    name: str
    valid: bool
    parts: LibraryNameParts | None = None
    reason: str = ""
    expected_format: str = "<topic_id>--<min_year>plus[--<model_variant>][--run-<YYYYMMDD>]"


@dataclass(frozen=True)
class AutoWorkspaceResolution:
    workspace: Path
    library_name: str
    topic_id: str
    min_year: int
    generated: bool
    validation: LibraryNameValidation

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "library_name": self.library_name,
            "topic_id": self.topic_id,
            "min_year": self.min_year,
            "generated": self.generated,
            "valid_name": self.validation.valid,
            "expected_format": self.validation.expected_format,
        }


def normalize_topic_id(value: str, *, max_len: int = 72) -> str:
    if not clean_text(value):
        return ""
    return slugify(value, max_len=max_len)


def normalize_model_variant(value: str | None) -> str:
    if not value:
        return ""
    normalized = slugify(value, max_len=48)
    if normalized in {"deepseek", "ds"}:
        raise ValueError("model_variant 不能写泛泛的 deepseek/ds；请使用 ds-v4-flash 或 ds-v4-pro")
    return normalized


def make_library_name(
    topic_id: str,
    min_year: int,
    *,
    model_variant: str | None = None,
    run_date: str | None = None,
) -> str:
    normalized_topic = normalize_topic_id(topic_id)
    if not normalized_topic:
        raise ValueError("topic_id 不能为空")
    if min_year < 1900 or min_year > 2099:
        raise ValueError(f"min_year 不合法：{min_year}")
    parts = [normalized_topic, f"{min_year}plus"]
    normalized_model = normalize_model_variant(model_variant)
    if normalized_model:
        parts.append(normalized_model)
    if run_date:
        if not re.fullmatch(r"\d{8}", run_date):
            raise ValueError("run_date 必须是 YYYYMMDD")
        parts.append(f"run-{run_date}")
    return "--".join(parts)


def parse_library_name(name: str) -> LibraryNameParts | None:
    match = LIBRARY_NAME_RE.fullmatch(clean_text(name))
    if not match:
        return None
    return LibraryNameParts(
        name=name,
        topic_id=match.group("topic_id"),
        min_year=int(match.group("min_year")),
        model_variant=match.group("model_variant") or "",
        run_date=match.group("run_date") or "",
    )


def validate_library_name(
    name: str,
    *,
    topic_id: str | None = None,
    min_year: int | None = None,
) -> LibraryNameValidation:
    parts = parse_library_name(name)
    if parts is None:
        return LibraryNameValidation(
            name=name,
            valid=False,
            reason=(
                "workspace 名称必须是 "
                "<topic_id>--<min_year>plus[--<model_variant>][--run-<YYYYMMDD>]"
            ),
        )
    if parts.model_variant in {"deepseek", "ds"}:
        return LibraryNameValidation(
            name=name,
            valid=False,
            parts=parts,
            reason="model_variant 不能写泛泛的 deepseek/ds；请使用 ds-v4-flash 或 ds-v4-pro",
        )
    if topic_id and parts.topic_id != normalize_topic_id(topic_id):
        return LibraryNameValidation(
            name=name,
            valid=False,
            parts=parts,
            reason=f"topic_id 不一致：名称中是 {parts.topic_id}，请求的是 {normalize_topic_id(topic_id)}",
        )
    if min_year and parts.min_year != min_year:
        return LibraryNameValidation(
            name=name,
            valid=False,
            parts=parts,
            reason=f"min_year 不一致：名称中是 {parts.min_year}，请求的是 {min_year}",
        )
    return LibraryNameValidation(name=name, valid=True, parts=parts)


def resolve_auto_workspace(
    *,
    direction: str,
    min_year: int | None,
    workspace: Path | None = None,
    workspace_name: str | None = None,
    workspaces_root: Path | None = None,
    topic_id: str | None = None,
    model_variant: str | None = None,
) -> AutoWorkspaceResolution:
    if workspace and workspace_name:
        raise ValueError("--workspace 和 --workspace-name 只能二选一")
    if workspace_name and model_variant:
        raise ValueError("--workspace-name 已经是完整库名，不能再同时传 --model-variant")

    root = (workspaces_root or (PROJECT_ROOT / "workspaces")).expanduser().resolve()
    requested_topic = normalize_topic_id(topic_id or "")

    if workspace is not None:
        resolved_workspace = workspace.expanduser().resolve()
        validation = validate_library_name(
            resolved_workspace.name,
            topic_id=requested_topic or None,
            min_year=min_year,
        )
        if not validation.valid:
            raise ValueError(f"非规范 workspace 名称：{resolved_workspace.name}。{validation.reason}")
        assert validation.parts is not None
        return AutoWorkspaceResolution(
            workspace=resolved_workspace,
            library_name=resolved_workspace.name,
            topic_id=validation.parts.topic_id,
            min_year=validation.parts.min_year,
            generated=False,
            validation=validation,
        )

    if workspace_name:
        validation = validate_library_name(
            workspace_name,
            topic_id=requested_topic or None,
            min_year=min_year,
        )
        if not validation.valid:
            raise ValueError(f"非规范 workspace name：{workspace_name}。{validation.reason}")
        assert validation.parts is not None
        return AutoWorkspaceResolution(
            workspace=(root / workspace_name).resolve(),
            library_name=workspace_name,
            topic_id=validation.parts.topic_id,
            min_year=validation.parts.min_year,
            generated=False,
            validation=validation,
        )

    if not min_year:
        raise ValueError("自动生成 workspace 名称时必须显式提供 --min-year")
    generated_topic = requested_topic or normalize_topic_id(direction)
    library_name = make_library_name(
        generated_topic,
        min_year,
        model_variant=model_variant,
    )
    validation = validate_library_name(library_name, topic_id=generated_topic, min_year=min_year)
    return AutoWorkspaceResolution(
        workspace=(root / library_name).resolve(),
        library_name=library_name,
        topic_id=generated_topic,
        min_year=min_year,
        generated=True,
        validation=validation,
    )


def workspace_contract_summary(workspace: Path, topic: dict[str, Any] | None = None) -> dict[str, Any]:
    topic = topic or {}
    validation = validate_library_name(workspace.name)
    parts = validation.parts
    topic_id = clean_text(topic.get("topic_id")) or (parts.topic_id if parts else workspace.name)
    topic_min_year = topic.get("min_year")
    try:
        min_year = int(topic_min_year) if topic_min_year else (parts.min_year if parts else None)
    except (TypeError, ValueError):
        min_year = parts.min_year if parts else None
    expected_name = ""
    if min_year:
        expected_name = make_library_name(
            topic_id,
            min_year,
            model_variant=parts.model_variant if parts else None,
            run_date=parts.run_date if parts else None,
        )
    return {
        "library_name": workspace.name,
        "valid_name": validation.valid,
        "validation_reason": validation.reason,
        "expected_format": validation.expected_format,
        "topic_id": topic_id,
        "min_year": min_year,
        "expected_name": expected_name,
        "name_matches_topic": bool(parts and parts.topic_id == normalize_topic_id(topic_id)),
        "raw_policy": {
            "directory": ".raw",
            "meaning": "原始候选证据，可用于审计、离线重建和召回排查",
            "default_export": "excluded",
            "include_flag": "--include-raw",
        },
    }


def export_workspace(
    workspace: Path,
    output: Path,
    *,
    include_raw: bool = False,
    include_cache: bool = False,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    output = output.expanduser().resolve()
    if not workspace.exists():
        raise FileNotFoundError(f"workspace 不存在：{workspace}")
    output.parent.mkdir(parents=True, exist_ok=True)

    excluded_roots: list[Path] = []
    if not include_raw:
        excluded_roots.append(raw_dir(workspace))
    if not include_cache:
        excluded_roots.append(cache_dir(workspace))

    written = 0
    skipped: list[str] = []
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(workspace.rglob("*")):
            if path == output or not path.is_file():
                continue
            if any(path == root or root in path.parents for root in excluded_roots):
                rel = path.relative_to(workspace).as_posix()
                top = rel.split("/", 1)[0]
                if top not in skipped:
                    skipped.append(top)
                continue
            archive.write(path, arcname=f"{workspace.name}/{path.relative_to(workspace).as_posix()}")
            written += 1
        manifest = {
            "schema_version": "papercompass.export.v1",
            "created_at": datetime.now(UTC).isoformat(),
            "workspace": workspace_label(workspace),
            "library_name": workspace.name,
            "include_raw": include_raw,
            "include_cache": include_cache,
            "excluded_by_default": [".raw", ".papercompass/cache"],
            "files_written": written,
        }
        archive.writestr(
            f"{workspace.name}/papercompass-export.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    return {
        "workspace": str(workspace),
        "output": str(output),
        "files_written": written,
        "include_raw": include_raw,
        "include_cache": include_cache,
        "skipped_roots": skipped,
    }

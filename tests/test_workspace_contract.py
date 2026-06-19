from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from papercompass.workspace_contract import (
    export_workspace,
    make_library_name,
    resolve_auto_workspace,
    validate_library_name,
)


def test_make_and_validate_library_name() -> None:
    name = make_library_name("Implicit Chain-of-Thought", 2022)
    assert name == "implicit-chain-of-thought--2022plus"

    validation = validate_library_name(name, topic_id="implicit-chain-of-thought", min_year=2022)
    assert validation.valid is True
    assert validation.parts is not None
    assert validation.parts.topic_id == "implicit-chain-of-thought"
    assert validation.parts.min_year == 2022


def test_model_variant_must_not_be_generic_deepseek() -> None:
    with pytest.raises(ValueError, match="ds-v4-flash"):
        make_library_name("small lm agents", 2022, model_variant="deepseek")

    validation = validate_library_name("small-lm-agents--2022plus--deepseek")
    assert validation.valid is False
    assert "ds-v4-flash" in validation.reason


def test_resolve_auto_workspace_generates_canonical_name(tmp_path: Path) -> None:
    result = resolve_auto_workspace(
        direction="Small language models and agents",
        min_year=2022,
        workspaces_root=tmp_path,
    )

    assert result.generated is True
    assert result.workspace == tmp_path / "small-language-models-and-agents--2022plus"
    assert result.topic_id == "small-language-models-and-agents"
    assert result.min_year == 2022


def test_resolve_auto_workspace_validates_explicit_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="非规范 workspace 名称"):
        resolve_auto_workspace(
            direction="anything",
            min_year=2022,
            workspace=tmp_path / "bad-name",
        )

    result = resolve_auto_workspace(
        direction="anything",
        min_year=None,
        workspace=tmp_path / "implicit-chain-of-thought--2022plus",
    )
    assert result.topic_id == "implicit-chain-of-thought"
    assert result.min_year == 2022


def test_export_workspace_excludes_raw_and_cache_by_default(tmp_path: Path) -> None:
    ws = tmp_path / "topic--2022plus"
    (ws / ".raw" / "manual").mkdir(parents=True)
    (ws / ".papercompass" / "cache" / "discovery").mkdir(parents=True)
    (ws / ".papercompass" / "auto").mkdir(parents=True)
    (ws / "data").mkdir(parents=True)
    (ws / "catalog").mkdir(parents=True)
    (ws / ".raw" / "manual" / "paper.jsonl").write_text("{}\n", encoding="utf-8")
    (ws / ".papercompass" / "cache" / "discovery" / "cache.json").write_text("{}", encoding="utf-8")
    (ws / ".papercompass" / "auto" / "final_summary.json").write_text("{}", encoding="utf-8")
    (ws / "data" / "papers.jsonl").write_text("{}\n", encoding="utf-8")
    (ws / "catalog" / "manifest.json").write_text("{}", encoding="utf-8")

    out = tmp_path / "export.zip"
    result = export_workspace(ws, out)

    assert result["include_raw"] is False
    assert result["include_cache"] is False
    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        assert "topic--2022plus/data/papers.jsonl" in names
        assert "topic--2022plus/catalog/manifest.json" in names
        assert "topic--2022plus/.papercompass/auto/final_summary.json" in names
        assert "topic--2022plus/papercompass-export.json" in names
        assert "topic--2022plus/.raw/manual/paper.jsonl" not in names
        assert "topic--2022plus/.papercompass/cache/discovery/cache.json" not in names
        manifest = json.loads(archive.read("topic--2022plus/papercompass-export.json"))
        assert manifest["include_raw"] is False
        assert manifest["excluded_by_default"] == [".raw", ".papercompass/cache"]


def test_export_workspace_can_include_raw_when_requested(tmp_path: Path) -> None:
    ws = tmp_path / "topic--2022plus"
    (ws / ".raw" / "manual").mkdir(parents=True)
    (ws / ".raw" / "manual" / "paper.jsonl").write_text("{}\n", encoding="utf-8")

    out = tmp_path / "export.zip"
    export_workspace(ws, out, include_raw=True)

    with zipfile.ZipFile(out) as archive:
        assert "topic--2022plus/.raw/manual/paper.jsonl" in set(archive.namelist())

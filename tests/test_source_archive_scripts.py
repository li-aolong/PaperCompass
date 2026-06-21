from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest


def _load_make_source_zip_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "make_source_zip.py"
    spec = importlib.util.spec_from_file_location("make_source_zip_script", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_make_source_zip_writes_release_manifest(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src" / "papercompass").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "scripts").mkdir()
    (root / "src" / "papercompass" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_placeholder.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "README.md").write_text("PaperCompass", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nversion = \"0.1.2\"\n", encoding="utf-8")
    output = tmp_path / "PaperCompass_source.zip"

    module = _load_make_source_zip_module()
    result = module.make_source_zip(root, output)

    manifest_path = Path(result["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    assert manifest["schema_version"] == "papercompass.source_archive_manifest.v1"
    assert manifest["version"] == "0.1.2"
    assert manifest["sha256"] == digest
    assert manifest["file_count"] == result["files"]
    assert "python scripts/check_source_archive.py --strict PaperCompass_source.zip" in manifest["release_gate"]


def test_make_source_zip_skips_symlinks_to_paths_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    package = root / "src" / "papercompass"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside_secret.py"
    outside.write_text("SECRET = 'outside'\n", encoding="utf-8")
    leak = package / "leak.py"
    try:
        leak.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unsupported: {exc}")
    output = tmp_path / "PaperCompass_source.zip"

    module = _load_make_source_zip_module()
    module.make_source_zip(root, output)

    with zipfile.ZipFile(output) as zf:
        names = set(zf.namelist())
    assert "src/papercompass/__init__.py" in names
    assert "src/papercompass/leak.py" not in names


@pytest.mark.parametrize("entry", ["../outside.py", "safe/../../outside.py"])
def test_source_archive_safe_extract_rejects_zip_slip(tmp_path: Path, entry: str) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "test_source_archive.py"
    spec = importlib.util.spec_from_file_location("test_source_archive_script", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(entry, "bad")

    with zipfile.ZipFile(archive) as zf:
        try:
            module.safe_extract(zf, tmp_path / "extract")
        except RuntimeError as exc:
            assert "unsafe archive path" in str(exc)
        else:
            raise AssertionError("safe_extract should reject path traversal")

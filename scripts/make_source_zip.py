from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile


INCLUDE_TOP_LEVEL = {
    ".github",
    "src",
    "tests",
    "docs",
    "templates",
    "skills",
    "scripts",
}
INCLUDE_FILES = {
    "README.md",
    "AGENT_ENTRY.md",
    "pyproject.toml",
    "MANIFEST.in",
    "uv.lock",
}
DENY_PARTS = {
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
DENY_SUFFIXES = {".pyc", ".pyo", ".key", ".pem", ".egg-info"}
RELEASE_GATE_COMMANDS = [
    "pytest -q",
    "python scripts/make_source_zip.py PaperCompass_source.zip",
    "python scripts/check_source_archive.py PaperCompass_source.zip",
    "python scripts/check_source_archive.py --strict PaperCompass_source.zip",
    "python scripts/test_source_archive.py PaperCompass_source.zip",
]


def should_include(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = rel.parts
    if not parts:
        return False
    if set(parts) & DENY_PARTS:
        return False
    if any(part.endswith(".egg-info") for part in parts):
        return False
    if path.suffix in DENY_SUFFIXES:
        return False
    if len(parts) == 1:
        return parts[0] in INCLUDE_FILES
    return parts[0] in INCLUDE_TOP_LEVEL


def make_source_zip(root: Path, output: Path) -> dict[str, int | str]:
    root = root.resolve()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if not path.is_file():
                continue
            if resolved == output:
                continue
            if not should_include(path, root):
                continue
            archive.write(path, arcname=path.relative_to(root).as_posix())
            count += 1
    manifest_path = write_archive_manifest(root, output, file_count=count)
    return {"output": str(output), "files": count, "manifest": str(manifest_path)}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_version(root: Path) -> str:
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return ""
    in_project = False
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if in_project and stripped.startswith("["):
            break
        if in_project:
            match = re.match(r"version\s*=\s*['\"]([^'\"]+)['\"]", stripped)
            if match:
                return match.group(1)
    return ""


def _git_commit(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def write_archive_manifest(root: Path, output: Path, *, file_count: int) -> Path:
    manifest_path = output.with_name(f"{output.name}.manifest.json")
    manifest = {
        "schema_version": "papercompass.source_archive_manifest.v1",
        "version": _project_version(root),
        "git_commit": _git_commit(root),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "archive": output.name,
        "file_count": file_count,
        "sha256": _sha256_file(output),
        "release_gate": RELEASE_GATE_COMMANDS,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a sanitized PaperCompass source archive.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = make_source_zip(args.root, args.output)
    print(result)


if __name__ == "__main__":
    main()

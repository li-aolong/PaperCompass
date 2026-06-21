from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile


def safe_extract(source_zip: zipfile.ZipFile, target: Path) -> None:
    target = target.resolve()
    for member in source_zip.infolist():
        normalized_name = member.filename.replace("\\", "/")
        if normalized_name.startswith(("/", "../")) or re.match(r"^[A-Za-z]:/", normalized_name):
            raise RuntimeError(f"unsafe archive path: {member.filename}")
        destination = (target / member.filename).resolve()
        try:
            destination.relative_to(target)
        except ValueError as exc:
            raise RuntimeError(f"unsafe archive path: {member.filename}") from exc
        source_zip.extract(member, target)


def test_source_archive(archive: Path) -> dict[str, object]:
    archive = archive.resolve()
    extract_dir = Path(tempfile.mkdtemp(prefix="papercompass-archive-test-"))
    with zipfile.ZipFile(archive) as source_zip:
        safe_extract(source_zip, extract_dir)
    command = [sys.executable, "-m", "pytest", "-q"]
    completed = subprocess.run(command, cwd=extract_dir, check=False)
    return {
        "schema_version": "papercompass.source_archive_test.v1",
        "archive": str(archive),
        "extracted_to": str(extract_dir),
        "command": command,
        "returncode": completed.returncode,
        "status": "passed" if completed.returncode == 0 else "failed",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract a PaperCompass source archive and run its pytest suite.")
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    result = test_source_archive(args.archive)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

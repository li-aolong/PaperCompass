from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Check a PaperCompass source archive for local files and secrets.")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--strict", action="store_true", help="Treat skipped large text files as failures.")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from papercompass.doctor import doctor_archive

    result = doctor_archive(args.archive, strict=args.strict)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

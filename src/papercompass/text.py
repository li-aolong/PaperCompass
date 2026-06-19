from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


STOPWORDS = {
    "the", "and", "for", "with", "using", "based", "from", "into", "towards", "toward",
    "this", "that", "than", "are", "can", "via", "its", "our", "your", "their", "a", "an",
    "of", "in", "on", "to", "by", "as", "is", "at", "or", "be",
}

DERIVED_TAG_PREFIXES = ("confidence:", "review:")


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())


def normalize_title(title: str) -> str:
    title = clean_text(title).lower()
    title = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", title)
    return " ".join(title.split())


def slugify(text: str, max_len: int = 84) -> str:
    text = clean_text(text).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return (text[:max_len].strip("-") or "untitled")


def short_hash(text: str, length: int = 10) -> str:
    return hashlib.sha1(clean_text(text).encode("utf-8")).hexdigest()[:length]


def parse_year(value: Any) -> int | None:
    match = re.search(r"(19[0-9]{2}|20[0-9]{2})", str(value or ""))
    return int(match.group(1)) if match else None


def as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [clean_text(v) for v in value if clean_text(v)]
    if isinstance(value, tuple | set):
        return [clean_text(v) for v in value if clean_text(v)]
    return [clean_text(value)]


def is_derived_tag(tag: Any) -> bool:
    tag_text = clean_text(tag).lower()
    return any(tag_text.startswith(prefix) for prefix in DERIVED_TAG_PREFIXES)


def strip_derived_tags(tags: Iterable[Any]) -> list[str]:
    return sorted({clean_text(tag) for tag in tags if clean_text(tag) and not is_derived_tag(tag)})


def split_authors(value: Any) -> list[str]:
    if isinstance(value, list):
        return [clean_text(v.get("name") if isinstance(v, dict) else v) for v in value if clean_text(v)]
    text = clean_text(value)
    if not text:
        return []
    if ";" in text:
        return [part.strip() for part in text.split(";") if part.strip()]
    if "," in text and " et al" not in text.lower():
        return [part.strip() for part in text.split(",") if part.strip()]
    return [text]


def format_author_list(value: Any, max_without_et_al: int = 10) -> str:
    """Display authors without et al. unless the available list is very long."""
    text = clean_text(value)
    if not text:
        return ""
    authors = split_authors(text)
    if len(authors) > max_without_et_al:
        return "; ".join(authors[:max_without_et_al]) + " et al."
    if " et al" in text.lower() and len(authors) <= max_without_et_al:
        return re.sub(r"\s*,?\s+et\s+al\.?$", "", text, flags=re.IGNORECASE)
    return "; ".join(authors) if len(authors) > 1 else text


def compact_authors(value: Any) -> str:
    authors = split_authors(value)
    return "; ".join(authors)


def title_tokens(title: str) -> list[str]:
    tokens = normalize_title(title).split()
    return [token for token in tokens if len(token) >= 3 and token not in STOPWORDS]


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def iter_jsonl(path: Path) -> Iterable[Any]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL line: {exc}") from exc


def write_jsonl(path: Path, items: Iterable[Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count

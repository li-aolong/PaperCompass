from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import catalog_dir, data_dir, workspace_relative_path
from .text import (
    _fsync_dir,
    as_list,
    atomic_write_text,
    clean_text,
    format_author_list,
    normalize_title,
    read_json,
    title_tokens,
    workspace_lock,
    write_json,
)


def rel(workspace: Path, path: Path) -> str:
    return str(path.relative_to(workspace))


def alias_values(value: str, kind: str) -> list[str]:
    value = clean_text(value)
    if not value:
        return []
    values = [value, value.lower()]
    if kind == "doi":
        values.append(value.removeprefix("https://doi.org/").removeprefix("http://doi.org/").lower())
    if kind == "arxiv":
        values.append(re.sub(r"v\d+$", "", value.lower()))
    return sorted(set(v for v in values if v))


def build_aliases(item: dict[str, Any], record: dict[str, Any]) -> list[str]:
    aliases = [
        f"paper_key:{record['paper_key']}",
        f"title:{record['normalized_title']}",
    ]
    ids = item.get("ids") or {}
    for field, prefix in [
        ("doi", "doi"),
        ("arxiv", "arxiv"),
        ("semantic_scholar", "semantic_scholar"),
        ("acl", "acl"),
        ("openreview", "openreview"),
        ("openalex", "openalex"),
        ("dblp", "dblp"),
    ]:
        for value in alias_values(ids.get(field) or item.get(f"{field}_id") or item.get(field), field):
            aliases.append(f"{prefix}:{value}")
    for url_key in ("url", "pdf_url"):
        if item.get(url_key):
            aliases.append(f"{url_key}:{item[url_key]}")
    return sorted(set(alias.lower() for alias in aliases if alias and not alias.endswith(":")))


def compact_record(workspace: Path, item: dict[str, Any], md_path: Path, json_path: Path) -> dict[str, Any]:
    ids = item.get("ids") or {}
    urls = item.get("urls") or {}
    return {
        "paper_key": item.get("paper_key"),
        "title": item.get("title", ""),
        "normalized_title": normalize_title(item.get("title", "")),
        "year": item.get("year", ""),
        "venue": item.get("venue", ""),
        "authors": format_author_list(item.get("authors", "")),
        "max_citation": item.get("max_citation", 0),
        "citation_count": item.get("citation_count", 0),
        "gs_citation": item.get("gs_citation", 0),
        "doi": ids.get("doi") or item.get("doi", ""),
        "arxiv_id": ids.get("arxiv") or item.get("arxiv_id", ""),
        "semantic_scholar_id": ids.get("semantic_scholar") or item.get("semantic_scholar_id", ""),
        "acl_id": ids.get("acl") or item.get("acl_id", ""),
        "url": urls.get("landing") or item.get("url", ""),
        "pdf_url": urls.get("pdf") or item.get("pdf_url", ""),
        "sources": as_list(item.get("sources")),
        "keyword_hits": as_list(item.get("keyword_hits")),
        "tags": as_list(item.get("tags")),
        "system_tags": as_list(item.get("system_tags")),
        "markdown_path": rel(workspace, md_path),
        "json_path": rel(workspace, json_path),
    }


def pointer(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "paper_key": record["paper_key"],
        "title": record["title"],
        "year": record["year"],
        "venue": record["venue"],
        "markdown_path": record["markdown_path"],
        "json_path": record["json_path"],
    }


def paper_markdown(item: dict[str, Any], record: dict[str, Any], aliases: list[str]) -> str:
    lines = [
        "---",
        f"paper_key: {record['paper_key']}",
        f"title: {json.dumps(record['title'], ensure_ascii=False)}",
        f"year: {record['year']}",
        f"venue: {json.dumps(record['venue'], ensure_ascii=False)}",
        f"doi: {record['doi']}",
        f"arxiv_id: {record['arxiv_id']}",
        f"semantic_scholar_id: {record['semantic_scholar_id']}",
        f"acl_id: {record['acl_id']}",
        f"tags: {json.dumps(record['tags'], ensure_ascii=False)}",
        f"max_citation: {record['max_citation']}",
        "---",
        "",
        f"# {record['title']}",
        "",
        f"- 目录键：`{record['paper_key']}`",
        f"- 年份：{record['year'] or 'N/A'}",
        f"- Venue：{record['venue'] or 'N/A'}",
        f"- 作者：{record['authors'] or 'N/A'}",
        f"- 引用：max={record['max_citation']}，Semantic Scholar={record['citation_count']}，Google Scholar={record['gs_citation']}",
    ]
    for label, key in [
        ("DOI", "doi"),
        ("arXiv", "arxiv_id"),
        ("Semantic Scholar ID", "semantic_scholar_id"),
        ("ACL Anthology ID", "acl_id"),
        ("链接", "url"),
        ("PDF", "pdf_url"),
    ]:
        if record.get(key):
            lines.append(f"- {label}：{record[key]}")
    lines.extend([
        f"- 来源：{', '.join(record['sources']) or 'N/A'}",
        f"- 命中词：{', '.join(record['keyword_hits']) or 'N/A'}",
        f"- 标签：{', '.join(record['tags']) or 'N/A'}",
        f"- 系统标记：{', '.join(record['system_tags']) or 'N/A'}",
        "",
        "## 摘要",
        "",
        clean_text(item.get("abstract")) or clean_text(item.get("summary")) or "N/A",
        "",
        "## 检索别名",
        "",
    ])
    lines.extend(f"- `{alias}`" for alias in aliases)
    lines.append("")
    return "\n".join(lines)


def sort_pointer(item: dict[str, Any]) -> tuple[int, str]:
    year = item.get("year")
    try:
        year_value = int(year)
    except (TypeError, ValueError):
        year_value = 0
    return (-year_value, normalize_title(item.get("title", "")))


def build_catalog(workspace: Path, source: Path | None = None) -> dict[str, Any]:
    with workspace_lock(workspace):
        return _build_catalog_unlocked(workspace, source)


def _build_catalog_unlocked(workspace: Path, source: Path | None = None) -> dict[str, Any]:
    source = source or data_dir(workspace) / "papers.json"
    items = read_json(source, [])
    if not isinstance(items, list):
        raise ValueError(f"catalog source is not a list: {source}")

    catalog = catalog_dir(workspace)
    build_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    tmp_dir = workspace / f".catalog.tmp.{build_id}"
    backup_dir = workspace / f".catalog.prev.{build_id}"
    tmp_papers = tmp_dir / "papers"
    tmp_index = tmp_dir / "index"
    tmp_papers.mkdir(parents=True)
    tmp_index.mkdir(parents=True)

    records: list[dict[str, Any]] = []
    alias_lookup: dict[str, dict[str, Any]] = {}
    title_lookup: dict[str, dict[str, Any]] = {}
    by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_keyword: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_tag: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_venue: dict[str, list[dict[str, Any]]] = defaultdict(list)
    title_word_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    title_lines: list[str] = []

    for item in items:
        year = clean_text(item.get("year")) or "unknown"
        key = clean_text(item.get("paper_key"))
        md_path = tmp_papers / year / f"{key}.md"
        json_path = tmp_papers / year / f"{key}.json"
        md_path.parent.mkdir(parents=True, exist_ok=True)

        final_md = catalog / md_path.relative_to(tmp_dir)
        final_json = catalog / json_path.relative_to(tmp_dir)
        record = compact_record(workspace, item, final_md, final_json)
        aliases = build_aliases(item, record)
        per_paper = dict(item)
        per_paper["retrieval"] = record
        per_paper["aliases"] = aliases

        atomic_write_text(md_path, paper_markdown(item, record, aliases))
        write_json(json_path, per_paper)
        records.append(record)
        p = pointer(record)

        title_lookup[record["normalized_title"]] = p
        for alias in aliases:
            alias_lookup[alias] = p
        by_year[str(record["year"])].append(p)
        for keyword in record["keyword_hits"] or ["__no_keyword__"]:
            by_keyword[keyword].append(p)
        for tag in record["tags"] or ["__no_tag__"]:
            by_tag[tag].append(p)
        for source_name in record["sources"] or ["__unknown_source__"]:
            by_source[source_name].append(p)
        by_venue[record["venue"] or "N/A"].append(p)
        for token in title_tokens(record["title"]):
            title_word_index[token].append(p)
        title_lines.append(f"- {record['year']} | {record['title']} | `{record['paper_key']}` | `{record['markdown_path']}`")

    records.sort(key=lambda r: sort_pointer(r))
    for group in (by_year, by_keyword, by_tag, by_source, by_venue, title_word_index):
        for key in list(group.keys()):
            group[key].sort(key=sort_pointer)

    write_json(tmp_index / "id_lookup.json", {r["paper_key"]: r for r in records})
    write_json(tmp_index / "title_lookup.json", title_lookup)
    write_json(tmp_index / "alias_lookup.json", alias_lookup)
    write_json(tmp_index / "by_year.json", by_year)
    write_json(tmp_index / "by_keyword.json", by_keyword)
    write_json(tmp_index / "by_tag.json", by_tag)
    write_json(tmp_index / "by_source.json", by_source)
    write_json(tmp_index / "by_venue.json", by_venue)
    write_json(tmp_index / "title_word_index.json", title_word_index)
    write_json(tmp_index / "top_cited.json", sorted(records, key=lambda r: int(r.get("max_citation") or 0), reverse=True)[:100])
    atomic_write_text(
        tmp_index / "quick_titles.md",
        "# 论文快速题名索引\n\n" + "\n".join(sorted(title_lines, key=str.lower)) + "\n",
    )

    existing_fulltext = catalog / "fulltext"
    if existing_fulltext.exists():
        shutil.copytree(existing_fulltext, tmp_dir / "fulltext", dirs_exist_ok=True)
    else:
        (tmp_dir / "fulltext").mkdir(parents=True, exist_ok=True)
        write_json(tmp_dir / "fulltext" / "index.json", {})

    manifest = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "source_json": workspace_relative_path(workspace, source),
        "paper_count": len(records),
        "catalog_root": "catalog",
        "primary_indexes": {
            "alias_lookup": "catalog/index/alias_lookup.json",
            "title_lookup": "catalog/index/title_lookup.json",
            "quick_titles": "catalog/index/quick_titles.md",
            "title_word_index": "catalog/index/title_word_index.json",
            "by_tag": "catalog/index/by_tag.json",
            "fulltext_index": "catalog/fulltext/index.json",
        },
        "lookup_order": [
            "已知 DOI/arXiv/Semantic Scholar/ACL/OpenReview/OpenAlex/DBLP ID 时查 index/alias_lookup.json",
            "已知完整题名时规范化后查 index/title_lookup.json",
            "只知道部分题名时查 index/title_word_index.json",
            "按研究方向浏览时查 index/by_tag.json、index/by_keyword.json、index/by_year.json 或 index/by_venue.json",
            "定位到 markdown_path/json_path 后只打开单篇文件",
        ],
    }
    write_json(tmp_dir / "manifest.json", manifest)
    write_catalog_docs(tmp_dir, len(records), workspace_relative_path(workspace, source))

    old_catalog_moved = False
    if catalog.exists():
        catalog.rename(backup_dir)
        _fsync_dir(workspace)
        old_catalog_moved = True
    try:
        tmp_dir.rename(catalog)
        _fsync_dir(workspace)
    except Exception:
        if old_catalog_moved and backup_dir.exists() and not catalog.exists():
            backup_dir.rename(catalog)
            _fsync_dir(workspace)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
            _fsync_dir(workspace)
        raise
    else:
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
            _fsync_dir(workspace)
    return {"catalog": "catalog", "paper_count": len(records), "manifest": "catalog/manifest.json"}


def write_catalog_docs(tmp_dir: Path, paper_count: int, source: str) -> None:
    readme = f"""# 论文检索目录

本目录用于让 LLM 或脚本快速定位具体论文，不需要扫描完整数据文件。

## 推荐检索顺序

1. 已知 DOI、arXiv ID、Semantic Scholar ID、ACL Anthology ID、OpenReview ID、OpenAlex ID、DBLP key：读取 `index/alias_lookup.json`。
2. 已知完整题名：将题名小写，移除标点并合并空白后查 `index/title_lookup.json`。
3. 只知道部分题名：查 `index/title_word_index.json`，取多个词候选的交集。
4. 只知道方向、标签或年份：查 `index/by_tag.json`、`index/by_keyword.json`、`index/by_year.json`、`index/by_venue.json`。
5. 定位后只打开返回的 `markdown_path` 或 `json_path`。

## 文件说明

- `manifest.json`：目录元数据。
- `index/alias_lookup.json`：ID/URL/题名别名到单篇路径的映射。
- `index/title_lookup.json`：规范化题名到单篇路径的映射。
- `index/id_lookup.json`：`paper_key` 到紧凑元数据的映射。
- `index/quick_titles.md`：人读题名索引。
- `index/title_word_index.json`：题名实词倒排索引。
- `index/by_tag.json`：客观标签索引，例如任务线、语言、source 类型。
- `papers/<year>/*.md`：单篇论文卡片。
- `papers/<year>/*.json`：单篇结构化记录。
- `fulltext/index.json`：已按需抓取的全文/PDF 索引。

## 规模

- 论文数：{paper_count}
- 来源：`{source}`
"""
    guide = """# 给 LLM 的论文检索指南

优先读取小索引：

1. `manifest.json`
2. `index/alias_lookup.json`
3. `index/title_lookup.json`
4. `index/title_word_index.json`
5. `index/by_tag.json`
6. `index/by_keyword.json`

拿到 `markdown_path` 或 `json_path` 后，只打开对应单篇文件。需要全文时先查 `fulltext/index.json`。
"""
    atomic_write_text(tmp_dir / "README.md", readme)
    atomic_write_text(tmp_dir / "LLM_RETRIEVAL_GUIDE.md", guide)


def resolve_pointer(workspace: Path, query: str) -> dict[str, Any]:
    catalog = catalog_dir(workspace)
    id_lookup = read_json(catalog / "index" / "id_lookup.json", {})
    alias_lookup = read_json(catalog / "index" / "alias_lookup.json", {})
    title_lookup = read_json(catalog / "index" / "title_lookup.json", {})

    raw = clean_text(query)
    lowered = raw.lower()
    if raw in id_lookup:
        record = id_lookup[raw]
        return {
            "paper_key": raw,
            "markdown_path": record["markdown_path"],
            "json_path": record["json_path"],
            "title": record["title"],
        }
    aliases = [lowered]
    if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", lowered):
        arxiv_base = re.sub(r"v\d+$", "", lowered)
        aliases.append(f"arxiv:{arxiv_base}")
    if re.match(r"^10\.\d{4,9}/", lowered):
        aliases.append(f"doi:{lowered}")
    for alias in aliases:
        if alias in alias_lookup:
            return alias_lookup[alias]
    normalized = normalize_title(raw)
    if normalized in title_lookup:
        return title_lookup[normalized]
    raise KeyError(f"未能定位论文：{query}")


def search_catalog(workspace: Path, query: str, limit: int = 20) -> list[dict[str, Any]]:
    catalog = catalog_dir(workspace)
    id_lookup = read_json(catalog / "index" / "id_lookup.json", {})
    title_word = read_json(catalog / "index" / "title_word_index.json", {})
    tokens = title_tokens(query)
    scores: dict[str, tuple[int, dict[str, Any]]] = {}
    for token in tokens:
        for p in title_word.get(token, []):
            key = p["paper_key"]
            score = scores.get(key, (0, p))[0] + 3
            scores[key] = (score, p)
    lowered = query.lower()
    for key, record in id_lookup.items():
        haystack = " ".join([
            record.get("title", ""),
            record.get("venue", ""),
            " ".join(record.get("keyword_hits", []) or []),
            " ".join(record.get("tags", []) or []),
        ]).lower()
        if lowered in haystack:
            score = scores.get(key, (0, pointer(record)))[0] + 5
            scores[key] = (score, pointer(record))
    ranked = sorted(scores.values(), key=lambda item: (-item[0], sort_pointer(item[1])))
    return [{**p, "score": score} for score, p in ranked[:limit]]

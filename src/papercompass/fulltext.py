from __future__ import annotations

import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .catalog import resolve_pointer
from .config import catalog_dir
from .text import clean_text, read_json, write_json


USER_AGENT = "papercompass-fulltext/0.1"


def strip_arxiv_version(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", clean_text(arxiv_id).lower())


def request_bytes(url: str, timeout: int) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("content-type", "")


def collapse_blank_lines(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def markdown_escape(value: str) -> str:
    return clean_text(value).replace("|", "\\|")


def node_plain_text(node: Tag | NavigableString | None) -> str:
    if node is None:
        return ""
    if isinstance(node, NavigableString):
        return clean_text(str(node))
    for math_node in node.find_all("math"):
        alt = math_node.get("alttext") or math_node.get("alttext-tex") or math_node.get_text(" ", strip=True)
        math_node.replace_with(f" ${clean_text(alt)}$ ")
    return clean_text(node.get_text(" ", strip=True))


def table_to_markdown(table: Tag) -> str:
    caption = node_plain_text(table.find("caption"))
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False) or tr.find_all(["th", "td"])
        row = [markdown_escape(cell.get_text(" ", strip=True)) for cell in cells]
        if any(row):
            rows.append(row)
    if not rows:
        text = node_plain_text(table)
        return f"\n\n{text}\n\n" if text else ""

    max_cols = max(len(row) for row in rows)
    rows = [row + [""] * (max_cols - len(row)) for row in rows]
    lines: list[str] = []
    if caption:
        lines.extend([f"**表格：{caption}**", ""])
    if 1 < max_cols <= 10 and len(rows) <= 80:
        header = rows[0]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join("---" for _ in header) + " |")
        for row in rows[1:]:
            lines.append("| " + " | ".join(row) + " |")
    else:
        lines.append("```text")
        for idx, row in enumerate(rows, start=1):
            lines.append(f"Row {idx}: " + " | ".join(cell for cell in row if cell))
        lines.append("```")
    return "\n\n" + "\n".join(lines) + "\n\n"


def safe_asset_name(src: str, index: int) -> str:
    parsed = urllib.parse.urlparse(src)
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        suffix = ".png"
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(parsed.path).stem).strip("-._") or "figure"
    return f"{index:03d}_{stem}{suffix}"


class MarkdownConverter:
    SKIP_TAGS = {"script", "style", "noscript", "svg"}
    BLOCK_TAGS = {
        "article", "aside", "blockquote", "div", "figure", "footer", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "li", "main", "ol", "p", "pre", "section", "table", "ul",
    }

    def __init__(self, base_url: str, out_dir: Path, timeout: int, download_assets: bool) -> None:
        self.base_url = base_url
        self.out_dir = out_dir
        self.timeout = timeout
        self.download_assets = download_assets
        self.image_index = 0
        self.assets: list[dict[str, Any]] = []

    def convert(self, root: Tag) -> str:
        return collapse_blank_lines("\n".join(self.convert_block(child) for child in root.children))

    def convert_block(self, node: Any) -> str:
        if isinstance(node, NavigableString):
            return clean_text(str(node))
        if not isinstance(node, Tag):
            return ""
        tag = node.name.lower()
        if tag in self.SKIP_TAGS:
            return ""
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = min(int(tag[1]) + 1, 6)
            text = self.convert_inline_children(node)
            return f"\n\n{'#' * level} {text}\n\n" if text else ""
        if tag in {"p", "figcaption"}:
            text = self.convert_inline_children(node)
            return f"\n\n{text}\n\n" if text else ""
        if tag in {"ul", "ol"}:
            ordered = tag == "ol"
            lines = []
            for idx, li in enumerate(node.find_all("li", recursive=False), start=1):
                text = self.convert_inline_children(li)
                if text:
                    lines.append(f"{idx}. {text}" if ordered else f"- {text}")
            return "\n\n" + "\n".join(lines) + "\n\n"
        if tag == "blockquote":
            text = self.convert(node)
            quoted = "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
            return f"\n\n{quoted}\n\n"
        if tag == "pre":
            return f"\n\n```text\n{node.get_text('', strip=False).strip()}\n```\n\n"
        if tag == "table":
            return table_to_markdown(node)
        if tag == "figure":
            return self.convert_figure(node)
        if tag == "img":
            return self.image_to_markdown(node)
        if any(isinstance(child, Tag) and child.name and child.name.lower() in self.BLOCK_TAGS for child in node.children):
            return "\n".join(self.convert_block(child) for child in node.children)
        return self.convert_inline_children(node)

    def convert_figure(self, figure: Tag) -> str:
        parts: list[str] = []
        for table in figure.find_all("table"):
            parts.append(table_to_markdown(table))
        for img in figure.find_all("img"):
            parts.append(self.image_to_markdown(img))
        caption = figure.find("figcaption")
        if caption:
            text = self.convert_inline_children(caption)
            if text:
                parts.append(f"\n\n**图表说明：** {text}\n\n")
        if not parts:
            text = node_plain_text(figure)
            if text:
                parts.append(f"\n\n{text}\n\n")
        return "\n".join(parts)

    def convert_inline_children(self, node: Tag) -> str:
        return clean_text(" ".join(self.convert_inline(child) for child in node.children))

    def convert_inline(self, node: Any) -> str:
        if isinstance(node, NavigableString):
            return clean_text(str(node))
        if not isinstance(node, Tag):
            return ""
        tag = node.name.lower()
        if tag in self.SKIP_TAGS:
            return ""
        if tag == "math":
            alt = node.get("alttext") or node.get("alttext-tex") or node.get_text(" ", strip=True)
            return f"${clean_text(alt)}$"
        if tag == "a":
            text = self.convert_inline_children(node) or clean_text(node.get("href"))
            href = clean_text(node.get("href"))
            if href and not href.startswith("#"):
                return f"[{text}]({urllib.parse.urljoin(self.base_url, href)})"
            return text
        if tag in {"strong", "b"}:
            text = self.convert_inline_children(node)
            return f"**{text}**" if text else ""
        if tag in {"em", "i"}:
            text = self.convert_inline_children(node)
            return f"*{text}*" if text else ""
        if tag == "code":
            text = clean_text(node.get_text(" ", strip=True))
            return f"`{text}`" if text else ""
        if tag == "img":
            return self.image_to_markdown(node).strip()
        if tag == "br":
            return "\n"
        return self.convert_inline_children(node)

    def image_to_markdown(self, img: Tag) -> str:
        src = clean_text(img.get("src"))
        if not src:
            return ""
        self.image_index += 1
        source_url = urllib.parse.urljoin(self.base_url, src)
        alt = clean_text(img.get("alt")) or f"figure {self.image_index}"
        path = source_url
        content_type = None
        if self.download_assets:
            assets_dir = self.out_dir / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            filename = safe_asset_name(source_url, self.image_index)
            local_path = assets_dir / filename
            try:
                payload, content_type = request_bytes(source_url, self.timeout)
                if len(payload) >= 100:
                    local_path.write_bytes(payload)
                    path = f"assets/{filename}"
            except Exception:  # noqa: BLE001
                path = source_url
        record = {"index": self.image_index, "alt": alt, "source_url": source_url, "path": path}
        if content_type:
            record["content_type"] = content_type
        self.assets.append(record)
        return f"\n\n![{alt}]({path})\n\n"


def load_paper(workspace: Path, pointer: dict[str, Any]) -> dict[str, Any]:
    return read_json(workspace / pointer["json_path"], {})


def fetch_ar5iv_markdown(arxiv_id: str, out_dir: Path, timeout: int, download_assets: bool) -> tuple[str, str, list[dict[str, Any]]]:
    arxiv_id = strip_arxiv_version(arxiv_id)
    url = f"https://ar5iv.labs.arxiv.org/html/{urllib.parse.quote(arxiv_id)}"
    payload, content_type = request_bytes(url, timeout)
    text = payload.decode("utf-8", errors="replace")
    if "html" not in content_type.lower() and not text.lstrip().startswith("<"):
        raise ValueError(f"ar5iv 返回的不是 HTML：{content_type}")
    soup = BeautifulSoup(text, "html.parser")
    root = soup.find("article") or soup.find("main") or soup.body or soup
    converter = MarkdownConverter(url, out_dir, timeout, download_assets)
    markdown = converter.convert(root)
    if len(markdown) < 2000:
        raise ValueError(f"ar5iv Markdown 过短：{len(markdown)} chars")
    return markdown, url, converter.assets


def candidate_pdf_urls(paper: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    arxiv_id = clean_text(paper.get("arxiv_id") or (paper.get("ids") or {}).get("arxiv"))
    if arxiv_id:
        urls.append(f"https://arxiv.org/pdf/{strip_arxiv_version(arxiv_id)}.pdf")
    doi = clean_text(paper.get("doi") or (paper.get("ids") or {}).get("doi"))
    match = re.search(r"arxiv\.([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", doi, flags=re.IGNORECASE)
    if match:
        urls.append(f"https://arxiv.org/pdf/{strip_arxiv_version(match.group(1))}.pdf")
    acl_id = clean_text(paper.get("acl_id") or (paper.get("ids") or {}).get("acl"))
    if acl_id:
        urls.append(f"https://aclanthology.org/{acl_id}.pdf")
    urls_dict = paper.get("urls") or {}
    for value in [paper.get("pdf_url"), urls_dict.get("pdf"), paper.get("url"), urls_dict.get("landing")]:
        value = clean_text(value)
        if value and value.lower().endswith(".pdf"):
            urls.append(value)
    landing = clean_text(paper.get("url") or urls_dict.get("landing"))
    if landing:
        parsed = urllib.parse.urlparse(landing)
        if parsed.netloc.endswith("aclanthology.org") and not parsed.path.endswith(".pdf"):
            urls.append(landing.rstrip("/") + ".pdf")
        if parsed.netloc.endswith("openreview.net"):
            query = urllib.parse.parse_qs(parsed.query)
            if query.get("id"):
                urls.append(f"https://openreview.net/pdf?id={urllib.parse.quote(query['id'][0])}")
        if parsed.netloc.endswith("proceedings.mlr.press") and parsed.path.endswith(".html"):
            urls.append(urllib.parse.urlunparse(parsed._replace(path=parsed.path[:-5] + ".pdf")))
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url not in seen:
            unique.append(url)
            seen.add(url)
    return unique


def download_pdf(paper: dict[str, Any], out_dir: Path, timeout: int) -> tuple[Path, str]:
    errors: list[str] = []
    for url in candidate_pdf_urls(paper):
        try:
            payload, content_type = request_bytes(url, timeout)
            if len(payload) < 10 * 1024:
                raise ValueError(f"PDF 文件过小：{len(payload)} bytes")
            if "pdf" not in content_type.lower() and not payload.startswith(b"%PDF"):
                raise ValueError(f"返回内容不像 PDF：{content_type}")
            path = out_dir / "paper.pdf"
            path.write_bytes(payload)
            return path, url
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
    raise RuntimeError("PDF 下载失败；" + " | ".join(errors))


def write_fulltext_markdown(out_dir: Path, paper: dict[str, Any], source_url: str, markdown: str) -> Path:
    header = [
        f"# {clean_text(paper.get('title'))}",
        "",
        f"- source_url: {source_url}",
        f"- fetched_at: {datetime.now().isoformat(timespec='seconds')}",
        f"- arxiv_id: {paper.get('arxiv_id', '')}",
        f"- doi: {paper.get('doi', '')}",
        "",
        "-----",
        "",
    ]
    path = out_dir / "fulltext.md"
    path.write_text(collapse_blank_lines("\n".join(header) + markdown), encoding="utf-8")
    return path


def update_fulltext_index(workspace: Path, record: dict[str, Any]) -> None:
    index_path = catalog_dir(workspace) / "fulltext" / "index.json"
    index = read_json(index_path, {})
    index[record["paper_key"]] = record
    write_json(index_path, index)


def fetch_fulltext(
    workspace: Path,
    query: str,
    force: bool = False,
    pdf_only: bool = False,
    html_only: bool = False,
    timeout: int = 45,
    download_assets: bool = True,
) -> dict[str, Any]:
    pointer = resolve_pointer(workspace, query)
    paper = load_paper(workspace, pointer)
    retrieval = paper.get("retrieval") or pointer
    paper_key = retrieval["paper_key"]
    year = clean_text(retrieval.get("year")) or "unknown"
    out_dir = catalog_dir(workspace) / "fulltext" / year / paper_key
    out_dir.mkdir(parents=True, exist_ok=True)

    index_path = catalog_dir(workspace) / "fulltext" / "index.json"
    existing = read_json(index_path, {}).get(paper_key)
    if existing and not force:
        existing_path = existing.get("fulltext_path") or existing.get("pdf_path")
        if existing_path and (workspace / existing_path).exists():
            return {"status": "exists", **existing}

    errors: list[str] = []
    arxiv_id = clean_text(paper.get("arxiv_id") or (paper.get("ids") or {}).get("arxiv"))
    if arxiv_id and not pdf_only:
        try:
            markdown, source_url, assets = fetch_ar5iv_markdown(arxiv_id, out_dir, timeout, download_assets)
            text_path = write_fulltext_markdown(out_dir, paper, source_url, markdown)
            record = {
                "paper_key": paper_key,
                "title": paper.get("title", ""),
                "year": year,
                "method": "ar5iv_html_to_markdown",
                "source_url": source_url,
                "fulltext_path": str(text_path.relative_to(workspace)),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "chars": len(markdown),
                "assets": assets,
            }
            write_json(out_dir / "metadata.json", record)
            update_fulltext_index(workspace, record)
            return {"status": "fetched", **record}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ar5iv failed: {exc}")
            if html_only:
                raise

    pdf_path, source_url = download_pdf(paper, out_dir, timeout)
    record = {
        "paper_key": paper_key,
        "title": paper.get("title", ""),
        "year": year,
        "method": "pdf_download",
        "source_url": source_url,
        "pdf_path": str(pdf_path.relative_to(workspace)),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "bytes": pdf_path.stat().st_size,
        "errors": errors,
    }
    write_json(out_dir / "metadata.json", record)
    update_fulltext_index(workspace, record)
    return {"status": "fetched", **record}

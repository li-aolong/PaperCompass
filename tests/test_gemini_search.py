"""Unit tests for the optional gemini_search discovery source.

All tests use a StubGeminiPlugin that returns canned BrainResponses, so no
real `gemini` CLI invocation happens. The aim is to lock in the public
contract:

- raw + provenance + coverage + source_run get written when the brain
  returns valid JSON
- one grounding call counts as exactly one budget.consume()
- budget exhaustion short-circuits remaining queries gracefully
- non-parseable / errored brain responses log to coverage.errors but do not
  raise
- arxiv_id is rescued from the URL when gemini omits the structured field
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from papercompass.discovery import RemoteBudget
from papercompass.plugins.brain import (
    BrainInvocationError,
    BrainResponse,
    GeminiPlugin,
)
from papercompass.sources.gemini_search import (
    _filter_by_year,
    _normalize_paper_row,
    sync_gemini_search,
)


class StubGeminiPlugin(GeminiPlugin):
    """Replaces _ask_once so tests never spawn a real gemini subprocess."""

    def __init__(self, responses: list[Any], *, available: bool = True) -> None:
        self._responses = list(responses)
        self._available = available
        self.calls: list[str] = []

    @classmethod
    def is_available(cls) -> bool:
        return True

    def _ask_once(self, prompt, *, schema=None, **kwargs) -> BrainResponse:
        self.calls.append(prompt)
        if not self._available:
            from papercompass.plugins.brain import BrainUnavailable

            raise BrainUnavailable("stub: gemini cli missing")
        if not self._responses:
            raise RuntimeError("StubGeminiPlugin: no more canned responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        # Each response can be either a dict (parsed) or a BrainResponse.
        if isinstance(item, BrainResponse):
            return item
        text = json.dumps(item) if item is not None else ""
        return BrainResponse(
            text=text,
            parsed=item,
            raw_stdout=text,
            raw_stderr="",
            plugin="gemini",
            duration_seconds=0.0,
            extra={"returncode": 0},
        )


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / ".papercompass").mkdir(parents=True)
    return ws


def _topic() -> dict[str, Any]:
    return {
        "topic_id": "slm-agent",
        "name": "SLM agents",
        "description": "Small language model agents",
        "min_year": 2022,
        "strong_keywords": ["small language model agent"],
        "weak_keywords": ["agent"],
    }


def _paper(title: str, year: int, **extra: Any) -> dict[str, Any]:
    base = {
        "title": title,
        "year": year,
        "authors": "A. Smith; B. Lee",
        "arxiv_id": "",
        "doi": "",
        "url": f"https://arxiv.org/abs/{year}.00001",
        "venue": "arXiv",
        "abstract": "stub",
        "source_url": f"https://arxiv.org/abs/{year}.00001",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------- normalizer


def test_normalize_paper_row_extracts_arxiv_id_from_url():
    row = {
        "title": "T",
        "year": 2024,
        "url": "https://arxiv.org/abs/2403.12345v2",
        "doi": "",
        "arxiv_id": "",
    }
    out = _normalize_paper_row(row)
    assert out is not None
    assert out["arxiv_id"] == "2403.12345"


def test_normalize_paper_row_drops_when_no_title():
    assert _normalize_paper_row({"title": "", "url": "https://x"}) is None


def test_normalize_paper_row_drops_when_no_locator():
    row = {"title": "T", "year": 2024, "url": "", "arxiv_id": "", "doi": ""}
    assert _normalize_paper_row(row) is None


def test_normalize_paper_row_keeps_when_doi_only():
    row = {
        "title": "T",
        "year": 2024,
        "url": "",
        "arxiv_id": "",
        "doi": "10.1234/foo",
    }
    out = _normalize_paper_row(row)
    assert out is not None
    assert out["doi"] == "10.1234/foo"


# ---------------------------------------------------------------- year filter


def test_filter_by_year_drops_below_min():
    rows = [{"year": 2021}, {"year": 2023}, {"year": 0}]
    out = _filter_by_year(rows, (2022, 2026))
    # year=0 is treated as "unknown" and kept; 2021 dropped
    assert len(out) == 2
    assert {r["year"] for r in out} == {2023, 0}


# ---------------------------------------------------------------- sync happy


def test_sync_gemini_search_writes_raw_and_consumes_budget(tmp_path: Path):
    ws = _ws(tmp_path)
    canned = [
        {
            "papers": [
                _paper("On-device LLM Tool Use", 2024),
                _paper("SLM Agent Survey", 2023, doi="10.1145/foo"),
                {"title": "", "url": ""},  # dropped by normalizer
            ]
        }
    ]
    plugin = StubGeminiPlugin(canned)
    budget = RemoteBudget(limit=10)

    res = sync_gemini_search(
        ws,
        _topic(),
        years=(2022, 2026),
        queries=["small language model agent tool use"],
        plugin=plugin,
        budget=budget,
        max_results_per_query=20,
    )

    assert res["source"] == "gemini_search"
    assert res["runs"] == 1
    assert res["kept"] == 2
    assert res["seen"] == 3
    assert res["status"] == "completed"
    assert budget.used == 1
    assert plugin.calls and "small language model agent" in plugin.calls[0]

    raw_files = list((ws / ".raw" / "gemini_search").glob("*.jsonl"))
    assert len(raw_files) == 1
    rows = [json.loads(line) for line in raw_files[0].read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    assert all(r["source_name"] == "gemini_search" for r in rows)
    assert all("raw" in r and "title" in r["raw"] for r in rows)


# ---------------------------------------------------------------- skips


def test_sync_gemini_search_skips_when_no_queries(tmp_path: Path):
    ws = _ws(tmp_path)
    res = sync_gemini_search(
        ws,
        _topic(),
        years=None,
        queries=[],
        plugin=StubGeminiPlugin([]),
        budget=RemoteBudget(),
    )
    assert res["status"] == "skipped_no_queries"
    assert res["runs"] == 0


def test_sync_gemini_search_skips_when_cli_missing(tmp_path: Path, monkeypatch):
    ws = _ws(tmp_path)

    class NoCli(StubGeminiPlugin):
        @classmethod
        def is_available(cls) -> bool:
            return False

    res = sync_gemini_search(
        ws,
        _topic(),
        years=None,
        queries=["q1"],
        plugin=NoCli([]),
        budget=RemoteBudget(),
    )
    assert res["status"] == "skipped_no_cli"
    assert res["runs"] == 0


# ---------------------------------------------------------------- failures


def test_sync_gemini_search_handles_brain_error_without_raising(tmp_path: Path):
    ws = _ws(tmp_path)
    plugin = StubGeminiPlugin([
        BrainInvocationError("simulated 502"),
        BrainInvocationError("retry also fails"),
        {"papers": [_paper("Recovered Paper", 2024)]},
    ])
    res = sync_gemini_search(
        ws,
        _topic(),
        years=(2022, 2026),
        queries=["query-a", "query-b"],
        plugin=plugin,
        budget=RemoteBudget(limit=10),
    )
    # query-a fails (with one retry inside ask()), query-b succeeds.
    # ask() consumes responses 1 then 2 for query-a, so query-b sees response 3.
    assert res["runs"] == 2
    assert res["kept"] == 1
    assert any(e.get("phase") == "ask" for e in res["errors"])


def test_sync_gemini_search_handles_unparseable_response(tmp_path: Path):
    ws = _ws(tmp_path)
    plugin = StubGeminiPlugin([
        BrainResponse(
            text="not json",
            parsed=None,
            raw_stdout="not json",
            raw_stderr="",
            plugin="gemini",
            duration_seconds=0.0,
        ),
    ])
    res = sync_gemini_search(
        ws,
        _topic(),
        years=None,
        queries=["q"],
        plugin=plugin,
        budget=RemoteBudget(limit=10),
    )
    assert res["runs"] == 1
    assert res["kept"] == 0
    assert any("parseable" in e["error"].lower() or "missing" in e["error"].lower() for e in res["errors"])


def test_sync_gemini_search_breaks_on_budget_exhausted(tmp_path: Path):
    ws = _ws(tmp_path)
    plugin = StubGeminiPlugin([
        {"papers": [_paper("First", 2024)]},
        # second query never asked because budget runs out first
    ])
    budget = RemoteBudget(limit=1)
    res = sync_gemini_search(
        ws,
        _topic(),
        years=None,
        queries=["q1", "q2"],
        plugin=plugin,
        budget=budget,
    )
    assert res["runs"] == 1
    assert res["kept"] == 1
    assert any(e.get("phase") == "budget" for e in res["errors"])
    # Plugin only called once even though we passed two queries.
    assert len(plugin.calls) == 1

"""Deterministic source-budget helpers.

Source query planning is code-owned so runs stay reproducible across brains.
The helpers here keep generated source configs internally consistent when a
later stage appends queries after the initial plan.
"""

from __future__ import annotations

import math
from typing import Any


def positive_int(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def planned_arxiv_remote_calls(
    query_count: int,
    *,
    recent_year_count: int | None,
    max_results: int | None,
    page_size: int | None,
) -> int:
    """Return the deterministic number of arXiv API buckets for a config."""

    queries = max(0, int(query_count or 0))
    if queries == 0:
        return 0
    years = max(1, int(recent_year_count or 1))
    results = max(1, int(max_results or 1))
    page = max(1, int(page_size or results))
    pages = max(1, math.ceil(results / page))
    return queries * years * pages


def recommended_arxiv_remote_calls(
    query_count: int,
    *,
    recent_year_count: int | None = 3,
    max_results: int | None = 35,
    page_size: int | None = 35,
    floor: int = 24,
) -> int:
    """Budget floor for generated arXiv configs.

    The floor keeps tiny topics from writing surprising single-digit budgets.
    Non-empty query plans get a small deterministic cushion so one appended
    seed-repair query cannot immediately exhaust the source budget.
    """

    planned = planned_arxiv_remote_calls(
        query_count,
        recent_year_count=recent_year_count,
        max_results=max_results,
        page_size=page_size,
    )
    if planned <= 0:
        return max(1, int(floor or 1))
    years = max(1, int(recent_year_count or 1))
    cushion = max(years, math.ceil(planned * 0.10))
    return max(int(floor or 1), planned + cushion)


def ensure_arxiv_budget_floor(arxiv_cfg: dict[str, Any], *, floor: int = 24) -> int:
    """Raise ``max_remote_calls`` until it covers the configured query plan."""

    queries = [str(q).strip() for q in arxiv_cfg.get("queries") or [] if str(q).strip()]
    recommended = recommended_arxiv_remote_calls(
        len(queries),
        recent_year_count=positive_int(arxiv_cfg.get("recent_year_count")) or 3,
        max_results=positive_int(arxiv_cfg.get("max_results")) or 35,
        page_size=(
            positive_int(arxiv_cfg.get("page_size"))
            or positive_int(arxiv_cfg.get("max_results"))
            or 35
        ),
        floor=floor,
    )
    current = positive_int(arxiv_cfg.get("max_remote_calls"))
    if current is None or current < recommended:
        arxiv_cfg["max_remote_calls"] = recommended
        return recommended
    return current

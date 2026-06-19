"""Smoke tests for the live brain plugins (codex, gemini, claude).

Each test sends a tiny prompt + simple JSON schema and asserts that the plugin
returns a parseable structured response. Skipped when the underlying CLI is
not on PATH or login is missing.

These tests cost real API tokens (~$0.01-0.05 each). They are tagged `smoke`
so users can run them only when they want a live sanity check:

    pytest -m smoke
    pytest -m smoke -k codex   # one plugin only

By default `pytest -q` does NOT include them — see the `addopts` patch in
conftest.py if you want a different default.

Notes per plugin:

- codex: uses `codex exec --output-schema`. Requires prior `codex login` once.
- gemini: uses `gemini -p "..." -y`. Requires `GEMINI_API_KEY` or login.
- claude: uses `claude -p ... --bare --json-schema`. Requires API key. Note
  this test cannot run from inside Claude Code itself (the running session
  intercepts the CLI). Run from another shell:
    pytest -m smoke -k claude
"""

from __future__ import annotations

import shutil

import pytest

from papercompass.plugins.brain import (
    ClaudePlugin,
    CodexPlugin,
    GeminiPlugin,
)


_TINY_PROMPT = (
    "Return a small JSON object describing the topic 'speculative decoding'. "
    "Do not include any prose."
)
_TINY_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "year_floor": {"type": "integer"},
        "core_terms": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["topic", "year_floor", "core_terms"],
}


def _has_cli(name: str) -> bool:
    return shutil.which(name) is not None


@pytest.mark.smoke
@pytest.mark.skipif(not _has_cli("codex"), reason="codex CLI not on PATH")
def test_codex_plugin_round_trip() -> None:
    plugin = CodexPlugin()
    resp = plugin.ask(_TINY_PROMPT, schema=_TINY_SCHEMA, timeout=300, retries=0)
    assert isinstance(resp.parsed, dict), f"codex parsed missing: text={resp.text[:200]!r}"
    assert "topic" in resp.parsed
    assert isinstance(resp.parsed.get("core_terms"), list)


@pytest.mark.smoke
@pytest.mark.skipif(not _has_cli("gemini"), reason="gemini CLI not on PATH")
def test_gemini_plugin_round_trip() -> None:
    plugin = GeminiPlugin()
    resp = plugin.ask(_TINY_PROMPT, schema=_TINY_SCHEMA, timeout=300, retries=0)
    assert isinstance(resp.parsed, dict), f"gemini parsed missing: text={resp.text[:200]!r}"
    assert "topic" in resp.parsed


@pytest.mark.smoke
@pytest.mark.skipif(not _has_cli("claude"), reason="claude CLI not on PATH")
def test_claude_plugin_round_trip() -> None:
    # NOTE: cannot run from inside a Claude Code session because the running
    # CLI intercepts. Run from a plain shell.
    plugin = ClaudePlugin()
    resp = plugin.ask(_TINY_PROMPT, schema=_TINY_SCHEMA, timeout=300, retries=0)
    assert isinstance(resp.parsed, dict), f"claude parsed missing: text={resp.text[:200]!r}"
    assert "topic" in resp.parsed

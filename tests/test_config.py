import json

from papercompass.cli import main
from papercompass.config import init_workspace


def test_init_workspace_uses_compact_state_layout(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")

    assert (workspace / "topic.yaml").exists()
    assert (workspace / "sources.yaml").exists()
    assert (workspace / ".raw").is_dir()
    assert (workspace / ".raw" / "arxiv").is_dir()
    assert (workspace / ".raw" / "openalex").is_dir()
    assert (workspace / ".raw" / "paperlists").is_dir()
    assert (workspace / ".raw" / "crossref").is_dir()
    assert (workspace / ".raw" / "dblp").is_dir()
    assert (workspace / ".raw" / "acl_anthology").is_dir()
    assert (workspace / ".raw" / "europepmc").is_dir()
    assert (workspace / ".raw" / "pubmed").is_dir()
    assert (workspace / ".raw" / "openreview").is_dir()
    assert (workspace / ".raw" / "semanticscholar").is_dir()
    assert (workspace / "data").is_dir()
    assert (workspace / "catalog").is_dir()
    assert (workspace / ".papercompass" / "cache").is_dir()
    assert (workspace / ".papercompass" / "logs").is_dir()
    assert (workspace / ".papercompass" / "manifests").is_dir()

    assert not (workspace / "overrides").exists()
    assert not (workspace / ".raw" / "imported").exists()
    assert not (workspace / ".raw" / "manual").exists()
    assert not (workspace / ".raw" / "agent_search").exists()
    assert not (workspace / ".raw" / "saved_search").exists()
    assert not (workspace / "cache").exists()
    assert not (workspace / "logs").exists()
    assert not (workspace / "manifests").exists()
    assert not (workspace / "fulltext").exists()
    assert not (workspace / "notes").exists()
    assert not (workspace / "ideas").exists()


def test_override_add_records_manual_patch(tmp_path, capsys) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    main([
        "override",
        "add",
        "--workspace",
        str(workspace),
        "--title",
        "A Paper",
        "--year",
        "2024",
        "--authors",
        "Alice; Bob",
        "--url",
        "https://example.org/paper",
        "--system-tag",
        "author_corrected",
    ])
    assert (workspace / "overrides").is_dir()
    captured = json.loads(capsys.readouterr().out)
    out_path = workspace / "overrides" / "manual.jsonl"
    assert captured["output"] == str(out_path)
    rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
    assert rows == [{
        "authors": "Alice; Bob",
        "system_tags": ["author_corrected"],
        "title": "A Paper",
        "url": "https://example.org/paper",
        "year": "2024",
    }]


def test_add_paper_creates_manual_raw_on_demand(tmp_path, capsys) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    main([
        "add-paper",
        "--workspace",
        str(workspace),
        "--title",
        "Confirmed Missing Paper",
        "--year",
        "2024",
        "--authors",
        "Alice; Bob",
        "--tag",
        "task:gec",
    ])
    captured = json.loads(capsys.readouterr().out)
    out_path = workspace / ".raw" / "manual"
    assert out_path.is_dir()
    row = json.loads(next(out_path.glob("*.jsonl")).read_text(encoding="utf-8").strip())
    assert captured["count"] == 1
    assert row["source_type"] == "manual"
    assert row["raw"]["title"] == "Confirmed Missing Paper"
    assert row["raw"]["tags"] == ["task:gec"]


def test_agent_search_record_creates_empty_raw_trace(tmp_path, capsys) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")

    assert not (workspace / ".raw" / "agent_search").exists()

    main([
        "agent-search",
        "record",
        "--workspace",
        str(workspace),
        "--source",
        "codex_check",
        "--query",
        "GEC 2022+ 查漏",
        "--note",
        "本轮查漏无新增候选",
    ])
    captured = json.loads(capsys.readouterr().out)

    out_path = workspace / ".raw" / "agent_search"
    files = list(out_path.glob("*.jsonl"))
    assert out_path.is_dir()
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == ""
    assert captured["count"] == 0
    assert captured["note"] == "本轮查漏无新增候选"
    assert (workspace / ".papercompass" / "logs" / "WORKLOG.md").exists()


def test_review_feedback_import_writes_agent_search_trace(tmp_path, capsys) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    input_path = tmp_path / "review_missing.jsonl"
    input_path.write_text(
        json.dumps({"title": "Missing Review Paper", "year": 2025}) + "\n",
        encoding="utf-8",
    )

    main([
        "review-feedback",
        "import",
        "--workspace",
        str(workspace),
        "--input",
        str(input_path),
        "--source",
        "claude_opus_4_7",
        "--query",
        "external review missing papers",
    ])
    captured = json.loads(capsys.readouterr().out)

    out_path = workspace / captured["output"]
    row = json.loads(out_path.read_text(encoding="utf-8").strip())
    assert captured["count"] == 1
    assert row["source_type"] == "agent_search"
    assert row["source_name"] == "claude_opus_4_7"
    assert "agent_run_log" in captured
    assert (workspace / ".papercompass" / "logs" / "AGENT_BUILD_LOG.md").exists()


def test_agent_run_log_records_decision_steps(tmp_path, capsys) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")

    main([
        "agent-run",
        "log",
        "--workspace",
        str(workspace),
        "--new-run",
        "--phase",
        "direction-decomposition",
        "--status",
        "completed",
        "--summary",
        "拆出强弱 query",
        "--file",
        "topic.yaml",
    ])
    first = json.loads(capsys.readouterr().out)

    main([
        "agent-run",
        "log",
        "--workspace",
        str(workspace),
        "--phase",
        "source-config",
        "--status",
        "completed",
        "--summary",
        "配置 OpenAlex",
        "--file",
        "sources.yaml",
    ])
    second = json.loads(capsys.readouterr().out)

    assert first["run_id"] == second["run_id"]
    steps = workspace / ".papercompass" / "logs" / "agent_build_steps.jsonl"
    rows = [json.loads(line) for line in steps.read_text(encoding="utf-8").splitlines()]
    assert [row["phase"] for row in rows] == ["direction-decomposition", "source-config"]
    md = (workspace / ".papercompass" / "logs" / "AGENT_BUILD_LOG.md").read_text(encoding="utf-8")
    assert "拆出强弱 query" in md
    assert "sources.yaml" in md

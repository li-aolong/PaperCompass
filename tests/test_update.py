import json
from pathlib import Path

import pytest

from papercompass.cli import main
from papercompass.confirmation import prepare_confirmation, update_confirmation_inputs
from papercompass.config import init_workspace
import papercompass.update as update_module
from papercompass.update import UpdateConfirmationRequired, run_workspace_update


def test_workspace_update_requires_explicit_confirmation(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")

    with pytest.raises(UpdateConfirmationRequired):
        run_workspace_update(workspace, min_year=2022)

    assert not (workspace / ".papercompass" / "updates").exists()


def test_update_without_token_has_no_side_effects_on_new_path(tmp_path) -> None:
    workspace = tmp_path / "new-topic"

    with pytest.raises(UpdateConfirmationRequired):
        run_workspace_update(workspace, min_year=2022)

    assert not workspace.exists()


def test_workspace_update_runs_full_refresh_and_writes_summary(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    calls: dict[str, dict] = {}

    def fake_discovery(workspace_arg, **kwargs):
        assert workspace_arg != workspace
        assert workspace_arg.name == "staged_workspace"
        calls["discovery"] = kwargs
        (workspace_arg / ".raw" / "new.jsonl").write_text("{}", encoding="utf-8")
        return {"paper_count": 3, "remote_calls_used": 2}

    def fake_build(workspace_arg):
        assert workspace_arg != workspace
        calls["build"] = {}
        (workspace_arg / "data" / "papers.json").write_text('[{"title":"Kept","year":2024}]', encoding="utf-8")
        return {"paper_count": 2, "anchor_paper_count": 1}

    def fake_catalog(workspace_arg):
        assert workspace_arg != workspace
        calls["catalog"] = {}
        (workspace_arg / "catalog" / "manifest.json").write_text('{"paper_count":2}', encoding="utf-8")
        return {"catalog": "catalog", "paper_count": 2}

    def fake_qa(workspace_arg, *, refresh_coverage=False, **kwargs):
        assert workspace_arg != workspace
        calls["qa"] = {"refresh_coverage": refresh_coverage, **kwargs}
        return {
            "status": "warning",
            "critical": [],
            "warnings": ["source_coverage_has_risk"],
            "counts": {"papers": 2, "anchors": 1, "pending": 0, "rejected": 4},
            "manifest": ".papercompass/manifests/quality_gates.json",
            "markdown": ".papercompass/reports/final_audit.md",
        }

    monkeypatch.setattr("papercompass.update.run_discovery", fake_discovery)
    monkeypatch.setattr("papercompass.update.build_workspace", fake_build)
    monkeypatch.setattr("papercompass.update.build_catalog", fake_catalog)
    monkeypatch.setattr("papercompass.update.build_quality_report", fake_qa)
    inputs = update_confirmation_inputs(
        workspace=workspace,
        sources=["openalex", "crossref"],
        min_year=2022,
        max_year=2026,
        paperlists_venues=["ACL"],
        refresh=True,
        refresh_coverage=True,
        timeout=9,
        max_remote_calls=12,
        mode="delta",
    )
    token = prepare_confirmation(workspace, command="update", inputs=inputs)["token"]

    result = run_workspace_update(
        workspace,
        sources=["openalex", "crossref"],
        min_year=2022,
        max_year=2026,
        paperlists_venues=["ACL"],
        refresh=True,
        refresh_coverage=True,
        timeout=9,
        max_remote_calls=12,
        confirmed_token=token,
        mode="delta",
    )

    assert calls["discovery"]["build"] is False
    assert calls["discovery"]["catalog"] is False
    assert calls["discovery"]["sources"] == ["openalex", "crossref"]
    assert calls["discovery"]["min_year"] == 2022
    assert calls["discovery"]["max_remote_calls"] == 12
    assert calls["qa"] == {"refresh_coverage": True}
    assert result["mode"] == "staged_checkpointed_full_rebuild_with_identity_delta"
    assert result["status"] == "qa_warning"
    assert result["qa"]["warnings"] == ["source_coverage_has_risk"]
    assert result["delta"]["checkpoint_committed"] is True
    assert result["delta"]["commit"].endswith("/commit.json")

    latest_path = workspace / ".papercompass" / "updates" / "latest.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    assert latest["summary"].startswith(".papercompass/updates/update_")
    assert (workspace / latest["summary"]).exists()
    assert latest["inputs"]["refresh"] is True
    assert (workspace / ".papercompass" / "checkpoints" / "discovery.json").exists()
    checkpoint = json.loads((workspace / ".papercompass" / "checkpoints" / "discovery.json").read_text(encoding="utf-8"))
    assert checkpoint["mode"] == "checkpointed_full_rebuild_with_identity_delta"
    assert "sources" in checkpoint
    commit = json.loads((workspace / result["delta"]["commit"]).read_text(encoding="utf-8"))
    assert commit["rollback_scope"] == [".raw", "data", "catalog"]
    assert ".papercompass/manifests" in commit["audit_artifacts_retained"]
    assert commit["artifact_transaction"]["backup_retained"] is False
    assert commit["artifact_transaction"]["backup_cleanup"]["status"] == "removed"
    assert commit["artifact_transaction"]["staging_cleanup"]["status"] == "removed"
    assert commit["publish"]["mode"] == "staged_workspace_publish"
    update_run_dir = (workspace / result["delta"]["commit"]).parent
    assert not (update_run_dir / "backup_before").exists()
    assert not (update_run_dir / "staged_workspace").exists()
    assert (workspace / ".raw" / "new.jsonl").exists()


def test_workspace_update_rolls_back_artifacts_when_qa_fails(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / ".raw" / "sentinel.txt").write_text("raw-before", encoding="utf-8")
    (workspace / "data" / "sentinel.txt").write_text("data-before", encoding="utf-8")
    (workspace / "catalog" / "sentinel.txt").write_text("catalog-before", encoding="utf-8")

    def fake_discovery(workspace_arg, **kwargs):
        (workspace_arg / ".raw" / "new.jsonl").write_text("polluted", encoding="utf-8")
        return {"paper_count": 1, "remote_calls_used": 1, "remote_calls_limit": 3, "source_results": []}

    def fake_build(workspace_arg):
        (workspace_arg / "data" / "papers.json").write_text('[{"title":"Changed"}]', encoding="utf-8")
        return {"papers": [{"title": "Changed", "year": 2024}], "paper_count": 1}

    def fake_catalog(workspace_arg):
        (workspace_arg / "catalog" / "manifest.json").write_text('{"changed":true}', encoding="utf-8")
        return {"paper_count": 1}

    def fake_qa(workspace_arg, **kwargs):
        return {"status": "failed", "critical": ["bad"], "warnings": [], "counts": {}}

    monkeypatch.setattr("papercompass.update.run_discovery", fake_discovery)
    monkeypatch.setattr("papercompass.update.build_workspace", fake_build)
    monkeypatch.setattr("papercompass.update.build_catalog", fake_catalog)
    monkeypatch.setattr("papercompass.update.build_quality_report", fake_qa)
    inputs = update_confirmation_inputs(
        workspace=workspace,
        sources=["arxiv"],
        min_year=2022,
        max_year=None,
        paperlists_venues=None,
        refresh=False,
        refresh_coverage=False,
        timeout=35,
        max_remote_calls=3,
        mode="delta",
    )
    token = prepare_confirmation(workspace, command="update", inputs=inputs)["token"]

    result = run_workspace_update(
        workspace,
        sources=["arxiv"],
        min_year=2022,
        max_remote_calls=3,
        confirmed_token=token,
    )

    assert result["status"] == "qa_failed"
    assert result["delta"]["checkpoint_committed"] is False
    assert result["delta"]["artifact_transaction"]["rollback_scope"] == [".raw", "data", "catalog"]
    assert result["delta"]["rollback"]["reason"] == "qa_failed_before_publish"
    assert result["delta"]["rollback"]["moved_current_to_failed_artifacts"] == []
    assert (workspace / ".raw" / "sentinel.txt").read_text(encoding="utf-8") == "raw-before"
    assert not (workspace / ".raw" / "new.jsonl").exists()
    assert (workspace / "data" / "sentinel.txt").read_text(encoding="utf-8") == "data-before"
    assert not (workspace / "data" / "papers.json").exists()
    assert (workspace / "catalog" / "sentinel.txt").read_text(encoding="utf-8") == "catalog-before"
    assert not (workspace / "catalog" / "manifest.json").exists()


def test_workspace_update_rolls_back_published_artifacts_when_checkpoint_write_fails(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / ".raw" / "sentinel.txt").write_text("raw-before", encoding="utf-8")
    (workspace / "data" / "sentinel.txt").write_text("data-before", encoding="utf-8")
    (workspace / "catalog" / "sentinel.txt").write_text("catalog-before", encoding="utf-8")
    checkpoint_dir = workspace / ".papercompass" / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / "discovery.json").write_text('{"old": true}\n', encoding="utf-8")
    (checkpoint_dir / "identity_index.jsonl").write_text('{"key":"old"}\n', encoding="utf-8")

    def fake_discovery(workspace_arg, **kwargs):
        (workspace_arg / ".raw" / "new.jsonl").write_text("polluted", encoding="utf-8")
        return {"paper_count": 1, "remote_calls_used": 1, "remote_calls_limit": 3, "source_results": []}

    def fake_build(workspace_arg):
        (workspace_arg / "data" / "papers.json").write_text('[{"title":"Changed","year":2024}]', encoding="utf-8")
        return {"papers": [{"title": "Changed", "year": 2024}], "paper_count": 1}

    def fake_catalog(workspace_arg):
        (workspace_arg / "catalog" / "manifest.json").write_text('{"changed":true}', encoding="utf-8")
        return {"paper_count": 1}

    def fake_qa(workspace_arg, **kwargs):
        return {"status": "passed", "critical": [], "warnings": [], "counts": {}}

    original_write_jsonl = update_module.write_jsonl

    def fail_identity_checkpoint(path, rows):
        if Path(path).name == "identity_index.jsonl":
            raise RuntimeError("checkpoint boom")
        return original_write_jsonl(path, rows)

    monkeypatch.setattr("papercompass.update.run_discovery", fake_discovery)
    monkeypatch.setattr("papercompass.update.build_workspace", fake_build)
    monkeypatch.setattr("papercompass.update.build_catalog", fake_catalog)
    monkeypatch.setattr("papercompass.update.build_quality_report", fake_qa)
    monkeypatch.setattr(update_module, "write_jsonl", fail_identity_checkpoint)
    inputs = update_confirmation_inputs(
        workspace=workspace,
        sources=["arxiv"],
        min_year=2022,
        max_year=None,
        paperlists_venues=None,
        refresh=False,
        refresh_coverage=False,
        timeout=35,
        max_remote_calls=3,
        mode="delta",
    )
    token = prepare_confirmation(workspace, command="update", inputs=inputs)["token"]

    with pytest.raises(RuntimeError, match="checkpoint boom"):
        run_workspace_update(
            workspace,
            sources=["arxiv"],
            min_year=2022,
            max_remote_calls=3,
            confirmed_token=token,
        )

    assert (workspace / ".raw" / "sentinel.txt").read_text(encoding="utf-8") == "raw-before"
    assert not (workspace / ".raw" / "new.jsonl").exists()
    assert (workspace / "data" / "sentinel.txt").read_text(encoding="utf-8") == "data-before"
    assert not (workspace / "data" / "papers.json").exists()
    assert (workspace / "catalog" / "sentinel.txt").read_text(encoding="utf-8") == "catalog-before"
    assert not (workspace / "catalog" / "manifest.json").exists()
    assert (checkpoint_dir / "discovery.json").read_text(encoding="utf-8") == '{"old": true}\n'
    assert (checkpoint_dir / "identity_index.jsonl").read_text(encoding="utf-8") == '{"key":"old"}\n'
    update_runs = sorted((workspace / ".papercompass" / "updates").glob("update_*"))
    failed_commit = json.loads((update_runs[-1] / "commit_checkpoint_failed.json").read_text(encoding="utf-8"))
    assert failed_commit["phase"] == "rolled_back"
    assert failed_commit["rollback"]["reason"] == "checkpoint_publish_failed"
    assert failed_commit["rollback"]["artifacts"]["restored"]
    assert failed_commit["rollback"]["checkpoints"]["restored"]


def test_cli_update_requires_confirmed_token(tmp_path, capsys) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")

    with pytest.raises(SystemExit) as exc:
        main(["update", "--workspace", str(workspace), "--min-year", "2022"])

    assert exc.value.code == 1
    captured = json.loads(capsys.readouterr().out)
    assert captured["status"] == "error"
    assert captured["error_code"] == "confirmation_required"
    assert "--confirmed-token" in captured["error"]
    assert not (workspace / ".papercompass" / "updates").exists()


def test_cli_update_prepare_writes_confirmation_token(tmp_path, capsys) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")

    main(["update", "--workspace", str(workspace), "--min-year", "2022", "--prepare"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "confirmation_required"
    assert payload["confirmation_token"].startswith("pcfm_")
    assert payload["inputs"]["min_year"] == 2022
    assert payload["inputs"]["mode"] == "delta"
    assert payload["inputs"]["workspace_context_hash"]
    assert (workspace / ".papercompass" / "confirmations" / f"{payload['confirmation_token']}.json").exists()


def test_update_token_rejects_changed_workspace_context(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    inputs = update_confirmation_inputs(
        workspace=workspace,
        sources=["openalex"],
        min_year=2022,
        max_year=None,
        paperlists_venues=None,
        refresh=False,
        refresh_coverage=False,
        timeout=35,
        max_remote_calls=None,
        mode="delta",
    )
    token = prepare_confirmation(workspace, command="update", inputs=inputs)["token"]
    (workspace / "sources.yaml").write_text("sources:\n  arxiv: {}\n", encoding="utf-8")

    with pytest.raises(UpdateConfirmationRequired, match="参数|不一致"):
        run_workspace_update(
            workspace,
            sources=["openalex"],
            min_year=2022,
            confirmed_token=token,
        )

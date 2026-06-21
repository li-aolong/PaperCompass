import json
import os
import time
import zipfile

from papercompass.config import init_workspace
from papercompass.doctor import doctor_archive, doctor_workspace, monitor_cost, monitor_summary, monitor_trends
from papercompass.metrics import record_run_metric


def test_doctor_archive_rejects_local_artifacts(tmp_path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("src/papercompass/__pycache__/x.pyc", b"bad")
        zf.writestr(".claude/settings.local.json", "{}")

    result = doctor_archive(archive)

    assert result["status"] == "failed"
    assert result["bad_entry_count"] == 2


def test_doctor_archive_rejects_egg_info_and_high_entropy_text(tmp_path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("src/papercompass.egg-info/PKG-INFO", "metadata")
        zf.writestr("notes", "token-like: aB3dE5fG7hI9jK1lM2nO4pQ6rS8tU0vW")

    result = doctor_archive(archive)

    assert result["status"] == "failed"
    assert "src/papercompass.egg-info/PKG-INFO" in result["bad_entries"]
    assert result["secret_hit_count"] == 1


def test_doctor_archive_strict_flags_large_text_skip(tmp_path) -> None:
    archive = tmp_path / "large.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("large.env", b"A" * (2 * 1024 * 1024 + 1))

    normal = doctor_archive(archive)
    strict = doctor_archive(archive, strict=True)

    assert normal["status"] == "passed"
    assert normal["large_text_skipped_count"] == 1
    assert strict["status"] == "failed"


def test_doctor_archive_accepts_clean_source_subset(tmp_path) -> None:
    archive = tmp_path / "good.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("src/papercompass/__init__.py", "")
        zf.writestr("tests/test_example.py", "")
        zf.writestr("README.md", "PaperCompass")

    result = doctor_archive(archive)

    assert result["status"] == "passed"


def test_doctor_archive_rejects_path_traversal_entries(tmp_path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../outside.py", "print('bad')")
        zf.writestr("/absolute.py", "print('bad')")
        zf.writestr("C:\\temp\\x.py", "print('bad')")

    result = doctor_archive(archive)

    assert result["status"] == "failed"
    assert result["bad_entry_count"] == 3


def test_doctor_workspace_and_monitor_summary(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    (workspace / "data" / "papers.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "anchor_papers.json").write_text("[]", encoding="utf-8")
    (workspace / "data" / "pending_review_candidates.json").write_text(json.dumps([{"title": "P"}]), encoding="utf-8")
    (workspace / "data" / "rejected_candidates.json").write_text("[]", encoding="utf-8")
    record_run_metric(workspace, {"run_id": "r1", "command": "discover", "qa_status": "warning"})
    record_run_metric(workspace, {
        "run_id": "r2",
        "command": "auto-build",
        "llm_input_tokens": 10,
        "llm_output_tokens": 5,
        "llm_cost_usd": 0.25,
    })
    cache_path = workspace / ".papercompass" / "cache" / "review" / "brain_scores.v2.jsonl"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({
            "cache_key": "old",
            "candidate_fingerprint": "stale",
            "topic_context_hash": "old",
            "decision": {},
        }) + "\n",
        encoding="utf-8",
    )

    doctor = doctor_workspace(workspace)
    monitor = monitor_summary(workspace)
    cost = monitor_cost(workspace)

    assert doctor["status"] == "warning"
    assert "pending_review_unresolved" in doctor["warnings"]
    assert doctor["review_cache"]["stale_count"] == 1
    assert monitor["latest_metric"]["run_id"] == "r2"
    assert cost["llm_input_tokens"] == 10
    assert cost["llm_cost_usd"] == 0.25


def test_monitor_trends_reports_operational_alerts(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    record_run_metric(workspace, {
        "run_id": "r1",
        "command": "auto-build",
        "remote_calls_used": 9,
        "remote_calls_limit": 10,
        "llm_cost_usd": 1.5,
        "review_cache_hit_count": 0,
        "review_cache_miss_count": 5,
        "prefilter_llm_review_ratio": 0.75,
        "source_error_count": 1,
    })

    trends = monitor_trends(workspace, llm_cost_limit=1.0)

    codes = {alert["code"] for alert in trends["alerts"]}
    assert trends["review_cache"]["hit_rate"] == 0.0
    assert trends["remote_calls"]["usage_ratio"] == 0.9
    assert trends["sources"]["source_failure_rate"] == 1.0
    assert "llm_cost_budget_exceeded" in codes
    assert "review_cache_hit_rate_low" in codes
    assert "prefilter_llm_review_ratio_high" in codes
    assert "remote_calls_near_limit" in codes
    assert "source_errors_recent" in codes


def test_doctor_workspace_fix_moves_low_risk_orphans_to_trash(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    tmp_file = workspace / "data" / "leftover.tmp"
    tmp_file.write_text("tmp", encoding="utf-8")
    catalog_swap = workspace / ".catalog.tmp.123"
    catalog_swap.mkdir()
    old = time.time() - 600
    os.utime(tmp_file, (old, old))
    os.utime(catalog_swap, (old, old))

    result = doctor_workspace(workspace, fix=True)

    assert result["fix"]["enabled"] is True
    assert not tmp_file.exists()
    assert not catalog_swap.exists()
    assert result["fix"]["moved_to_trash"]["orphan_tmp_files"]
    assert result["fix"]["moved_to_trash"]["orphan_catalog_swap_dirs"]


def test_doctor_workspace_fix_skips_recent_orphans(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    tmp_file = workspace / "data" / "active.tmp"
    tmp_file.write_text("tmp", encoding="utf-8")

    result = doctor_workspace(workspace, fix=True)

    assert tmp_file.exists()
    assert not result["fix"]["moved_to_trash"]["orphan_tmp_files"]
    assert result["fix"]["moved_to_trash"]["skipped_recent_orphans"]


def test_doctor_workspace_ignores_tmp_files_already_in_trash(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    trash_tmp = workspace / ".papercompass" / "trash" / "doctor_20260101_000000" / "data" / "leftover.tmp"
    trash_tmp.parent.mkdir(parents=True)
    trash_tmp.write_text("tmp", encoding="utf-8")
    old = time.time() - 600
    os.utime(trash_tmp, (old, old))

    result = doctor_workspace(workspace, fix=True)

    assert trash_tmp.exists()
    assert not any("leftover.tmp" in item for item in result["orphan_tmp_files"])
    assert not result["fix"]["moved_to_trash"]["orphan_tmp_files"]


def test_doctor_workspace_prunes_success_update_backups_only(tmp_path) -> None:
    workspace = tmp_path / "topic"
    init_workspace(workspace, "topic")
    success_dir = workspace / ".papercompass" / "updates" / "update_20260101_000000"
    success_backup = success_dir / "backup_before"
    success_backup.mkdir(parents=True)
    (success_backup / "data.txt").write_text("backup", encoding="utf-8")
    (success_dir / "commit.json").write_text(
        json.dumps({"phase": "committed", "checkpoint_committed": True}),
        encoding="utf-8",
    )
    failed_dir = workspace / ".papercompass" / "updates" / "update_20260102_000000"
    failed_backup = failed_dir / "backup_before"
    failed_backup.mkdir(parents=True)
    failed_artifacts = failed_dir / "failed_artifacts"
    failed_artifacts.mkdir()
    (failed_dir / "commit.json").write_text(
        json.dumps({"phase": "rolled_back", "checkpoint_committed": False}),
        encoding="utf-8",
    )
    old = time.time() - 600
    os.utime(success_backup, (old, old))
    os.utime(failed_backup, (old, old))

    result = doctor_workspace(workspace, fix=True, prune_updates=True)

    assert not success_backup.exists()
    assert failed_backup.exists()
    assert failed_artifacts.exists()
    assert result["fix"]["moved_to_trash"]["update_success_backups"]

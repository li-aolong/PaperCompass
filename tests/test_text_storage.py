from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path
import threading

import pytest

from papercompass.text import WorkspaceLockTimeout, atomic_write_text, workspace_lock, write_jsonl


def _workspace_lock_process_contender(workspace: str, queue: mp.Queue) -> None:
    from papercompass.text import WorkspaceLockTimeout, workspace_lock

    try:
        queue.put("started")
        with workspace_lock(Path(workspace), timeout_seconds=0.2):
            queue.put("entered")
    except WorkspaceLockTimeout:
        queue.put("timeout")
    except BaseException as exc:  # noqa: BLE001
        queue.put(f"error:{type(exc).__name__}:{exc}")


def test_atomic_write_text_cleans_temp_file_on_replace_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "state.json"

    def fail_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise RuntimeError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(RuntimeError, match="replace failed"):
        atomic_write_text(target, "{}")

    assert not target.exists()
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_write_jsonl_cleans_temp_file_on_replace_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "rows.jsonl"

    def fail_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise RuntimeError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(RuntimeError, match="replace failed"):
        write_jsonl(target, [{"title": "A"}])

    assert not target.exists()
    assert list(tmp_path.glob(".rows.jsonl.*.tmp")) == []


def test_workspace_lock_is_reentrant_and_does_not_touch_workspace(tmp_path: Path):
    workspace = tmp_path / "ws"

    with workspace_lock(workspace):
        with workspace_lock(workspace):
            pass

    assert not workspace.exists()


def test_workspace_lock_blocks_other_threads(tmp_path: Path):
    workspace = tmp_path / "ws"
    started = threading.Event()
    entered = threading.Event()

    def contender() -> None:
        started.set()
        with workspace_lock(workspace):
            entered.set()

    with workspace_lock(workspace):
        thread = threading.Thread(target=contender)
        thread.start()
        assert started.wait(1)
        assert not entered.wait(0.05)

    assert entered.wait(1)
    thread.join(timeout=1)


def test_workspace_lock_timeout(tmp_path: Path):
    workspace = tmp_path / "ws"
    timed_out = threading.Event()

    def contender() -> None:
        try:
            with workspace_lock(workspace, timeout_seconds=0.05):
                pass
        except WorkspaceLockTimeout:
            timed_out.set()

    with workspace_lock(workspace):
        thread = threading.Thread(target=contender)
        thread.start()
        assert timed_out.wait(1)

    thread.join(timeout=1)


def test_workspace_lock_blocks_other_processes(tmp_path: Path):
    workspace = tmp_path / "ws"
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()

    with workspace_lock(workspace):
        process = ctx.Process(
            target=_workspace_lock_process_contender,
            args=(str(workspace), queue),
        )
        process.start()
        assert queue.get(timeout=10) == "started"
        result = queue.get(timeout=10)
        assert result == "timeout", result

    process.join(timeout=10)
    assert process.exitcode == 0
    queue.close()

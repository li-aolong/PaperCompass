from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, IO, Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None


STOPWORDS = {
    "the", "and", "for", "with", "using", "based", "from", "into", "towards", "toward",
    "this", "that", "than", "are", "can", "via", "its", "our", "your", "their", "a", "an",
    "of", "in", "on", "to", "by", "as", "is", "at", "or", "be",
}

DERIVED_TAG_PREFIXES = ("confidence:", "review:")
DEFAULT_LOCK_TIMEOUT_SECONDS = 300.0
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[Path, threading.RLock] = {}
_THREAD_LOCKS = threading.local()


class WorkspaceLockTimeout(TimeoutError):
    """Raised when a workspace/file lock cannot be acquired before timeout."""


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


def _fsync_dir(path: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0)
    if not flags:
        return
    try:
        fd = os.open(str(path), os.O_RDONLY | flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lock_timeout_seconds(value: float | int | None = None) -> float | None:
    if value is not None:
        return float(value)
    raw = clean_text(os.environ.get("PAPERCOMPASS_LOCK_TIMEOUT_SECONDS"))
    if not raw:
        return DEFAULT_LOCK_TIMEOUT_SECONDS
    try:
        parsed = float(raw)
    except ValueError:
        return DEFAULT_LOCK_TIMEOUT_SECONDS
    return None if parsed <= 0 else parsed


def _deadline(timeout_seconds: float | None) -> float | None:
    return None if timeout_seconds is None else time.monotonic() + max(0.0, timeout_seconds)


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def _acquire_file_lock(handle: IO[str], *, timeout_seconds: float | None = None) -> None:
    if fcntl is not None:
        if timeout_seconds is None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            return
        deadline = _deadline(timeout_seconds)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError as exc:
                if _remaining(deadline) <= 0:
                    raise WorkspaceLockTimeout("file lock timeout") from exc
                time.sleep(min(0.05, _remaining(deadline) or 0.05))
        return
    if msvcrt is not None:  # pragma: no cover - Windows-only
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("0")
            handle.flush()
        handle.seek(0)
        if timeout_seconds is None:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            return
        deadline = _deadline(timeout_seconds)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if _remaining(deadline) <= 0:
                    raise WorkspaceLockTimeout("file lock timeout") from exc
                time.sleep(min(0.05, _remaining(deadline) or 0.05))
        return
    raise RuntimeError("file locking is unavailable on this platform")


def _release_file_lock(handle: IO[str]) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - Windows-only
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _process_lock_for(path: Path) -> threading.RLock:
    key = path.expanduser().resolve(strict=False)
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _thread_locks() -> dict[Path, tuple[int, IO[str], threading.RLock]]:
    locks = getattr(_THREAD_LOCKS, "locks", None)
    if locks is None:
        locks = {}
        _THREAD_LOCKS.locks = locks
    return locks


@contextlib.contextmanager
def _path_file_lock(path: Path, *, timeout_seconds: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    key = path.expanduser().resolve(strict=False)
    timeout_seconds = _lock_timeout_seconds(timeout_seconds)
    deadline = _deadline(timeout_seconds)
    thread_locks = _thread_locks()
    existing = thread_locks.get(key)
    if existing is not None:
        depth, handle, process_lock = existing
        thread_locks[key] = (depth + 1, handle, process_lock)
        try:
            yield handle
        finally:
            depth, handle, process_lock = thread_locks[key]
            if depth > 1:
                thread_locks[key] = (depth - 1, handle, process_lock)
            else:
                del thread_locks[key]
        return

    process_lock = _process_lock_for(key)
    if timeout_seconds is None:
        acquired = process_lock.acquire()
    else:
        acquired = process_lock.acquire(timeout=_remaining(deadline) or 0.0)
    if not acquired:
        raise WorkspaceLockTimeout(f"lock timeout: {key}")
    handle = key.open("a+", encoding="utf-8")
    try:
        _acquire_file_lock(handle, timeout_seconds=_remaining(deadline))
        thread_locks[key] = (1, handle, process_lock)
        try:
            yield handle
        finally:
            del thread_locks[key]
            _release_file_lock(handle)
    finally:
        handle.close()
        process_lock.release()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


@contextlib.contextmanager
def _file_lock(path: Path, *, timeout_seconds: float | None = None):
    with _path_file_lock(path, timeout_seconds=timeout_seconds) as handle:
        yield handle


@contextlib.contextmanager
def workspace_lock(workspace: Path, *, timeout_seconds: float | None = None):
    canonical = workspace.expanduser().resolve(strict=False)
    workspace_key = hashlib.sha1(os.path.normcase(str(canonical)).encode("utf-8")).hexdigest()
    lock_path = Path(tempfile.gettempdir()) / "papercompass-locks" / f"{workspace_key}.lock"
    with _path_file_lock(lock_path, timeout_seconds=timeout_seconds):
        yield


def write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


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
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
        return count
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def append_jsonl_locked(path: Path, rows: Iterable[Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    count = 0
    with _file_lock(lock_path):
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
    _fsync_dir(path.parent)
    return count


def append_text_locked(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _file_lock(lock_path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    _fsync_dir(path.parent)

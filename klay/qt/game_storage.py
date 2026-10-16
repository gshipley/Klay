"""Serialize metadata updates made by the UI and import subprocess."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable


@contextmanager
def game_file_lock(path: Path):
    # Keep the lock inode separate from the JSON inode replaced by atomic saves.
    # Never unlink it: another process may already be waiting on this inode.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(f".{path.name}.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_game_file(path: Path) -> dict[str, Any] | None:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as error:
        raise OSError(f"Invalid game metadata: {path}") from error
    if not isinstance(current, dict):
        raise OSError(f"Invalid game metadata: {path}")
    return current


def read_game_file(path: Path) -> dict[str, Any] | None:
    """Take a current record snapshot without holding the lock during launch."""
    with game_file_lock(path):
        return _read_game_file(path)


def update_game_file(
    path: Path, update: Callable[[dict[str, Any] | None], dict[str, Any]],
    *, existing_only: bool = False,
) -> dict[str, Any] | None:
    """Read, merge, and atomically replace one record while holding its lock."""
    with game_file_lock(path):
        current = _read_game_file(path)
        if current is None and existing_only:
            return None
        result = update(current)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=f"{path.stem}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(result, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return result

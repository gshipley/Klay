"""Client for Klay's isolated, standard-library-only host jail helper.

The helper's source is passed as a Python program when Klay runs in Flatpak:
paths under /app are not available to host processes. Game data is always JSON
on stdin, never interpolated into Python or a shell command.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any


class JailError(OSError):
    """Isolation could not be established; an ordinary launch is forbidden."""


def _helper_command(operation: str, game_id: str, data_dir_name: str) -> list[str]:
    if not sys.platform.startswith("linux"):
        raise JailError("Game jails are available on Linux only.")
    helper = Path(__file__).with_name("jail_helper.py")
    arguments = [
        "--operation", operation, "--game-id", game_id,
        "--data-dir-name", data_dir_name,
    ]
    if os.getenv("FLATPAK_ID"):
        return [
            "flatpak-spawn", "--host", "--watch-bus", "python3", "-I", "-c",
            helper.read_text(encoding="utf-8"), *arguments,
        ]
    return [sys.executable, "-I", str(helper), *arguments]


def _stop(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _read_response(process: subprocess.Popen[Any], timeout: float) -> dict[str, Any]:
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    chunks = bytearray()
    while b"\n" not in chunks:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise JailError("Jail preparation timed out. The game was not launched normally.")
        readable, _, _ = select.select([process.stdout], [], [], min(remaining, 1))
        if not readable:
            continue
        data = os.read(process.stdout.fileno(), 8192)
        if not data:
            if not chunks:
                raise JailError("The host jail helper exited without confirming isolation.")
            break
        chunks.extend(data)
        if len(chunks) > 256 * 1024:
            raise JailError("The host jail helper returned an invalid response.")
    try:
        response = json.loads(bytes(chunks).split(b"\n", 1)[0])
    except (ValueError, UnicodeError) as error:
        raise JailError("The host jail helper returned an invalid response.") from error
    if not isinstance(response, dict) or response.get("ok") is not True:
        message = response.get("error") if isinstance(response, dict) else None
        raise JailError(str(message or "The jail helper could not establish isolation."))
    return response


def _start(
    operation: str, config: dict[str, Any], *, game_id: str,
    data_dir_name: str, timeout: float,
) -> tuple[subprocess.Popen[Any], dict[str, Any]]:
    command = _helper_command(operation, game_id, data_dir_name)
    # A file prevents stderr pipe backpressure during long preparations/runs.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errors, start_new_session=True,
        )
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps(config).encode("utf-8"))
            process.stdin.close()
            response = _read_response(process, timeout)
            if operation != "launch":
                try:
                    result = process.wait(timeout=5)
                except subprocess.TimeoutExpired as error:
                    raise JailError("The jail helper did not finish its request.") from error
                if result:
                    raise JailError("The jail helper failed its request.")
            elif response.get("status") != "running":
                raise JailError("The jail helper did not confirm the game was started.")
            process.jail_info = response  # type: ignore[attr-defined]
            return process, response
        except BaseException as error:
            _stop(process)
            if not isinstance(error, (OSError, ValueError, TypeError)):
                raise
            errors.seek(0)
            detail = errors.read(4096).decode("utf-8", errors="replace").strip()
            if isinstance(error, JailError):
                if detail:
                    raise JailError(f"{error}\n{detail}") from error
                raise
            raise JailError(f"Jail launch failed: {error}" + (f"\n{detail}" if detail else "")) from error
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()


def launch_game(
    config: dict[str, Any], *, game_id: str, data_dir_name: str = "klay",
) -> subprocess.Popen[Any]:
    process, _ = _start(
        "launch", config, game_id=game_id, data_dir_name=data_dir_name,
        timeout=900,
    )
    return process


def stop_game(process: subprocess.Popen[Any]) -> None:
    """Stop and reap the supervisor, including a Flatpak host bridge."""
    _stop(process)


def stop_jail(config: dict[str, Any], *, game_id: str, data_dir_name: str = "klay") -> dict[str, Any]:
    """Stop a verified host jail, including runs from a previous app session."""
    _, response = _start("stop", config, game_id=game_id, data_dir_name=data_dir_name, timeout=15)
    return response


def check_game(
    config: dict[str, Any], *, game_id: str, data_dir_name: str = "klay",
) -> dict[str, Any]:
    _, response = _start(
        "check", config, game_id=game_id, data_dir_name=data_dir_name,
        timeout=45,
    )
    return response


def game_report(
    config: dict[str, Any], *, game_id: str, data_dir_name: str = "klay",
) -> dict[str, Any]:
    _, response = _start(
        "report", config, game_id=game_id, data_dir_name=data_dir_name,
        timeout=15,
    )
    return response


def open_folder(path: str) -> None:
    if not path or not Path(path).is_absolute():
        raise JailError("The jail helper returned an invalid folder path.")
    command = ["xdg-open", path]
    if os.getenv("FLATPAK_ID"):
        command = ["flatpak-spawn", "--host", *command]
    subprocess.Popen(command, start_new_session=True)

#!/usr/bin/env python3
"""Build and run a trusted Windows D3D11/12 probe in temporary jail storage."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from klay.qt import jail_helper as helper


def run_probe(runner: Path, expected_vendor: int | None, timeout: int) -> dict:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("This probe currently supports x86_64 Linux hosts only.")
    missing = [tool for tool in ("gcc", "ld") if shutil.which(tool) is None]
    if missing:
        raise RuntimeError("Install the manual probe's build dependencies: " + ", ".join(missing))
    source = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="klay-d3d-probe-") as temporary:
        root = Path(temporary)
        game = root / "source"
        game.mkdir()
        commands = [
            ["gcc", "-c", "-Os", "-ffreestanding", "-fno-ident", "-fno-stack-protector", "-fno-pie", "-mno-red-zone", "-fno-asynchronous-unwind-tables", "-fno-unwind-tables", str(source / "probe.c"), "-o", "probe.o"],
            ["gcc", "-c", str(source / "imports.s"), "-o", "imports.o"],
            ["ld", "-mi386pep", "--image-base", "0x400000", "--disable-dynamicbase", "--disable-reloc-section", "--subsystem", "console", "--entry", "entry", "probe.o", "imports.o", "-o", str(game / "graphics-probe.exe")],
        ]
        for command in commands:
            subprocess.run(command, cwd=root, check=True, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        executable_hash = hashlib.sha256((game / "graphics-probe.exe").read_bytes()).digest()
        config = helper.validate_config({
            "enabled": True, "game_dir": str(game), "exe": "graphics-probe.exe",
            "runner_type": "proton", "runner": str(runner), "arguments": [],
        })
        store = helper.Store("manual-d3d-graphics-probe", "klay-test", home=root)
        try:
            preflight = helper.check(config, store)
            helper.prepare_game(store, config)
            command = helper.build_command(config, store.path, store.path)
            insertion = command.index("--")
            command[insertion:insertion] = ["--setenv", "VKD3D_DEBUG", "warn", "--setenv", "DXVK_LOG_LEVEL", "warn"]
            # Production mount restrictions are used as-is. There is no switch
            # to expose extra host paths or relax the jail for this probe.
            with tempfile.TemporaryFile() as log:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    exit_code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    raise RuntimeError(f"The trusted graphics probe exceeded {timeout} seconds.")
                finally:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)
                log.seek(0, os.SEEK_END)
                size = log.tell()
                log.seek(max(0, size - 12000))
                diagnostic_tail = log.read().decode(errors="replace")
            output = store.path / "game" / "graphics-result.txt"
            results = output.read_text(encoding="ascii") if output.exists() else ""
            required = (
                "D3D11CreateDevice=0x00000000", "D3D12CreateDevice=0x00000000",
                "D3D12DefaultAdapter=0x00000000",
            )
            passed = exit_code == 0 and all(marker in results for marker in required)
            if expected_vendor is not None:
                passed = passed and f"DXGIAdapterVendorID=0x{expected_vendor:08x}" in results
            source_untouched = (
                sorted(path.name for path in game.iterdir()) == ["graphics-probe.exe"]
                and hashlib.sha256((game / "graphics-probe.exe").read_bytes()).digest() == executable_hash
            )
            return {
                "ok": passed and source_untouched, "exit_code": exit_code,
                "source_untouched": source_untouched, "results": results.strip(),
                "graphics_preflight": preflight.get("graphics"),
                "diagnostic_tail": "" if passed else diagnostic_tail,
            }
        finally:
            store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runner", type=Path, help="Installed trusted Proton build directory containing proton")
    parser.add_argument("--expected-vendor", type=lambda value: int(value, 0), help="Optional DXGI vendor ID, e.g. 0x10de for NVIDIA")
    parser.add_argument("--timeout", type=int, default=150, help="Probe timeout in seconds (default: 150)")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error("--timeout must be between 1 and 600 seconds")
    if args.expected_vendor is not None and not 0 <= args.expected_vendor <= 0xffffffff:
        parser.error("--expected-vendor must be an unsigned 32-bit identifier")
    try:
        result = run_probe(args.runner.expanduser().resolve(), args.expected_vendor, args.timeout)
    except (helper.JailError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        detail = str(error)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            detail += ": " + error.stderr.decode(errors="replace")[-2000:].strip()
        print(json.dumps({"ok": False, "error": detail}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

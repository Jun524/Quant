"""Inspect macOS caffeinate guards without trusting stale state-file PIDs."""

from __future__ import annotations

from pathlib import Path
import shlex
import shutil
import subprocess


REQUIRED_ASSERTIONS = frozenset("ims")


def inspect_caffeinate_guard(
    watched_pid: int | None,
    *,
    ps_output: str | None = None,
    executable: str | None = None,
) -> dict:
    """Return guards whose ``-w`` target and assertion flags are exact matches."""
    available_path = executable if executable is not None else shutil.which("caffeinate")
    available = bool(available_path)
    result = {
        "available": available,
        "watched_pid": watched_pid,
        "required_assertions": sorted(REQUIRED_ASSERTIONS),
        "running": False,
        "guards": [],
    }
    if not available or not watched_pid or watched_pid < 1:
        return result
    if ps_output is None:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            result["inspection_error"] = "ps_failed"
            return result
        ps_output = completed.stdout

    for raw_line in ps_output.splitlines():
        fields = raw_line.strip().split(maxsplit=1)
        if len(fields) != 2:
            continue
        try:
            pid = int(fields[0])
            argv = shlex.split(fields[1])
        except (ValueError, IndexError):
            continue
        if not argv or Path(argv[0]).name != "caffeinate":
            continue
        try:
            wait_index = argv.index("-w")
            target = int(argv[wait_index + 1])
        except (ValueError, IndexError):
            continue
        if target != watched_pid:
            continue
        assertions: set[str] = set()
        for argument in argv[1:wait_index]:
            if argument.startswith("-"):
                assertions.update(character for character in argument[1:] if character in "dimsu")
        valid = REQUIRED_ASSERTIONS.issubset(assertions)
        result["guards"].append(
            {
                "pid": pid,
                "assertions": sorted(assertions),
                "valid": valid,
            }
        )
    result["running"] = any(item["valid"] for item in result["guards"])
    return result

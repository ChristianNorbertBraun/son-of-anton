"""Run a process with bounded output, a timeout and cooperative cancel. Output goes to a temp file,
never into unbounded memory buffers (a hostile postinstall could print gigabytes)."""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from .models import Cancelled


def run_capped(cmd: list[str], *, cwd: Path | None, env: dict, timeout: float, limit: int = 64_000,
               tail: bool = True, stdin_text: str | None = None,
               should_cancel: Callable[[], bool] = lambda: False) -> tuple[int, str, bool]:
    """Return (exit_code, output, truncated). tail=True keeps the LAST `limit` bytes, else the first.
    Exit code 124 means timeout. Raises Cancelled if should_cancel() turns true."""
    with tempfile.TemporaryFile() as out:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                             stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        if stdin_text is not None:
            try:
                p.stdin.write(stdin_text.encode())
                p.stdin.close()
            except BrokenPipeError:
                pass
        deadline = time.monotonic() + timeout
        code = None
        while code is None:
            try:
                code = p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if should_cancel():
                    _kill(p)
                    raise Cancelled() from None
                if time.monotonic() > deadline:
                    _kill(p)
                    code = 124
        size = out.seek(0, os.SEEK_END)
        truncated = size > limit
        out.seek(max(0, size - limit) if tail else 0)
        return code, out.read(limit).decode(errors="replace"), truncated


def _kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, 9)
    except ProcessLookupError:
        pass
    p.wait()

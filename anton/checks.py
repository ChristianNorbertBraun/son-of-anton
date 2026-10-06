"""Run repo checks and compare against the baseline taken before Claude touched anything."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Check


@dataclass(frozen=True)
class CheckResult:
    name: str
    kind: str
    exit_code: int
    metric: int | None  # parsed number of problems, None if not measurable
    tail: str  # last lines of output for the report

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


def parse_metric(check: Check, output: str, exit_code: int) -> int | None:
    if check.metric:
        m = re.search(check.metric, output)
        if m:
            return int(m.group(1))
    return 0 if exit_code == 0 else None


def run_check(check: Check, cwd: Path, env: dict, timeout: int = 900,
              wrap: Callable[[list[str]], list[str]] = lambda cmd: cmd) -> CheckResult:
    try:
        p = subprocess.run(wrap(["bash", "-c", check.cmd]), cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=timeout)
        out, code = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        out, code = f"timeout after {timeout}s", 124
    tail = "\n".join(out.strip().splitlines()[-12:])
    return CheckResult(check.name, check.kind, code, parse_metric(check, out, code), tail)


def compare(baseline: dict[str, CheckResult], after: dict[str, CheckResult]) -> tuple[bool, list[str]]:
    """Return (ok, lines). Gates must pass; regression checks must not get worse."""
    ok, lines = True, []
    for name, a in after.items():
        b = baseline.get(name)
        if a.kind == "gate":
            lines.append(f"- {name}: {'passed' if a.passed else 'FAILED'}")
            ok = ok and a.passed
            continue
        if a.metric is None or b is None or b.metric is None:
            lines.append(f"- {name}: not comparable (exit {a.exit_code})")
            continue
        worse = a.metric > b.metric
        arrow = f"{b.metric} -> {a.metric}"
        lines.append(f"- {name}: {arrow} ({'WORSE' if worse else 'not worse'})")
        ok = ok and not worse
    return ok, lines

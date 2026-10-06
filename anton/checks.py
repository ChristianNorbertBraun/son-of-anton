"""Run repo checks and compare against the baseline taken before Claude touched anything."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Check
from .proc import run_capped


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
    if check.metric_lines:  # one line per problem (e.g. prettier's "[warn] <file>")
        count = len(re.findall(check.metric_lines, output, re.M))
        if count:
            return count
    if check.metric:
        m = re.search(check.metric, output)
        if m:
            return int(m.group(1))
    return 0 if exit_code == 0 else None


def run_check(check: Check, cwd: Path, env: dict, timeout: int = 900,
              wrap: Callable[[list[str]], list[str]] = lambda cmd: cmd,
              should_cancel: Callable[[], bool] = lambda: False) -> CheckResult:
    code, out, _ = run_capped(wrap(["bash", "-c", check.cmd]), cwd=cwd, env=env, timeout=timeout,
                              should_cancel=should_cancel)
    if code == 124:
        out += f"\ntimeout after {timeout}s"
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
            if b is not None and b.passed and not a.passed:
                # clean before, broken now: worse, even if the problems cannot be counted
                lines.append(f"- {name}: passed -> FAILED (WORSE)")
                ok = False
            else:
                lines.append(f"- {name}: not comparable (exit {a.exit_code})")
            continue
        worse = a.metric > b.metric
        arrow = f"{b.metric} -> {a.metric}"
        lines.append(f"- {name}: {arrow} ({'WORSE' if worse else 'not worse'})")
        ok = ok and not worse
    return ok, lines

"""Self-update through agent-tool-selfupdate (github.com/ChristianNorbertBraun/agent-tool-selfupdate).

The library is not part of this repository: an installation puts the newest release that selfupdate.toml allows
next to this package, so it is imported only when an update function runs. The service, its jobs and the tests
work without it. What an update checks before it switches (tests in the sandbox, a probe of the new updater,
config check, running jobs first, health check and rollback) is described in the library's README."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import UpdateConfig

PACKAGE = "anton"
STATE = Path.home() / ".local/state/son-of-anton"  # the same as `state` in selfupdate.toml
MISSING = ("the updater library agent-tool-selfupdate is not installed next to this version; "
           "install it once as described in its README (\"First installation\")")


class UpdateRefused(Exception):
    """No update is possible. The text is safe to show."""


def _lib():
    try:
        import agent_tool_selfupdate as su
    except ImportError:
        raise UpdateRefused(MISSING) from None
    return su


def _layout(su, root: Path | None):
    return su.Layout(root) if root else su.Layout()  # the home of the service user


def installed() -> str:
    """The release directory this code runs from, e.g. 0.3.0+u1.0.0 (in a checkout: its folder name)."""
    return Path(__file__).resolve().parents[1].name


def mark_running(state: Path = STATE) -> None:
    """The service records which release it runs, so the updater can see the restart worked."""
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    (state / "running-version").write_text(installed())


def paused(state: Path = STATE) -> bool:
    """True while an update waits for running jobs: the pool starts nothing new."""
    try:
        import agent_tool_selfupdate as su
    except ImportError:
        return False  # without the library no update can be waiting
    return su.paused(state)


def last_result(state: Path = STATE) -> str:
    try:
        import agent_tool_selfupdate as su
    except ImportError:
        return ""
    return su.last_result(state)


def plan(cfg: UpdateConfig, to: str | None = None, fetch: Callable[[str], Any] | None = None,
         root: Path | None = None):
    """What an update would install, compared with the release this code runs from: .summary(), .notes(),
    .newer, .target. Downloads no archive."""
    su = _lib()
    try:
        extra = {"fetch": fetch} if fetch else {}
        return su.plan(su.Source(cfg.repo, cfg.publisher), _layout(su, root), to=to, installed=installed(), **extra)
    except su.UpdateRefused as e:
        raise UpdateRefused(str(e)) from None


def spawn() -> None:
    """Start `anton update --yes` in its own systemd unit, so restarting the service does not kill the update."""
    su = _lib()
    try:
        su.spawn(PACKAGE)
    except su.UpdateRefused as e:
        raise UpdateRefused(str(e)) from None


def update(cfg: UpdateConfig, running_jobs: Callable[[], int], notify: Callable[[str], None], to: str | None = None,
           force: bool = False, wait_seconds: float = 1800, state: Path = STATE, root: Path | None = None,
           **deps: Any) -> str:
    """Install the newest release; running jobs finish first and the user gets a Telegram message with the result.
    Returns the status: updated | current | failed | rolled-back."""
    su = _lib()
    result = su.update(su.Source(cfg.repo, cfg.publisher), _layout(su, root),
                       su.Deps(running_jobs=running_jobs, notify=lambda text: notify(f"Son of Anton: {text}"), **deps),
                       to=to, force=force, wait_seconds=wait_seconds, state_dir=state)
    return result.status

"""Self-update. A release is published by the owner on GitHub (tag vX.Y.Z); this module installs it.

Trust model: whoever can publish a release on the configured repository as the configured login. Everything
else is about not breaking a working installation: the new code is unpacked next to the old one, its tests run
in the sandbox, its config check runs against the real config, running jobs finish first, the switch is one
atomic symlink change, and a failed health check switches back."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from . import sandbox
from .config import UpdateConfig
from .version import TAG_RE, __version__, parse

KEEP = 3                      # installed versions kept on disk (plus the current and previous one)
MAX_ARCHIVE = 30 * 1024 * 1024
MAX_UNPACKED = 80 * 1024 * 1024
MAX_MEMBERS = 5000
PAUSE_MAX_AGE = 3 * 3600      # a pause file left behind by a crashed updater stops counting after this
SERVICE = "anton.service"
VERSION_LINE = re.compile(r'__version__ = "([^"]+)"')


class UpdateRefused(Exception):
    """The release or the archive is not acceptable. The text is shown to the user."""


@dataclass(frozen=True)
class Layout:
    root: Path = field(default_factory=Path.home)

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def state(self) -> Path:
        return self.root / ".local/state/son-of-anton"

    @property
    def pause(self) -> Path:
        return self.state / "pause"

    @property
    def running(self) -> Path:
        return self.state / "running-version"


@dataclass(frozen=True)
class Release:
    tag: str
    version: tuple[int, int, int]
    notes: str
    url: str

    @property
    def name(self) -> str:
        return self.tag[1:]


@dataclass(frozen=True)
class Result:
    status: str  # updated | current | failed | rolled-back
    message: str


def paused(layout: Layout | None = None, now: Callable[[], float] = time.time) -> bool:
    """True while an update waits for running jobs: the pool starts nothing new."""
    pause = (layout or Layout()).pause
    try:
        return now() - pause.stat().st_mtime < PAUSE_MAX_AGE
    except OSError:
        return False


def mark_running(layout: Layout | None = None) -> None:
    """The daemon records which version it runs, so the updater can see the restart worked."""
    layout = layout or Layout()
    layout.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    layout.running.write_text(__version__)


# ---- finding and checking a release -------------------------------------------------------------

def _get(url: str, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "son-of-anton-updater",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise UpdateRefused("download is too large")
    return data


def get_json(url: str) -> dict:
    return json.loads(_get(url, 1_000_000))


def get_bytes(url: str) -> bytes:
    return _get(url, MAX_ARCHIVE)


def release_from_api(data: dict, cfg: UpdateConfig) -> Release:
    tag = data.get("tag_name")
    if not (isinstance(tag, str) and TAG_RE.fullmatch(tag)):
        raise UpdateRefused("the release tag is not vX.Y.Z")
    if data.get("draft") or data.get("prerelease"):
        raise UpdateRefused("the release is a draft or pre-release")
    author = data.get("author") or {}
    login = str(author.get("login", ""))
    if author.get("type") != "User" or login.lower() != cfg.publisher.lower():
        raise UpdateRefused(f"the release was not published by {cfg.publisher}")
    return Release(tag, parse(tag), str(data.get("body") or "")[:4000],  # type: ignore[arg-type]
                   f"https://github.com/{cfg.repo}/archive/refs/tags/{tag}.tar.gz")


def find_release(cfg: UpdateConfig, fetch: Callable[[str], dict] = get_json, to: str | None = None) -> Release:
    if to is not None and not TAG_RE.fullmatch(to):
        raise UpdateRefused("--to must look like v1.2.3")
    path = f"releases/tags/{to}" if to else "releases/latest"
    try:
        return release_from_api(fetch(f"https://api.github.com/repos/{cfg.repo}/{path}"), cfg)
    except (OSError, ValueError) as e:  # no network, 404 (no release yet), bad JSON
        raise UpdateRefused(f"could not read the release: {type(e).__name__}") from None


def is_newer(release: Release, installed: str = __version__) -> bool:
    return release.version > (parse(installed) or (0, 0, 0))


# ---- unpacking and verifying -----------------------------------------------------------------------

def unpack(data: bytes, dest: Path) -> Path:
    """Unpack a GitHub source archive into `dest` and return its single top directory. Only plain files and
    directories are accepted; absolute paths, `..`, links and devices refuse the whole archive."""
    dest.mkdir(parents=True)
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError):
        raise UpdateRefused("the download is not a valid archive") from None
    with tf:
        members = tf.getmembers()
        if not members or len(members) > MAX_MEMBERS:
            raise UpdateRefused("the archive has an unexpected number of files")
        tops, total = set(), 0
        for m in members:
            parts = PurePosixPath(m.name).parts
            if not parts or m.name.startswith("/") or ".." in parts:
                raise UpdateRefused(f"unsafe path in archive: {m.name[:60]!r}")
            if not (m.isreg() or m.isdir()):
                raise UpdateRefused(f"unsupported entry in archive: {m.name[:60]!r}")
            tops.add(parts[0])
            total += m.size
        if len(tops) != 1 or total > MAX_UNPACKED:
            raise UpdateRefused("the archive is not one project folder of reasonable size")
        for m in members:
            target = dest.joinpath(*PurePosixPath(m.name).parts)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tf.extractfile(m) as src, open(target, "wb") as out:  # type: ignore[union-attr]
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o755 if m.mode & 0o100 else 0o644)
    return dest / tops.pop()


def verify_tree(top: Path, release: Release) -> None:
    if not (top / "anton/__init__.py").is_file() or not (top / "tests").is_dir():
        raise UpdateRefused("the archive does not look like Son of Anton")
    try:
        found = VERSION_LINE.search((top / "anton/version.py").read_text())
    except OSError:
        found = None
    if not found or found.group(1) != release.name:
        raise UpdateRefused(f"anton/version.py does not say {release.name}")


def run_tests(top: Path) -> tuple[bool, str]:
    """The release's own unit tests, inside the sandbox: nothing of the new code runs unconfined before it passed."""
    script = "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests 2>&1 | tail -n 5; exit ${PIPESTATUS[0]}"
    env = {"PATH": sandbox.SANDBOX_PATH, "HOME": sandbox.SANDBOX_HOME, "LANG": "C.UTF-8"}
    p = subprocess.run(sandbox.wrap(["bash", "-c", script], top), cwd=top, env=env, capture_output=True,
                       text=True, timeout=900)
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-300:]


def config_check(top: Path) -> tuple[bool, str]:
    """Does the NEW code still understand the real config? (Runs the new code, but only reads the config.)"""
    p = subprocess.run([sys.executable, "-m", "anton", "config-check"], cwd=top,
                       env={**os.environ, "PYTHONPATH": str(top), "PYTHONDONTWRITEBYTECODE": "1"},
                       capture_output=True, text=True, timeout=60)
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-300:]


# ---- switching and restarting ----------------------------------------------------------------------

def current_name(layout: Layout) -> str | None:
    try:
        return layout.current.resolve().name if layout.current.is_symlink() else None
    except OSError:
        return None


def switch(layout: Layout, name: str) -> None:
    """Point `current` at releases/<name> in one atomic step."""
    tmp = layout.root / f".current-{os.getpid()}"
    tmp.unlink(missing_ok=True)
    os.symlink(layout.releases / name, tmp)
    os.replace(tmp, layout.current)


def prune(layout: Layout, keep: tuple[str, ...], limit: int = KEEP) -> None:
    versions = sorted((p for p in layout.releases.iterdir() if p.is_dir() and parse(p.name)),
                      key=lambda p: parse(p.name), reverse=True)  # type: ignore[arg-type,return-value]
    for p in versions[limit:]:
        if p.name not in keep:
            shutil.rmtree(p, ignore_errors=True)


def systemctl(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")}
    return subprocess.run(["systemctl", "--user", *args], env=env, capture_output=True, text=True, timeout=120)


def restart() -> None:
    p = systemctl("restart", SERVICE)
    if p.returncode != 0:
        raise UpdateRefused(f"could not restart the service: {p.stderr.strip()[:150]}")


def healthy(version: str, layout: Layout, timeout: float = 90, settle: float = 15,
            sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> bool:
    """The service is active, says it runs `version`, and has not crashed again for `settle` seconds."""
    deadline, since = clock() + timeout, None
    while clock() < deadline:
        try:
            says = layout.running.read_text().strip()
        except OSError:
            says = ""
        if systemctl("is-active", SERVICE).stdout.strip() == "active" and says == version:
            since = clock() if since is None else since
            if clock() - since >= settle:
                return True
        else:
            since = None
        sleep(2)
    return False


def spawn_update() -> None:
    """Run `anton update --yes` in its own systemd unit, so restarting the service does not kill the updater."""
    p = subprocess.run(["systemd-run", "--user", "--collect", "--quiet", "--unit=anton-update",
                        str(Path.home() / "bin/anton"), "update", "--yes"], capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise UpdateRefused("an update is already running" if "already" in p.stderr else "could not start the update")


# ---- the whole update ------------------------------------------------------------------------------

@dataclass
class Deps:
    get_json: Callable[[str], dict] = get_json
    get_bytes: Callable[[str], bytes] = get_bytes
    run_tests: Callable[[Path], tuple[bool, str]] = run_tests
    config_check: Callable[[Path], tuple[bool, str]] = config_check
    restart: Callable[[], None] = restart
    healthy: Callable[[str, Layout], bool] = healthy
    running_jobs: Callable[[], int] = lambda: 0
    notify: Callable[[str], None] = lambda text: None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    log: Callable[[str], None] = print


def update(cfg: UpdateConfig, layout: Layout, deps: Deps, to: str | None = None, force: bool = False,
           wait_seconds: float = 1800, installed: str = __version__) -> Result:
    """Install the newest release (or `to`). Never leaves a broken installation behind."""
    def done(status: str, message: str, notify: bool = True) -> Result:
        deps.log(message)
        if notify:
            deps.notify(f"Son of Anton: {message}")
        return Result(status, message)

    incoming = layout.releases / f".incoming-{os.getpid()}"
    try:
        release = find_release(cfg, deps.get_json, to)
        if not (is_newer(release, installed) or force):
            return done("current", f"already on {installed} (newest release: {release.tag})", notify=False)
        layout.releases.mkdir(parents=True, exist_ok=True)
        target = layout.releases / release.name
        previous = current_name(layout)
        if target.exists() and target.name == previous:
            raise UpdateRefused(f"{release.name} is the installed version")
        shutil.rmtree(incoming, ignore_errors=True)
        top = unpack(deps.get_bytes(release.url), incoming)
        verify_tree(top, release)
        deps.log(f"unpacked {release.tag}, running its tests in the sandbox")
        ok, out = deps.run_tests(top)
        if not ok:
            raise UpdateRefused(f"the tests of {release.tag} failed: {out[-200:]}")
        ok, out = deps.config_check(top)
        if not ok:
            raise UpdateRefused(f"{release.tag} cannot read the current config: {out[-200:]}")
        shutil.rmtree(target, ignore_errors=True)
        top.rename(target)
        shutil.rmtree(incoming, ignore_errors=True)

        layout.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        layout.pause.touch()
        try:
            end = deps.clock() + wait_seconds
            while deps.running_jobs() > 0:
                if deps.clock() > end:
                    raise UpdateRefused("jobs are still running; nothing was changed, try again later")
                deps.sleep(5)
            switch(layout, release.name)
            deps.log(f"switched to {release.name}, restarting")
            try:
                deps.restart()
                good = deps.healthy(release.name, layout)
            except UpdateRefused:
                good = False
            if good:
                prune(layout, keep=(release.name, previous or ""))
                return done("updated", f"updated {installed} -> {release.name}. {release.notes[:300]}".strip())
            if previous:
                switch(layout, previous)
                try:
                    deps.restart()
                except UpdateRefused:
                    pass
                back = deps.healthy(previous, layout)
                return done("rolled-back", f"{release.name} failed its health check; went back to {previous}"
                            + ("" if back else " (and that one did not come up either: check the service!)"))
            return done("failed", f"{release.name} failed its health check and there is no previous version to go back to")
        finally:
            layout.pause.unlink(missing_ok=True)
    except UpdateRefused as e:
        return done("failed", f"update refused: {e}")
    except Exception as e:  # nothing here may leave a half state without telling
        return done("failed", f"update failed: {type(e).__name__}: {str(e)[:150]}")
    finally:
        shutil.rmtree(incoming, ignore_errors=True)

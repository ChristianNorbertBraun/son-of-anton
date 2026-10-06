"""bubblewrap sandbox for everything that runs repo or model controlled code.

What the sandboxed process sees: the system read-only, a few /etc files, the job checkout
at /work (with .git read-only), a tmpfs home. It does NOT see ~anton, the app key, the
Claude token file, other jobs or other users' homes. The network stays open (Claude API, npm).
"""
from __future__ import annotations

import shutil
from pathlib import Path

WORK = "/work"
SANDBOX_HOME = "/home/sandbox"
CLAUDE_IN_SANDBOX = "/opt/claude/claude"  # not under /usr: that mount is read-only, bwrap cannot add files there
ETC_RO = ("ssl", "ca-certificates", "resolv.conf", "hosts", "nsswitch.conf", "passwd", "group",
          "ld.so.cache", "alternatives", "localtime")
SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin"


class SandboxError(Exception):
    pass


def available() -> bool:
    return shutil.which("bwrap") is not None


def wrap(cmd: list[str], work: Path, claude_bin: Path | None = None,
         etc_dir: Path = Path("/etc"), protected: tuple[Path, ...] = ()) -> list[str]:
    """Prefix `cmd` with a bwrap invocation. Fails closed if the checkout overlaps a protected dir."""
    work = Path(work).resolve()
    for p in protected:
        p = Path(p).resolve()
        if work == p or p in work.parents or work in p.parents:
            raise SandboxError(f"checkout {work} overlaps protected path {p}")
    args = [
        "bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", SANDBOX_HOME,
    ]
    for name in ETC_RO:
        src = Path(etc_dir) / name
        if src.exists():
            args += ["--ro-bind", str(src), f"/etc/{name}"]
    args += ["--bind", str(work), WORK]
    if (work / ".git").exists():
        # hooks/config written here would later run OUTSIDE the sandbox, with the GitHub token
        args += ["--ro-bind", str(work / ".git"), f"{WORK}/.git"]
    if claude_bin is not None:
        args += ["--ro-bind", str(Path(claude_bin).resolve()), CLAUDE_IN_SANDBOX]
    return args + ["--chdir", WORK, "--", *cmd]

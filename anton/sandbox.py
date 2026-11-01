"""bubblewrap sandbox for everything that runs repo or model controlled code.

What the sandboxed process sees: the system read-only, a few /etc files, the job checkout
at /work (with .git read-only), a small tmpfs home and /tmp. It does NOT see ~anton, the app
key, the Claude token file, other jobs or other users' homes. No nested user namespaces,
bounded tmpfs, file size and process count. The network stays open (Claude API, npm);
that includes loopback and the LAN, a known gap that needs an egress rule outside the sandbox.
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
TMP_BYTES = 512 * 1024 * 1024
HOME_BYTES = 64 * 1024 * 1024
MAX_FILE_KB = 1024 * 1024  # largest single file a sandboxed process may write (1 GiB)
MAX_PROCS = 1024
# Hidden from Claude in flow steps, which load the repo's CLAUDE.md: everything else that a checkout can use to
# configure Claude stays out of reach. A file becomes read-only and empty, writes to the directory vanish with it.
# Not /dev/null: a ro-bind is mounted nodev, so opening it there fails instead of reading nothing.
MASKED_DIRS = (".claude",)
MASKED_FILES = (".mcp.json", "CLAUDE.local.md")
MASK_BYTES = 1024 * 1024
# runs inside the sandbox before the real command: bound file size and process count
LIMIT_SCRIPT = f'ulimit -f {MAX_FILE_KB}; ulimit -u {MAX_PROCS}; exec "$@"'


class SandboxError(Exception):
    pass


def available() -> bool:
    return shutil.which("bwrap") is not None


def check_masks(work: Path) -> None:
    """A mount follows symlinks, and a mount of the wrong type stops bwrap: refuse before anything runs."""
    for names, is_kind in ((MASKED_DIRS, Path.is_dir), (MASKED_FILES, Path.is_file)):
        for name in names:
            path = work / name
            if path.is_symlink() or (path.exists() and not is_kind(path)):
                raise SandboxError(f"{name} in the checkout is not a plain {'directory' if names is MASKED_DIRS else 'file'}")


def wrap(cmd: list[str], work: Path, claude_bin: Path | None = None,
         etc_dir: Path = Path("/etc"), protected: tuple[Path, ...] = (), masks: bool = False) -> list[str]:
    """Prefix `cmd` with a bwrap invocation. Fails closed if the checkout overlaps a protected dir."""
    work = Path(work).resolve()
    for p in protected:
        p = Path(p).resolve()
        if work == p or p in work.parents or work in p.parents:
            raise SandboxError(f"checkout {work} overlaps protected path {p}")
    args = [
        "bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc",
        "--unshare-user", "--disable-userns",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--proc", "/proc", "--dev", "/dev",
        "--size", str(TMP_BYTES), "--tmpfs", "/tmp",
        "--size", str(HOME_BYTES), "--tmpfs", SANDBOX_HOME,
    ]
    for name in ETC_RO:
        src = Path(etc_dir) / name
        if src.exists():
            args += ["--ro-bind", str(src), f"/etc/{name}"]
    args += ["--bind", str(work), WORK]
    if (work / ".git").exists():
        # hooks/config written here would later run OUTSIDE the sandbox, with the GitHub token
        args += ["--ro-bind", str(work / ".git"), f"{WORK}/.git"]
    if masks:
        check_masks(work)
        for name in MASKED_DIRS:
            args += ["--size", str(MASK_BYTES), "--tmpfs", f"{WORK}/{name}"]
        empty = work.parent / "mask-empty"
        empty.touch(mode=0o400)
        for name in MASKED_FILES:
            args += ["--ro-bind", str(empty), f"{WORK}/{name}"]
    if claude_bin is not None:
        args += ["--ro-bind", str(Path(claude_bin).resolve()), CLAUDE_IN_SANDBOX]
    return args + ["--chdir", WORK, "--", "bash", "-c", LIMIT_SCRIPT, "_", *cmd]

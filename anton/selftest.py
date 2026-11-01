"""`anton selftest`: try to break out of the sandbox, the way a manipulated agent would.
It runs through the SAME functions the runner uses (sb_cmd, clean_env), not a copy of them."""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from . import gitops, runner, sandbox
from .proc import run_capped

CONF = runner.CONF_DIR

# flow steps load the repo's CLAUDE.md: what else a checkout could configure must be out of reach
MASK_SCRIPT = """
t() { n=$1; shift; if "$@" >/dev/null 2>&1; then echo "FAIL $n (was allowed)"; else echo "PASS $n"; fi; }
y() { n=$1; shift; if "$@" >/dev/null 2>&1; then echo "PASS $n"; else echo "FAIL $n (was blocked)"; fi; }
t mask-hides-planted-settings test -e /work/.claude/settings.json
t mask-mcp-is-empty           test -s /work/.mcp.json
t mask-mcp-refuses-writes     sh -c 'echo x > /work/.mcp.json'
t mask-local-refuses-writes   sh -c 'echo x > /work/CLAUDE.local.md'
y mask-mcp-is-readable        cat /work/.mcp.json
y mask-claude-dir-writable    touch /work/.claude/vanishes
y repo-instructions-visible   test -s /work/CLAUDE.md
"""


def check_masks(work: Path) -> list[str]:
    """Run MASK_SCRIPT on a checkout with planted files, then look at what is left on this side of the sandbox."""
    (work / ".claude").mkdir()
    (work / ".claude/settings.json").write_text('{"hooks": {}}')
    (work / ".mcp.json").write_text('{"mcpServers": {}}')
    (work / "CLAUDE.md").write_text("repo instructions\n")
    gitops.exclude_masked(work)
    _, out, _ = run_capped(runner.sb_cmd(work, ["bash", "-c", MASK_SCRIPT], masks=True), cwd=None,
                           env=runner.clean_env(), timeout=60, limit=8000)
    lines = [ln for ln in out.splitlines() if ln.startswith(("PASS", "FAIL"))]
    expected = re.findall(r"^[ty] (\S+)", MASK_SCRIPT, re.M)  # a sandbox that never started must not look like a pass
    lines += [f"FAIL {name} (did not run)" for name in expected if not any(name in ln for ln in lines)]
    lines.append("FAIL mask-writes-reach-the-checkout" if (work / ".claude/vanishes").exists()
                 or (work / ".mcp.json").read_text() != '{"mcpServers": {}}' else "PASS mask-writes-vanish")
    return lines


# t = must FAIL inside the sandbox, y = must SUCCEED, WARN lines are known gaps (not failures)
SCRIPT = f"""
t() {{ n=$1; shift; if "$@" >/dev/null 2>&1; then echo "FAIL $n (was allowed)"; else echo "PASS $n"; fi; }}
y() {{ n=$1; shift; if "$@" >/dev/null 2>&1; then echo "PASS $n"; else echo "FAIL $n (was blocked)"; fi; }}
t cannot-read-app-key        cat {CONF}/app-key.pem
t cannot-read-claude-token   cat {CONF}/claude-token
t cannot-read-allowlist      cat {CONF}/repos.toml
t cannot-list-anton-home     ls {runner.HOME}
t cannot-read-jobs-dir       ls {runner.JOBS_DIR}
t cannot-see-other-homes     sh -c 'ls /home | grep -v -x sandbox'
t cannot-write-usr           touch /usr/evil
t cannot-plant-git-hook      touch /work/.git/hooks/pre-commit
t cannot-edit-git-config     sh -c 'echo x >> /work/.git/config'
t cannot-touch-git-index     touch /work/.git/index
t cannot-rename-git-dir      mv /work/.git /work/.git.moved
t cannot-delete-git-dir      rm -rf /work/.git
t no-nested-userns           unshare --user true
t tmp-is-bounded             sh -c 'dd if=/dev/zero of=/tmp/big bs=1M count=700'
t no-secret-in-env           sh -c 'env | grep -i -E "token|secret|private|app_key"'
t no-secret-in-any-environ   sh -c 'cat /proc/*/environ 2>/dev/null | tr "\\0" "\\n" | grep -i -E "token|secret|private|app_key"'
y can-write-work             touch /work/ok
y can-write-tmp              touch /tmp/ok
y node-runs                  node -e 'process.exit(0)'
y network-works              node -e "fetch('https://api.github.com/zen').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
if bash -c 'exec 3<>/dev/tcp/127.0.0.1/22' 2>/dev/null; then echo "WARN sandbox can reach loopback services (known gap: needs an egress rule)"; fi
"""


def run() -> int:
    if not sandbox.available():
        print("FAIL bwrap not installed")
        return 1
    state = runner.HOME / ".local/state"
    state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=state) as tmp:
        work = Path(tmp) / "work"
        work.mkdir()
        gitops.git(["init", "-q", str(work)])
        (work / ".git/hooks").mkdir(exist_ok=True)
        # clean_env() is what install/check steps get: it must carry no token at all
        code, out, _ = run_capped(runner.sb_cmd(work, ["bash", "-c", SCRIPT]), cwd=None,
                                  env=runner.clean_env(), timeout=120, limit=20_000)
        lines = [ln for ln in out.splitlines() if ln.startswith(("PASS", "FAIL", "WARN"))]
        lines += check_masks(work)
        # the agent itself must start inside the sandbox (no token needed for --version)
        v = subprocess.run(runner.sb_cmd(work, [sandbox.CLAUDE_IN_SANDBOX, "--version"], with_claude=True),
                           env=runner.clean_env(), capture_output=True, text=True, timeout=60)
    lines.append("PASS claude-starts-in-sandbox" if v.returncode == 0 and "Claude Code" in v.stdout
                 else f"FAIL claude-starts-in-sandbox ({v.stderr.strip()[:120]})")
    print("\n".join(lines) or f"no output (exit {code})")
    checks = [ln for ln in lines if not ln.startswith("WARN")]
    failed = [ln for ln in checks if ln.startswith("FAIL")]
    warns = len(lines) - len(checks)
    print(f"-- {len(checks) - len(failed)}/{len(checks)} passed, {warns} known gap(s)")
    return 1 if failed or not checks else 0

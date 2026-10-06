"""`anton selftest`: try to break out of the sandbox, the way a manipulated agent would."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from . import gitops, runner, sandbox

CONF = runner.CONF_DIR

# t = must FAIL inside the sandbox, y = must SUCCEED
SCRIPT = f"""
t() {{ n=$1; shift; if "$@" >/dev/null 2>&1; then echo "FAIL $n (was allowed)"; else echo "PASS $n"; fi; }}
y() {{ n=$1; shift; if "$@" >/dev/null 2>&1; then echo "PASS $n"; else echo "FAIL $n (was blocked)"; fi; }}
t cannot-read-app-key        cat {CONF}/app-key.pem
t cannot-read-claude-token   cat {CONF}/claude-token
t cannot-read-allowlist      cat {CONF}/repos.toml
t cannot-list-anton-home     ls {runner.HOME}
t cannot-read-jobs-dir       ls {runner.JOBS_DIR}
t cannot-read-hermes-home    ls /home/hermes
t cannot-write-usr           touch /usr/evil
t cannot-plant-git-hook      touch /work/.git/hooks/pre-commit
t cannot-edit-git-config     sh -c 'echo x >> /work/.git/config'
t no-secret-in-env           sh -c 'env | grep -i -E "token|secret|private|app_key"'
y can-write-work             touch /work/ok
y can-write-tmp              touch /tmp/ok
y node-runs                  node -e 'process.exit(0)'
y network-works              node -e "fetch('https://api.github.com/zen').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
"""


def run() -> int:
    if not sandbox.available():
        print("FAIL bwrap not installed")
        return 1
    with tempfile.TemporaryDirectory(dir=runner.JOBS_DIR.parent / ".local/state") as tmp:
        work = Path(tmp) / "work"
        work.mkdir()
        gitops.git(["init", "-q", str(work)])
        (work / ".git/hooks").mkdir(exist_ok=True)
        env = {"PATH": sandbox.SANDBOX_PATH, "HOME": sandbox.SANDBOX_HOME, "LANG": "C.UTF-8"}
        cmd = sandbox.wrap(["bash", "-c", SCRIPT], work, protected=(CONF,))
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        # the agent itself must be able to start inside the sandbox (no token needed for --version)
        v = subprocess.run(sandbox.wrap([sandbox.CLAUDE_IN_SANDBOX, "--version"], work,
                                        claude_bin=runner.CLAUDE, protected=(CONF,)),
                           env=env, capture_output=True, text=True, timeout=60)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith(("PASS", "FAIL"))]
    lines.append("PASS claude-starts-in-sandbox" if v.returncode == 0 and "Claude Code" in v.stdout
                 else f"FAIL claude-starts-in-sandbox ({v.stderr.strip()[:120]})")
    print("\n".join(lines) or f"no output; stderr: {p.stderr[:300]}")
    failed = [ln for ln in lines if ln.startswith("FAIL")]
    print(f"-- {len(lines) - len(failed)}/{len(lines)} passed")
    return 1 if failed or not lines else 0

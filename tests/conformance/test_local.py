import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from anton.adapters.local import LocalCodeHost, LocalTracker

from .kit import Contract

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def bare_repo(root: Path) -> Path:
    """A bare repo with a first commit on main, and nothing else."""
    bare = root / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    env = {**os.environ, **IDENTITY}
    empty = subprocess.run(["git", "hash-object", "-t", "tree", "-w", "--stdin"], cwd=bare, env=env, input="",
                           capture_output=True, text=True, check=True).stdout.strip()
    sha = subprocess.run(["git", "commit-tree", empty, "-m", "first"], cwd=bare, env=env, capture_output=True,
                         text=True, check=True).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/heads/main", sha], cwd=bare, env=env, check=True)
    return bare


class LocalConformance(Contract, unittest.TestCase):
    def make(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bare = bare_repo(root)
        tracker, host = LocalTracker(root), LocalCodeHost(root, bare)
        env = {**os.environ, **IDENTITY}

        def push(branch):
            tree = subprocess.run(["git", "rev-parse", "main^{tree}"], cwd=bare, env=env, capture_output=True,
                                  text=True, check=True).stdout.strip()
            sha = subprocess.run(["git", "commit-tree", tree, "-p", "main", "-m", branch], cwd=bare, env=env,
                                 capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run(["git", "update-ref", f"refs/heads/{branch}", sha], cwd=bare, env=env, check=True)
            return sha
        owner = SimpleNamespace(add=tracker.owner_add, close=tracker.owner_close, push=push, review=host.owner_review,
                                note=host.owner_note, merge=host.owner_merge, comments=host.comments,
                                task_comments=tracker.comments, task_state=tracker.state_of)
        return tracker, host, owner


if __name__ == "__main__":
    unittest.main()

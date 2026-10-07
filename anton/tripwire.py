"""Last line of defence before a change leaves the machine: refuse anything that looks like a secret.
It checks the lines a change ADDS for (a) the exact secrets stored in the config directory and (b) the shape of
common tokens and private keys. Findings name the kind and the file, never the value."""
from __future__ import annotations

import re
from pathlib import Path

MIN_SECRET = 12  # shorter values are not secrets (a chat id, a port) and would only cause false alarms
SHAPES = (
    ("Claude token", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
)
SKIP_FILES = ("telegram-chat",)


def secret_values(conf_dir: Path) -> list[str]:
    """Every value stored in the config directory that must never appear in a change (files only, no nesting)."""
    values: list[str] = []
    try:
        files = [p for p in conf_dir.iterdir() if p.is_file() and p.name not in SKIP_FILES
                 and not p.name.endswith(".toml") and ".bak" not in p.name]
    except OSError:
        return values
    for p in files:
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        values += [text.strip()] + [line.strip() for line in text.splitlines()]
    return sorted({v for v in values if len(v) >= MIN_SECRET and not v.startswith("-----")})


def added_lines(patch: str) -> list[tuple[str, str]]:
    """(file, line) for every line the patch adds. Removed and context lines are ignored: deleting a leaked secret is fine."""
    out, path = [], ""
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((path, line[1:]))
    return out


def scan(patch: str, values: list[str]) -> list[str]:
    """Findings like 'Claude token in src/a.py'. Never contains the secret itself."""
    findings: list[str] = []
    for path, line in added_lines(patch):
        for value in values:
            if value in line:
                findings.append(f"a stored secret in {path}")
        for kind, shape in SHAPES:
            if shape.search(line):
                findings.append(f"{kind} in {path}")
    return sorted(set(findings))

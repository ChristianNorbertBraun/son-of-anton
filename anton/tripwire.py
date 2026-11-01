"""Last line of defence before a change leaves the machine: refuse anything that looks like a secret.
It checks the lines a change ADDS for (a) the exact secrets stored in the config directory and (b) the shape of
common tokens and private keys. Findings name the kind and the file, never the value."""
from __future__ import annotations

import re
from pathlib import Path

MIN_SECRET = 12  # shorter values are not secrets (a chat id, a port) and would only cause false alarms
MIN_PRIVATE = 4  # names and addresses are shorter than tokens; below this a line would match ordinary words
SHAPES = (
    ("Claude token", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
)
# private-strings is not a secret store: a personal site may contain the owner's name, so only flows read it
PRIVATE_FILE = "private-strings"
SKIP_FILES = ("telegram-chat", PRIVATE_FILE)


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


def private_strings(conf_dir: Path) -> list[str]:
    """Values that must not reach GitHub from a flow, one per line. `#` starts a comment line."""
    try:
        lines = (conf_dir / PRIVATE_FILE).read_text(errors="ignore").splitlines()
    except OSError:
        return []
    return sorted({v for v in (line.strip() for line in lines) if len(v) >= MIN_PRIVATE and not v.startswith("#")})


def added_lines(patch: str) -> list[tuple[str, str]]:
    """(file, line) for every line the patch adds. Removed and context lines are ignored: deleting a leaked secret is fine."""
    out, path = [], ""
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((path, line[1:]))
    return out


def _kinds(text: str, values: list[str], private: list[str]) -> list[str]:
    kinds = [kind for kind, shape in SHAPES if shape.search(text)]
    if any(value in text for value in values):
        kinds.append("a stored secret")
    folded = text.casefold()
    if any(value.casefold() in folded for value in private):
        kinds.append("a private string")
    return kinds


def scan(patch: str, values: list[str], private: list[str] | None = None) -> list[str]:
    """Findings like 'Claude token in src/a.py'. Never contains the secret itself."""
    return sorted({f"{kind} in {path}" for path, line in added_lines(patch)
                   for kind in _kinds(line, values, private or [])})


def scan_text(text: str, where: str, values: list[str], private: list[str] | None = None) -> list[str]:
    """The same check for text that is no patch (a title, a commit message, a comment). `where` names the place."""
    return sorted(f"{kind} in {where}" for kind in _kinds(text, values, private or []))

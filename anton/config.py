"""Repo allowlist. This file is a security boundary: keep it strict and small."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Hard rule: only github.com. Repos on any other host (e.g. a company GitHub Enterprise) are rejected.
ALLOWED_HOST = "github.com"
# GitHub owners: alphanumerics and hyphens. Repo names may contain . _ - but never be "." or "..".
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/(?!\.{1,2}$)[A-Za-z0-9_.-]+$")
BRANCH_PREFIX = "anton/"
DEFAULT_MODEL = "claude-opus-5-5"  # always Opus 5.5 for code changes
DEFAULT_PATH = Path.home() / ".config/son-of-anton/repos.toml"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Check:
    name: str
    cmd: str
    kind: str  # "gate" must pass; "regression" must not get worse than baseline
    metric: str | None = None  # regex, group 1 = integer (errors, files, ...)


@dataclass(frozen=True)
class RepoConfig:
    slug: str
    host: str
    installation_id: int
    mode: str
    base: str
    install: str | None
    checks: tuple[Check, ...]
    protected_paths: tuple[str, ...]
    allow_tools: tuple[str, ...]
    deny_tools: tuple[str, ...]
    max_turns: int
    timeout_minutes: int
    model: str


@dataclass(frozen=True)
class GithubConfig:
    app_id: int  # GitHub App id (not secret; the private key is)
    bot_name: str  # e.g. "my-anton[bot]", used as git author
    bot_id: int  # numeric id of the bot user, for the noreply email


def parse_github(data: dict) -> GithubConfig:
    g = data.get("github")
    if not g:
        raise ConfigError("[github] section missing (app_id, bot_name, bot_id)")
    try:
        return GithubConfig(int(g["app_id"]), str(g["bot_name"]), int(g["bot_id"]))
    except (KeyError, ValueError) as e:
        raise ConfigError(f"[github] invalid: {e}") from e


def load_github(path: Path = DEFAULT_PATH) -> GithubConfig:
    try:
        with open(path, "rb") as f:
            return parse_github(tomllib.load(f))
    except FileNotFoundError as e:
        raise ConfigError(f"allowlist not found: {path}") from e


@dataclass(frozen=True)
class DaemonConfig:
    max_parallel: int = 2
    daily_limit: int = 10  # jobs per rolling 24h, protects the Claude subscription limit
    poll_seconds: int = 120


def parse_daemon(data: dict) -> DaemonConfig:
    d = data.get("daemon", {})
    cfg = DaemonConfig(
        max_parallel=int(d.get("max_parallel", 2)),
        daily_limit=int(d.get("daily_limit", 10)),
        poll_seconds=int(d.get("poll_seconds", 120)),
    )
    if not (1 <= cfg.max_parallel <= 4):
        raise ConfigError("daemon.max_parallel must be between 1 and 4 (Pi has 8 GB RAM)")
    if cfg.daily_limit < 1 or cfg.poll_seconds < 30:
        raise ConfigError("daemon.daily_limit must be >= 1 and poll_seconds >= 30")
    return cfg


def load_daemon(path: Path = DEFAULT_PATH) -> DaemonConfig:
    try:
        with open(path, "rb") as f:
            return parse_daemon(tomllib.load(f))
    except FileNotFoundError as e:
        raise ConfigError(f"allowlist not found: {path}") from e


def _checks(raw: list[dict]) -> tuple[Check, ...]:
    out = []
    for c in raw:
        if c.get("kind") not in ("gate", "regression"):
            raise ConfigError(f"check {c.get('name')!r}: kind must be gate or regression")
        out.append(Check(c["name"], c["cmd"], c["kind"], c.get("metric")))
    return tuple(out)


def parse_repos(data: dict) -> dict[str, RepoConfig]:
    defaults = data.get("defaults", {})
    repos: dict[str, RepoConfig] = {}
    for slug, r in data.get("repos", {}).items():
        if not SLUG_RE.match(slug):
            raise ConfigError(f"invalid repo slug: {slug!r}")
        host = r.get("host", ALLOWED_HOST)
        if host != ALLOWED_HOST:
            raise ConfigError(f"{slug}: host {host!r} not allowed, only {ALLOWED_HOST}")
        if r.get("mode", "pr") != "pr":
            raise ConfigError(f"{slug}: mode {r.get('mode')!r} not implemented yet")
        repos[slug] = RepoConfig(
            slug=slug,
            host=host,
            installation_id=int(r["installation_id"]),
            mode="pr",
            base=r.get("base", "main"),
            install=r.get("install"),
            checks=_checks(r.get("checks", [])),
            protected_paths=tuple(r.get("protected_paths", [])),
            allow_tools=tuple(r.get("allow_tools", defaults.get("allow_tools", []))),
            deny_tools=tuple(r.get("deny_tools", defaults.get("deny_tools", []))),
            max_turns=int(r.get("max_turns", defaults.get("max_turns", 30))),
            timeout_minutes=int(r.get("timeout_minutes", defaults.get("timeout_minutes", 30))),
            model=r.get("model", defaults.get("model", DEFAULT_MODEL)),
        )
    return repos


def load_repos(path: Path = DEFAULT_PATH) -> dict[str, RepoConfig]:
    try:
        with open(path, "rb") as f:
            return parse_repos(tomllib.load(f))
    except FileNotFoundError as e:
        raise ConfigError(f"allowlist not found: {path}") from e


def get_repo(repos: dict[str, RepoConfig], slug: str) -> RepoConfig:
    if slug not in repos:
        raise ConfigError(f"{slug} is not in the allowlist")
    return repos[slug]

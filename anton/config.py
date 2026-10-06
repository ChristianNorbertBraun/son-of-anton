"""Allowlist and settings. This file is a security boundary: keep it strict and small."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Hard rule: only github.com. Repos on any other host (e.g. a company GitHub Enterprise) are rejected.
ALLOWED_HOST = "github.com"
# GitHub owners: alphanumerics and hyphens. Repo names may contain . _ - but never be "." or "..".
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.{1,2}\Z)[A-Za-z0-9_.-]+")
BRANCH_PREFIX = "anton/"
DEFAULT_MODEL = "claude-opus-5-5"  # always Opus 5.5 for code changes
DEFAULT_PATH = Path.home() / ".config/son-of-anton/repos.toml"

# A repo can ADD denies but never remove these, whatever the TOML says.
DENY_FLOOR = (
    "Bash(git:*)", "Bash(gh:*)", "Bash(curl:*)", "Bash(wget:*)", "Bash(ssh:*)", "Bash(scp:*)",
    "Bash(nc:*)", "Bash(sudo:*)", "Bash(npm run deploy:*)", "Bash(npm publish:*)",
    "WebFetch", "WebSearch",
)


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
    deny_tools: tuple[str, ...]  # always includes DENY_FLOOR
    max_turns: int
    timeout_minutes: int
    model: str


@dataclass(frozen=True)
class GithubConfig:
    app_id: int  # GitHub App id (not secret; the private key is)
    bot_name: str  # e.g. "my-anton[bot]", used as git author
    bot_id: int  # numeric id of the bot user, for the noreply email


@dataclass(frozen=True)
class DaemonConfig:
    max_parallel: int = 2
    daily_limit: int = 10  # jobs per rolling 24h, protects the Claude subscription limit
    max_queued: int = 20  # waiting jobs; bounds queue growth
    poll_seconds: int = 120


@dataclass(frozen=True)
class Settings:
    github: GithubConfig
    daemon: DaemonConfig
    repos: dict[str, RepoConfig]


def _int(value, where: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{where}: must be an integer") from e
    if not lo <= value <= hi:
        raise ConfigError(f"{where}: must be between {lo} and {hi}")
    return value


def _req(table: dict, key: str, where: str):
    if key not in table:
        raise ConfigError(f"{where}: missing '{key}'")
    return table[key]


def _str_list(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise ConfigError(f"{where}: must be a list of non-empty strings")
    return tuple(value)


def _checks(raw, where: str) -> tuple[Check, ...]:
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: must be an array of tables")
    out = []
    for i, c in enumerate(raw):
        w = f"{where}[{i}]"
        if not isinstance(c, dict):
            raise ConfigError(f"{w}: must be a table")
        name, cmd, kind = _req(c, "name", w), _req(c, "cmd", w), _req(c, "kind", w)
        if not (isinstance(name, str) and re.fullmatch(r"[a-z0-9_-]{1,40}", name)):
            raise ConfigError(f"{w}: name must match [a-z0-9_-]{{1,40}}")
        if not (isinstance(cmd, str) and cmd.strip()):
            raise ConfigError(f"{w}: cmd must be a non-empty string")
        if kind not in ("gate", "regression"):
            raise ConfigError(f"{w}: kind must be gate or regression")
        metric = c.get("metric")
        if metric is not None:
            try:
                re.compile(metric)
            except (re.error, TypeError) as e:
                raise ConfigError(f"{w}: metric is not a valid regex") from e
        out.append(Check(name, cmd, kind, metric))
    return tuple(out)


def parse_github(data: dict) -> GithubConfig:
    g = data.get("github")
    if not isinstance(g, dict):
        raise ConfigError("[github] section missing (app_id, bot_name, bot_id)")
    name = _req(g, "bot_name", "[github]")
    if not (isinstance(name, str) and name):
        raise ConfigError("[github].bot_name must be a non-empty string")
    return GithubConfig(_int(_req(g, "app_id", "[github]"), "[github].app_id", 1, 2**40),
                        name, _int(_req(g, "bot_id", "[github]"), "[github].bot_id", 1, 2**40))


def parse_daemon(data: dict) -> DaemonConfig:
    d = data.get("daemon", {})
    cfg = DaemonConfig(
        max_parallel=_int(d.get("max_parallel", 2), "daemon.max_parallel", 1, 4),  # Pi has 8 GB RAM
        daily_limit=_int(d.get("daily_limit", 10), "daemon.daily_limit", 1, 1000),
        max_queued=_int(d.get("max_queued", 20), "daemon.max_queued", 1, 200),
        poll_seconds=_int(d.get("poll_seconds", 120), "daemon.poll_seconds", 30, 86400),
    )
    return cfg


def parse_repos(data: dict) -> dict[str, RepoConfig]:
    defaults = data.get("defaults", {})
    d_allow = _str_list(defaults.get("allow_tools", []), "defaults.allow_tools")
    d_deny = _str_list(defaults.get("deny_tools", []), "defaults.deny_tools")
    d_protected = _str_list(defaults.get("protected_paths", []), "defaults.protected_paths")
    repos: dict[str, RepoConfig] = {}
    for slug, r in data.get("repos", {}).items():
        w = f'repos."{slug}"'
        if not SLUG_RE.fullmatch(slug):
            raise ConfigError(f"invalid repo slug: {slug!r}")
        if not isinstance(r, dict):
            raise ConfigError(f"{w}: must be a table")
        host = r.get("host", ALLOWED_HOST)
        if host != ALLOWED_HOST:
            raise ConfigError(f"{slug}: host {host!r} not allowed, only {ALLOWED_HOST}")
        if r.get("mode", "pr") != "pr":
            raise ConfigError(f"{slug}: mode {r.get('mode')!r} not implemented yet")
        base = r.get("base", "main")
        if not (isinstance(base, str) and re.fullmatch(r"[A-Za-z0-9._/][A-Za-z0-9._/-]*", base)):
            raise ConfigError(f"{w}.base: invalid branch name")
        install = r.get("install")
        if install is not None and not (isinstance(install, str) and install.strip()):
            raise ConfigError(f"{w}.install: must be a non-empty string")
        model = r.get("model", defaults.get("model", DEFAULT_MODEL))
        if not (isinstance(model, str) and model):
            raise ConfigError(f"{w}.model: must be a non-empty string")
        deny = tuple(dict.fromkeys(DENY_FLOOR + d_deny + _str_list(r.get("deny_tools", []), f"{w}.deny_tools")))
        allow = _str_list(r["allow_tools"], f"{w}.allow_tools") if "allow_tools" in r else d_allow
        repos[slug] = RepoConfig(
            slug=slug, host=host,
            installation_id=_int(_req(r, "installation_id", w), f"{w}.installation_id", 1, 2**40),
            mode="pr", base=base, install=install,
            checks=_checks(r.get("checks", []), f"{w}.checks"),
            protected_paths=d_protected + _str_list(r.get("protected_paths", []), f"{w}.protected_paths"),
            allow_tools=allow, deny_tools=deny,
            max_turns=_int(r.get("max_turns", defaults.get("max_turns", 30)), f"{w}.max_turns", 1, 100),
            timeout_minutes=_int(r.get("timeout_minutes", defaults.get("timeout_minutes", 30)),
                                 f"{w}.timeout_minutes", 1, 120),
            model=model,
        )
    return repos


def parse_settings(data: dict) -> Settings:
    return Settings(parse_github(data), parse_daemon(data), parse_repos(data))


def _load(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"allowlist not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e


def load_settings(path: Path = DEFAULT_PATH) -> Settings:
    return parse_settings(_load(path))


def load_repos(path: Path = DEFAULT_PATH) -> dict[str, RepoConfig]:
    return parse_repos(_load(path))


def load_daemon(path: Path = DEFAULT_PATH) -> DaemonConfig:
    return parse_daemon(_load(path))


def load_github(path: Path = DEFAULT_PATH) -> GithubConfig:
    return parse_github(_load(path))


def get_repo(repos: dict[str, RepoConfig], slug: str) -> RepoConfig:
    if slug not in repos:
        raise ConfigError(f"{slug} is not in the allowlist")
    return repos[slug]

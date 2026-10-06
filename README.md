# Son of Anton

Runs [Claude Code](https://code.claude.com) on a Raspberry Pi and turns tasks into **draft pull requests**.

> Early personal project: unit-tested and sandbox-tested, but not independently audited.

## How it works

Task → queue → fresh clone → Claude Code implements it in a sandbox → checks → draft PR.

- **Sandbox:** `npm`, your checks and Claude run in [bubblewrap](https://github.com/containers/bubblewrap): system read-only, checkout at `/work` (`.git` read-only), no access to keys or tokens.
- **Claude** runs headless (`claude -p`) with a tool allow/deny list and turn and time limits. It cannot run git, gh, curl or deploy commands.
- **The runner, not Claude,** commits and pushes an `anton/*` branch, using a short-lived GitHub App token scoped to that one repo.
- **Gates:** `gate` checks must pass, `regression` checks must not get worse. Changes to protected paths fail the job.
- **Queue:** SQLite, limited parallelism, daily limit, no duplicate jobs per issue.

## Setup

1. Dedicated unprivileged user; `python3` (3.11+), `git`, `openssl`, `bubblewrap`, Node for your projects.
2. Claude Code for that user and a token from `claude setup-token`.
3. A GitHub App (webhook off; Contents, Issues, Pull requests = write; no Workflows), installed only on allowed repos. Key in `~/.config/son-of-anton/app-key.pem` (mode 600).
4. `examples/repos.toml` → `~/.config/son-of-anton/repos.toml`, fill in your values. Only `github.com` is accepted.

## Commands

```
anton selftest                          # try to escape the sandbox; must pass 100%
anton enqueue --repo o/r --task "..."   # add a job
anton queue [--active]                  # queue and 24h budget
anton serve                             # run the worker pool
anton cancel <job-id>
anton run --repo o/r --task "..." [--dry-run]   # one job now, no queue
```

Tests: `python3 -m unittest discover -s tests`

## Limits

- The network stays open, so a manipulated agent could send the Claude token out. Use a dedicated machine.
- You review every PR; a merge is only as safe as that review.
- No egress filter, no fork mode. Check the current terms before using a subscription for unattended automation.

## License

MIT

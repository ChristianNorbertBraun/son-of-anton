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

## Using it

A **job** is one task for one repository: a repo (`owner/name`, must be on your allowlist), a **task text** (plain instructions, like you would write to a colleague, up to 20,000 characters) and optionally an issue number. Jobs live in a small SQLite queue, not in a folder of files.

```
anton serve                       # 1. the worker: leave it running, it takes jobs from the queue

anton enqueue --repo you/your-site --task \
  "Fix the TypeScript error in dateString(): parameter 'date' has an implicit any type.
   Smallest correct change, leave unrelated code alone."      # 2. add a job from any shell

anton queue --active              # 3. watch it
```

A job goes `queued` → `running` (clone, install, baseline checks, Claude, checks again) and ends as
`pr-open` (a draft PR is waiting for you), `no-changes`, `failed` (the reason is shown) or `cancelled`.
At most a few jobs run in parallel and a daily limit applies (see `[daemon]` in the config).

Good tasks are concrete and small, and say what to leave alone. Vague ones ("improve the site") give vague PRs.

`~/jobs/<id>/` is **not** an inbox. It only holds the log and result of one finished job (`log.txt`, `job.json`, `claude.json`); the checkout is deleted afterwards.

### From a GitHub issue

Give a repo `allowed_authors = ["your-login"]` in the config and the daemon polls it. Put the label `anton` on an issue and it becomes a job (title and body are the task). A job starts only if **both** the issue's author **and** the user who set the label are in `allowed_authors`; everything else is ignored silently, and issue comments are never read. The label then moves through `anton:queued` → `anton:running` → `anton:pr` (with a comment linking the draft PR) or `anton:failed`. To retry, put `anton` on it again. `anton poll --once` checks right now instead of waiting.

### From a chat agent (MCP)

If `~/.config/son-of-anton/bridge-token` exists, `anton serve` also opens an MCP endpoint on `127.0.0.1:8765` (bearer token, loopback only) with the tools `anton_create_task`, `anton_queue_issue`, `anton_status`, `anton_cancel` and `anton_list_repos`. They use the same submit path as everything else, plus a smaller daily quota for the chat agent (`bridge_daily_limit`), because it can be prompt-injected. Put `telegram-token` and `telegram-chat` next to it to get a message when a job starts or ends.

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
anton poll --once                       # check GitHub for labelled issues now
anton serve                             # run the worker pool
anton cancel <job-id>
anton run --repo o/r --task "..." [--dry-run]   # one job now, no queue
```

Tests: `python3 -m unittest discover -s tests`

## Limits

- The network stays open, so a manipulated agent could send the Claude token out, and the sandbox can reach loopback and LAN services. Use a dedicated machine and add an egress rule for the runner user. `anton selftest` reports this as a known gap.
- You review every PR; a merge is only as safe as that review.
- No egress filter, no fork mode. Check the current terms before using a subscription for unattended automation.

## License

MIT

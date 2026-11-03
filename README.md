# Son of Anton

Runs [Claude Code](https://code.claude.com) on a Raspberry Pi and turns tasks into **draft pull requests**.

> Early personal project: unit-tested and sandbox-tested, but not independently audited.

## How it works

Task → queue → fresh clone → Claude Code implements it in a sandbox → checks → draft PR. The same machinery can also **add a commit to an existing PR** and **answer a read-only question** about a repo.

- **Sandbox:** `npm`, your checks and Claude run in [bubblewrap](https://github.com/containers/bubblewrap): system read-only, checkout at `/work` (`.git` read-only), no access to keys or tokens.
- **Claude** runs headless (`claude -p`, the task goes in on stdin) with a tool allow/deny list and turn and time limits. It cannot run git, gh, curl or deploy commands. Installing dependencies (`npm install`) and web research (`WebSearch`, `WebFetch`) are opt-in per repo through `allow_tools`; install scripts never run, whoever types `npm install`.
- **The runner, not Claude,** commits and pushes an `anton/*` branch, using a short-lived GitHub App token scoped to that one repo.
- **Gates:** `gate` checks must pass, `regression` checks must not get worse. A change that contains a stored secret or the shape of a token or private key is refused before anything leaves the machine. A change to protected paths (CI workflows, `.env`, keys, ...) is never pushed: it comes back as a **patch** instead (see below). Symlinks and nested repositories fail the job.
- **Queue:** SQLite, limited parallelism, daily limits, no duplicate jobs per issue or PR.
- **English artifacts:** Claude starts its answer with `TITLE: <imperative English title>` and writes code, comments and the summary in English, whatever language the task is in. The runner derives the commit message, the PR title and the (ASCII) branch name from that title.

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
`pr-open` (a draft PR is waiting for you), `proposed` (a patch is waiting for you), `no-changes`, `failed` (the reason is shown) or `cancelled`.
At most a few jobs run in parallel and a daily limit applies (see `[daemon]` in the config).

Good tasks are concrete and small, and say what to leave alone. Vague ones ("improve the site") give vague PRs. File names are not needed: Claude explores the repo itself.

`anton enqueue --pr 7 --task "..."` adds a commit to the branch of an open PR instead (same repository only, never the default or a protected branch, plain push, no force). `anton ask --repo o/r --question "..."` answers a question without creating anything.

### Protected files: patches

Anton has no permission to change `.github/*`, `.env*`, keys and similar, and never will. If a task needs it (for example a GitHub Actions workflow), the job ends as `proposed`: nothing is pushed, no PR is opened. Instead you get the whole change as a patch: as a file in Telegram, as a comment on the issue (label `anton:patch`), or with `anton patch <job-id> > anton.patch`. Review it, then apply and push it yourself on a checkout of the base branch (or the PR branch):

```
git apply anton.patch && git add -A && git commit -m "..." && git push
```

The text at the top of the patch (title, touched paths, summary) is ignored by `git apply`. Patches over 200,000 characters fail the job.

`~/jobs/<id>/` is **not** an inbox. It only holds the log and result of one finished job (`log.txt`, `job.json`, `claude.json`); the checkout is deleted afterwards.

### From a GitHub issue

Give a repo `allowed_authors = ["your-login"]` in the config and the daemon polls it. Put the label `anton` on an issue and it becomes a job (title and body are the task). A job starts only if **both** the issue's author **and** the user who set the label are in `allowed_authors`; everything else is ignored silently, and issue comments are never read. The label then moves through `anton:queued` → `anton:running` → `anton:pr` (with a comment linking the draft PR) or `anton:failed`. To retry, put `anton` on it again. `anton poll --once` checks right now instead of waiting.

### From a chat agent (MCP)

If `~/.config/son-of-anton/bridge-token` exists (one token file per chat client, e.g. `bridge-token-anton`), `anton serve` also opens an MCP endpoint on `127.0.0.1:8765` (bearer token, loopback only). Tools:

- **Ask** a question about a repo: `anton_ask`, `anton_answer`. Read-only (Claude gets only Read, Glob and Grep, no Bash), the answer comes back in the chat in the language of the question. Nothing is created.
- **Change code**: `anton_create_task` (new draft PR), `anton_update_pr` (add a commit to any open PR of an allowed repo, no force push, never the default or a protected branch, no forks), `anton_queue_issue`.
- **Issues**: `anton_create_issue`, `anton_get_issue`, `anton_update_issue` (prefer `append`), `anton_comment`. Texts are published without @-mentions and closing keywords, and what you read from GitHub is handed to the agent marked as untrusted data.
- `anton_status`, `anton_cancel`, `anton_list_repos`.
- **Self-update**: `anton_update_check` (read-only) and `anton_update_apply` (only when you ask for it).

They use the same submit path as everything else, plus smaller per-client quotas (`bridge_daily_limit` jobs, `bridge_write_limit` GitHub writes, `bridge_ask_limit` questions), because a chat agent can be prompt-injected. Put `telegram-token` and `telegram-chat` next to it to get a message when a job starts or ends.

### Flows

A flow takes an issue from label to merge in several steps, with a gate wherever a person has to decide. It needs `tracker = "github"`, checks and `allowed_authors` in the repo's config, and a running `anton conduct` (see `examples/anton-conductor.service`).

| Label | Steps |
|---|---|
| `flow:quick` | implement, draft PR, review, fix if needed, ready, you review and merge |
| `flow:standard` | plan, draft PR, you mark it ready, implement, review, fix if needed, remove the plan, ready, you review and merge |

- A flow starts only if the issue's author is allowed (or the App, for issues from chat) and an allowed person set the label. The label goes away at the start; set it again for a new flow.
- The gates are the buttons on the pull request. Only clicks and reviews by `allowed_authors` count, never by bots, the App or chat agents, and a review counts only for the commit it was written on.
  - **Plan gate**: the PR is a draft with only `.rook/PLAN.md`. "Request changes" revises the plan on the same PR, up to 3 times. **"Ready for review"** is the go: Anton implements, reviews and fixes its work, removes the plan and updates the PR text. Each new version of the plan turns the PR back into a draft; a Ready clicked before that version was up gets a comment and has to be clicked again. "Approve" starts nothing here, it gets a comment that says to click Ready.
  - **Merge gate**: the PR is ready. "Request changes" starts another round (`rework`): your review and line comments go to Claude as data, the change passes the checks, Anton reviews it again, and the PR comes back to the gate; after 3 rounds the flow is `blocked`. "Approve" sets `anton:approved` and nothing else: a later "Request changes" still starts a round. Merging is yours, and ends the flow as `done`.
  - A review on an older commit changes nothing and gets one comment saying so.
  - Known limit: review text is read when the gate is looked at, so anyone with write access to the repo could edit an owner's review before Anton reads it. Flows are meant for repos where only the owner has write access.
- Each code change passes the repo's checks before it is pushed, with up to 3 attempts to fix a failure. A flow never force-pushes, and the branch `anton/flow-<id>` is off limits for every other job.
- Steps load the repo's `CLAUDE.md`. Everything else a checkout could use to configure Claude is hidden, and a step cannot change a `CLAUDE.md`.
- `blocked` means the owner has to look. The reason is on the issue. `anton flow show <id>` has the details, `anton flow retry <id>` runs the step again, and `anton flow cancel <id>` ends the flow. A retry takes over a push to the branch only if that push is what blocked the flow; if the plan could have changed with it, the PR is a draft again and needs Ready again.
- `anton dashboard` is a read-only page of all flows and their steps, on 127.0.0.1 (see `examples/anton-dashboard.service`). Reach it through a tunnel, `ssh -L 8766:127.0.0.1:8766 <host>`, at `http://127.0.0.1:8766/<token>`. The token is in `dashboard-token` in the config directory, made on first start, because loopback does not keep out other users of the machine.
- Trackers other than GitHub come as adapters: built in, or as a separate program that speaks JSON lines over stdin and stdout (`anton/adapters/stdio.py` has the protocol, `tests/conformance/` the suite every adapter must pass). An adapter runs outside the sandbox with its tracker's token, so it is trusted code; nothing installs one by itself. The config cannot name one yet.
- Each line of an optional `private-strings` file in the config directory is refused in anything a flow writes to GitHub. Normal jobs do not read it. `private_check = false` turns it off for a repo.

## Setup

1. Dedicated unprivileged user; `python3` (3.11+), `git`, `openssl`, `bubblewrap`, Node for your projects.
2. Claude Code for that user and a token from `claude setup-token`.
3. A GitHub App (webhook off; Contents, Issues, Pull requests = write; no Workflows), installed only on allowed repos. Key in `~/.config/son-of-anton/app-key.pem` (mode 600). Each token is scoped to one repo and the permissions of its call: changes to a pull request get pull requests write only, except turning one back into a draft, which GitHub allows only with contents write as well.
4. `examples/repos.toml` → `~/.config/son-of-anton/repos.toml`, fill in your values. Only `github.com` is accepted. Per repo: `checks` (`gate` must pass, `regression` must not get worse; `metric` or `metric_lines` count the problems), `protected_paths`, `allow_tools` / `deny_tools` (a mandatory deny floor can only be extended), `allowed_authors` and `trigger_label` for the issue trigger, and the `[daemon]` limits.

## Updating

Son of Anton updates itself from its own releases with [agent-tool-selfupdate](https://github.com/ChristianNorbertBraun/agent-tool-selfupdate), so you can improve it with itself. You publish a release on GitHub (UI or CLI) with a tag `vX.Y.Z` whose `anton/version.py` says the same; then `anton update` (or "update yourself" in the chat, via `anton_update_apply`) installs it together with the newest release of agent-tool-selfupdate that `selfupdate.toml` allows (`~=1.0`: up to the next major). A new release of the library alone is an update too.

1. Only releases published **by the login in `[update] publisher`**, no drafts, no pre-releases, newer than the running version (no downgrades). Nothing Anton or the chat agents do can publish one; to be sure, restrict tag creation of `v*` to yourself in a GitHub ruleset.
2. Both archives are unpacked next to the old version (`~/releases/<version>+u<library>`, plain files only). The library's and Anton's tests run in the sandbox, the new updater must still find the releases (probe), and the new code must read your real config (`anton config-check`).
3. Running jobs finish first (no new job starts meanwhile, queued ones wait). Then `~/current` points to the new version in one step, the service restarts and must report the new version and stay up for 15 seconds.
4. If that fails the symlink goes back and the old version is started again. A Telegram message says what happened, and `anton_update_check` shows the last result. Three versions are kept.

`~/bin/anton` and the service run `~/current`. Merging a PR never changes the running installation; only a release you publish and an update you ask for do. Add `[update]` to the config to turn it on (see `examples/repos.toml`). An installation from before 0.3.0 gets the library once by hand: see "First installation" in the library's README.

## Commands

```
anton selftest                          # try to escape the sandbox; must pass 100%
anton enqueue --repo o/r --task "..."   # add a job (--pr N: add a commit to that open PR)
anton ask --repo o/r --question "..."   # read-only question, prints the answer
anton queue [--active]                  # queue and 24h budget
anton poll --once                       # check GitHub for labelled issues now
anton serve                             # run the worker pool
anton cancel <job-id>
anton patch <job-id> > anton.patch       # the patch of a `proposed` job
anton update [--check]                  # install the newest release (see Updating)
anton version
anton run --repo o/r --task "..." [--dry-run]   # one job now, no queue
```

Tests: `python3 -m unittest discover -s tests`. The tests of the update adapter need agent-tool-selfupdate next to the package (it is in `.gitignore`); without it they are skipped. Fetch it with:

```
d=$(mktemp -d) && curl -fsSL https://github.com/ChristianNorbertBraun/agent-tool-selfupdate/archive/refs/tags/v1.0.0.tar.gz | tar xz -C "$d" --strip-components=1
PYTHONPATH="$d" python3 -m agent_tool_selfupdate fetch --publisher ChristianNorbertBraun --into .
```

## Limits

- The sandbox shares the network of the runner user, so a manipulated agent could still send the Claude token to the internet. Keep it away from your own network with an egress rule for that user (`examples/anton-egress.nft`: no loopback, LAN or tailnet, DNS allowed); without it `anton selftest` reports a known gap.
- You review every PR; a merge is only as safe as that review. Look at dependency changes in particular: a new package is run by your CI at merge time.
- No filter for internet destinations, no fork mode. Check the current terms before using a subscription for unattended automation.

## License

MIT

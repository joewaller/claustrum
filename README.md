# Claustrum

Unconscious coordination layer for multiple Claude Code sessions.

Named after the [brain structure](https://en.wikipedia.org/wiki/Claustrum) that coordinates activity across cortical regions. Like its biological counterpart, Claustrum provides shared awareness between independent Claude Code sessions without requiring conscious coordination.

## The Problem

You're running three Claude Code sessions in tmux — one refactoring auth, one writing tests, one updating docs. Session B renames `UserService` to `AccountService`. Session A, mid-refactor, has no idea. It edits the old class name. You get a merge nightmare.

## The Solution

Claustrum hooks into Claude Code at the runtime level. No MCP server. No tool calls Claude has to remember to make. It uses Claude Code's [hooks system](https://docs.anthropic.com/en/docs/claude-code/hooks) to automatically:

- **Inject awareness** — Every turn, each session sees what other sessions are doing (via `UserPromptSubmit` context injection)
- **Enforce file locks** — Before any edit, check if another session has claimed that file (via `PreToolUse` blocking)
- **Broadcast changes** — After every file edit, notify other sessions what changed (via `PostToolUse` side effects)
- **Manage lifecycle** — Sessions register on start, clean up on exit

Claude doesn't *decide* to coordinate. It just *perceives* the coordination state as part of its environment, like a system prompt.

## How It Works

```
User hits Enter
       │
       ▼
UserPromptSubmit hook fires
       │
       ├──→ Heartbeat (I'm alive)
       ├──→ Read messages (what should I know?)
       ├──→ Check nearby sessions (who else is working?)
       │
       ▼
stdout → injected as context Claude sees
       │
       ▼
Claude processes turn (now aware of all other sessions)
       │
       ▼
PreToolUse hook on Edit/Write
       │
       ├──→ Claim file (atomic lock)
       ├──→ exit 2 if claimed by another session (edit blocked)
       │
       ▼
PostToolUse hook on Edit/Write
       │
       └──→ Broadcast: "I just edited auth/service.ts"
```

The backing store is a single SQLite file (`~/.claustrum/state.db`) in WAL mode. No daemon. No server. Each session reads and writes directly. SQLite handles the concurrency.

## Quick Start

```bash
# Clone
git clone https://github.com/joewaller/claustrum.git
cd claustrum

# Install hooks into Claude Code (one command)
./claustrum install

# Restart your Claude Code sessions — that's it
```

No dependencies. No pip install. Just Python 3 and SQLite (both ship with macOS and most Linux).

## What Claude Sees

When another session is active, this gets injected into context before every turn:

```
[Claustrum — 3 other sessions]

  ⠂ google-ads-mcp (5s ago):
    Task: Adding keyword planner endpoint

  ⠂ chuck-louis-bug (45s ago):
    Task: Fixing Slack bot identity mismatch

  ⠐ wp-field-check (1h ago):
    Task: Checking custom field mappings

Messages:
  • [file-change] c1094c0a: Edited src/auth/service.ts
```

- `⠂` = active (recent heartbeat)
- `⠐` = idle (session open but no recent interaction)

Session names come from the tmux session name (set via `tmux rename-session`). If no other sessions exist, Claustrum is silent. Zero noise.

## CLI Reference

Hooks are automatic, but you can also interact with Claustrum directly:

```bash
# See what's going on
claustrum status

# Manually register your session's task (Claude can do this via Bash)
claustrum checkin --uid <session-id> --task "refactoring auth"

# Update what you're working on
claustrum update --uid <session-id> --files "src/auth/*.ts"

# Send a message to another session
claustrum send --uid <your-id> --to <their-id> --body "don't touch middleware.ts"

# Broadcast to all sessions
claustrum send --uid <your-id> --to all --body "renamed UserService to AccountService"

# Claim a file explicitly
claustrum claim --uid <your-id> --file src/auth/service.ts

# Release a claim
claustrum release --uid <your-id> --file src/auth/service.ts

# Mark session as done
claustrum done --uid <your-id> --summary "auth refactor complete"

# Clean up stale sessions
claustrum gc

# Nuclear reset
claustrum reset
```

## Install / Uninstall

```bash
# Install hooks into ~/.claude/settings.json
./claustrum install

# Remove all claustrum hooks
./claustrum uninstall
```

The installer is idempotent — run it again to update hook paths after moving the script.

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Session A   │     │  Session B   │     │  Session C   │
│  refactoring │     │  testing     │     │  docs        │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │                    │                    │
       │  hooks             │  hooks             │  hooks
       │                    │                    │
       ▼                    ▼                    ▼
  ┌─────────────────────────────────────────────────┐
  │           ~/.claustrum/state.db                  │
  │              SQLite (WAL mode)                   │
  │                                                  │
  │  sessions:  who's alive, what they're doing      │
  │  messages:  directed and broadcast notifications │
  │  claims:    file-level exclusive locks           │
  └─────────────────────────────────────────────────┘
```

No daemon. No server. No network. Just a file and hooks.

## Cross-machine coordination (optional)

The single-file CLI above is enough for one machine. If you have agents
running on multiple machines (laptops, VMs) and want them to see each
other's work, run the optional cloud companion in [`cloud/`](cloud/).

It's a small Postgres-backed HTTP service that augments the local SQLite —
local stays as the same-machine fast path, cloud handles cross-machine.
Augment, not replace; if the cloud server is down, local coordination keeps
working.

Activate by setting `CLAUSTRUM_CLOUD_URL=https://your-server` in the
environment. Unset = exact prior single-machine behaviour.

See [`cloud/README.md`](cloud/README.md) for the architecture and
[`cloud/server/README.md`](cloud/server/README.md) for how to run your own.

## Design Principles

1. **Unconscious over conscious** — Coordination happens via hooks, not tool calls. Claude doesn't decide to check; it simply perceives.
2. **Silent when alone** — If you're the only session, Claustrum produces zero output. No noise.
3. **Fail-open** — If the DB is locked or the script crashes, hooks fail silently (exit 0). A broken coordination layer should never block your work.
4. **Zero dependencies** — Python 3 stdlib only. Ships on every Mac and most Linux boxes.
5. **Persistent sessions** — Sessions live until the LLM explicitly exits (via `SessionEnd` hook). No automatic expiry from inactivity — idle sessions remain visible so you always see the full picture. Use `claustrum gc` to manually clean up stale data.

## Why "Claustrum"?

The [claustrum](https://en.wikipedia.org/wiki/Claustrum) is a thin sheet of neurons in the brain, positioned between the cortex and deeper structures. Neuroscientists believe it acts as a conductor — coordinating activity across independent brain regions without controlling them. Each region does its own work; the claustrum ensures they're aware of each other.

That's exactly what this does for Claude Code sessions.

## License

MIT

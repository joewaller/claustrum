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
[Claustrum — 1 other active session]

  a1b2c3d4e5f6 (12s ago):
    Task: refactoring auth module
    Working on: src/auth/service.ts, src/auth/middleware.ts
    Dir: /Users/you/project

Messages:
  • [file-change] a1b2c3d4e5f6: Edited src/auth/service.ts
```

If no other sessions are active, Claustrum is silent. Zero noise.

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

## Design Principles

1. **Unconscious over conscious** — Coordination happens via hooks, not tool calls. Claude doesn't decide to check; it simply perceives.
2. **Silent when alone** — If you're the only session, Claustrum produces zero output. No noise.
3. **Fail-open** — If the DB is locked or the script crashes, hooks fail silently (exit 0). A broken coordination layer should never block your work.
4. **Zero dependencies** — Python 3 stdlib only. Ships on every Mac and most Linux boxes.
5. **Ephemeral by default** — Sessions expire after 5 minutes of silence. Old messages are cleaned automatically. This is a coordination layer, not a knowledge base.

## Why "Claustrum"?

The [claustrum](https://en.wikipedia.org/wiki/Claustrum) is a thin sheet of neurons in the brain, positioned between the cortex and deeper structures. Neuroscientists believe it acts as a conductor — coordinating activity across independent brain regions without controlling them. Each region does its own work; the claustrum ensures they're aware of each other.

That's exactly what this does for Claude Code sessions.

## License

MIT

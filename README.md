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

When other sessions are alive, a **lightweight** roster is injected before every
turn. It's deliberately one line per session — with many sessions open, dumping
each one's full task + file list was thousands of tokens per turn:

```
[Claustrum — 12 other sessions]

  ⚠️  Collisions on files you've claimed:
      • chuck-louis-bug: service.ts — Fixing Slack bot identity mismatch

  · 2 other live session(s) in this directory: api-tests, api-docs  — `claustrum show <name>` to inspect

  ⠂ google-ads-mcp (5s ago)
  ⠂ chuck-louis-bug (45s ago)
  ⠐ wp-field-check (1h ago)
  … and 7 more — `claustrum status` for the full list

Messages:
  • [breaking-change] c1094c0a: renamed UserService → AccountService

  · 14 file edit(s) by 4 other session(s): service.ts, routes.ts, README.md +3 more
```

What's loud vs quiet:

- **Collisions** — another live session has claimed a file *you've* also claimed. The only place per-session detail is spent, because it's the one thing needing attention.
- **Potential clash** — other live sessions in your exact working dir (e.g. a shared worktree). Suppressed at the monorepo root, where everyone co-tenants. `claustrum show <name>` drills into any session on demand.
- **Roster** — one line each (`⠂` active / `⠐` idle), most-recent first, capped; the rest collapse to a count.
- **Messages** — directed messages (info / breaking-change) verbatim; the per-edit `file-change` broadcasts collapse to a single deduped line.

Session names come from the tmux session name (set via `tmux rename-session`). If no other sessions are alive, Claustrum is silent. Zero noise.

## CLI Reference

Hooks are automatic, but you can also interact with Claustrum directly:

```bash
# See what's going on (full table — the heartbeat tray is the trimmed version)
claustrum status

# Full detail for ONE session (by name, name-substring, or uid): task,
# working_on, claimed files, cwd, and live-state. This is what the tray's
# `claustrum show <name>` hint points at.
claustrum show api-tests

# Manually register your session's task (Claude can do this via Bash)
claustrum checkin --uid <session-id> --task "refactoring auth"

# Update what you're working on (also feeds the cloud detail layer)
claustrum update --uid <session-id> --files "src/auth/*.ts"

# List the canonical taxonomy (for a classifying sub-agent — output never
# touches the main session's context). Topics are grouped by domain.
claustrum topics

# List the canonical DOMAIN taxonomy (each topic belongs to exactly one domain)
claustrum domains

# Tag this session with a topic + domain (cloud) and see who's worked on it before.
# --domain is recorded on the session (else derived from the topic's domain).
claustrum classify-self <session-id> "gateway-deploy" --domain gateway

# Propose a new topic — added to the canonical taxonomy immediately unless it's a
# near-duplicate of an existing one (similarity guard maps it to that instead).
# --domain places it in a domain (default 'general'; must already exist).
claustrum propose-topic <session-id> "gateway-deploy" "Deploying MCP gateway changes" --domain gateway

# Propose a new domain — same immediate-with-similarity-guard behaviour.
claustrum propose-domain <session-id> "growth" "Growth & acquisition work"

# Send a message to another session
claustrum send --uid <your-id> --to <their-id> --body "don't touch middleware.ts"

# Broadcast to all sessions
claustrum send --uid <your-id> --to all --body "renamed UserService to AccountService"

# Claim a file explicitly
claustrum claim --uid <your-id> --file src/auth/service.ts

# Release a claim
claustrum release --uid <your-id> --file src/auth/service.ts

# Mark session as done (--resolution feeds the cloud solved-archive)
claustrum done --uid <your-id> --resolution "auth refactor: merged PR #123, deployed prod"

# Browse the solved-problem archive (completed work, any age, paginated)
claustrum archive                       # most recent solves
claustrum archive --repo gateway        # filter by repo / --topic / --person
claustrum archive --limit 50 --offset 50  # page through history

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

### Cloud client environment variables

| Variable | Purpose |
|----------|---------|
| `CLAUSTRUM_CLOUD_URL` | Base URL of the cloud server (e.g. `https://claustrum.example.com`). Unset = no cloud calls. |
| `CLAUSTRUM_AUTH_HEADER` | Header name carrying the bearer credential. Default `Authorization`. |
| `CLAUSTRUM_AUTH_VALUE` | Static header value (e.g. `Bearer <token>`). Use for long-lived credentials. |
| `CLAUSTRUM_AUTH_COMMAND` | Shell command whose stdout becomes the header value. Runs per request — wrap `gcloud auth print-identity-token` (or any token producer) with caching for short-lived tokens. Takes precedence over `CLAUSTRUM_AUTH_VALUE`. |
| `CLAUSTRUM_CLASSIFY_CMD` | The LLM judge behind the **classification skill**: a command fed `{"mode","signal","candidates"}` JSON on stdin that prints `{"choice","is_new","description"}` — pick an existing domain/topic or propose a new one. Called (detached) by the turn-3 hook + the `heartbeat` tick, twice per run (domain then topic). LLM-agnostic (any cheap CLI); run with `CLAUSTRUM_PRIVATE=1`. Unset = no skill (in-session directive + heuristic floor only). |

All cloud calls have a 1.5s timeout and swallow failures. The local SQLite
store is the source of truth — cloud is purely augmentative.

### Topic / detail model + privacy gate

The cloud layer publishes work in two layers so cross-machine, cross-person
duplication is caught without leaking content:

| Layer | Visibility | Example |
|-------|------------|---------|
| **Topic** | Always on the board (all peers) | `gateway-deploy` |
| **Detail** | Cloud-resident, **hidden by default**, pulled on demand | "deploying MCP gateway #57; touching `whitelist-manager`" |
| **Private** | Suppressed — never leaves the machine | a redundancy / pay-review session |

The per-turn `UserPromptSubmit` hook publishes only the **coarse label** (tmux
slug), never the raw prompt. The goal is that **every** session carries a
**domain + topic**, set (in order of authority) by: a human/deliberate
`classify-self` (confidence 80) or the harness-fired **classification skill**
(`CLASSIFY_SKILL_CONF`); a cheap **LLM-free heuristic** (keyword overlap of the
on-machine signal against the cloud taxonomy, confidence ≤60); and mirror-down of
an already-set cloud topic+domain.

**Reliable, token-cheap, agent-agnostic classification.** The **classification
skill** is the primary path: a headless, match-first classifier the *harness*
fires — never a directive the working agent can ignore. Two fallback layers keep
coverage up when no classify CLI is configured.

1. **Classification skill (primary, harness-fired).** A session names itself
   around **turn 3**. The skill is a detached process — spawned by the
   `UserPromptSubmit` hook at `CLASSIFY_TRIGGER_TURN` for Claude, and by the
   machine-wide `claustrum heartbeat` tick for any other `wa`-harnessed agent
   (codex / antigravity / gemini / qwen — the tick is their only agent-agnostic
   trigger). It reads the session **transcript** (leading with the curated session
   name, the strongest prior) and runs **match-first** at both levels: it is shown
   the existing `claustrum domains`, picks the best fit or — only on a genuine miss
   — mints a new one via `propose-domain`; then, scoped to that domain, it picks or
   mints a **topic**. It writes `classify-self` at `CLASSIFY_SKILL_CONF` and only
   the resulting `(domain, topic)` is retained — the token-heavy pick/propose
   reasoning stays in the sub-process (the main session pays ~nothing). The LLM is
   a pure JSON judge (`CLAUSTRUM_CLASSIFY_CMD`, any cheap CLI; run with
   `CLAUSTRUM_PRIVATE=1` as a recursion guard) so it works across every agent.
   Bounded: a judge failure spends an attempt and, after `CLASSIFY_SKILL_ATTEMPTS`,
   the session is marked `classify_failed` (surfaced by `claustrum show` + logged
   to `~/.claustrum/classify.log` — loud, but never in the working session's face).
   Transcripts are read across agents: Claude (`~/.claude/projects/*/<uid>.jsonl`),
   Codex (`~/.codex/sessions/**/rollout-*.jsonl`, by cwd), Antigravity
   (`conversations/<uid>.db`, read-only SQLite, string-scraped).

   **Emergent domains stay convergent.** Domains are minted like topics (no
   promotion gate), yet growth is logarithmic because near-duplicates are collapsed
   *twice*: match-first, the judge prefers an existing name; and the cloud
   `propose_*` similarity guard maps a surface-variant onto the existing name. A new
   name is minted only on a genuine miss at both layers.
2. **In-session directive (fallback — no classify CLI).** When
   `CLAUSTRUM_CLASSIFY_CMD` is unset, the hook instead asks the working agent to
   spawn a sub-agent and classify from full context, re-asserted each turn from
   turn 2 until it lands (capped at `CLASSIFY_MAX_NUDGES`).
3. **Keyword-heuristic floor (always-on, zero-cost, provisional).** The tick
   commits a best-guess topic for any untagged session — orphan/adopted panes,
   never-renamed `session01`s — from the on-machine signal, at low confidence, so
   the board shows *something* before the skill lands. It commits a **specific**
   topic only on a grounded, unique keyword lead; a **tie** or a no-overlap session
   is left **untagged** (never bucketed into a generic `app` catch-all, which would
   pollute the board and fire false topic-collision alerts). The skill upgrades
   these to a confident, context-grounded classification.

Once confidently classified, a low-frequency **drift re-verify** re-surfaces the
current topic+domain on a cadence so the agent self-corrects if the work has drifted
or was misclassified.

> The detail layer (files touched,
PR, last push, a value-scrubbed `working_on`) is fed by `update` + the
`PostToolUse` hook. `GET /v1/list` then ranks peers by overlap strength —
exact-file (t1), same PR / shared directory (t2), same topic (t3), same repo
(t4) — and the loud t1/t2 collisions surface in the heartbeat tray.

**Local topic mirror.** The topic + confidence are mirrored into the local
`state.db` `sessions` row (columns `topic`, `topic_confidence`) so a session's
subject is **joinable offline by uid** — written authoritatively at
`classify-self`/auto-classify time, and refreshed from the cloud checkin
response when a session is already tagged. (The cloud carried these from the
start; the local mirror is what lets downstream tools — e.g. conversation
nugget mining — join `.jsonl` transcripts to topics by the session uuid without
a cloud round-trip.) Forward-path only: archived/historical sessions stay
untagged locally.

**Canonical taxonomy API.** Claustrum's `topics` and `domains` tables are the
**canonical** vocabularies; other consumers (e.g. the memory-enhanced KG)
reconcile to them rather than inventing a parallel namespace. Every topic belongs
to exactly one domain (`topics.domain`, NOT NULL); domains are a first-class
taxonomy that mirrors topics (bootstrap seeds, registrar register, or propose —
which now adds the name canonically **immediately**, with a similarity guard that
maps near-duplicates onto the existing name):

- `GET /v1/topics` — the full taxonomy (`name`, `description`, `parent`, `source`,
  `domain`). Read-only, any authenticated caller. Consumers cache it and collapse
  variants via `parent`.
- `POST /v1/topics/register` — trusted write-through: add a canonical topic if
  absent (idempotent). Optional `domain` (defaults to `general`, validated). Gated
  by a shared secret (`X-Claustrum-Registrar-Secret` == `CLAUSTRUM_REGISTRAR_SECRET`);
  **disabled with 403 when the env var is unset**, so the emergent `propose`/promote
  path stays the only writer until an operator opts a trusted registrar in.
- `GET /v1/domains` — the full domain taxonomy (`name`, `description`, `parent`,
  `source`). Read-only, any authenticated caller.
- `POST /v1/domains/register` — trusted write-through for domains; same registrar
  secret gate as topics.
- `POST /v1/propose_topic` / `POST /v1/propose_domain` — add a topic/domain to the
  canonical taxonomy immediately, unless the similarity guard finds a near-duplicate
  (returned as `mapped_to` so the caller classifies into the existing name). This is
  the secret-free path the classify sub-agent uses to mint genuinely-new names.

### Solved-problem archive

Live overlap only stops *simultaneous* duplication. The solved-archive also
stops re-solving *already-completed* work: when a session is marked done with a
`resolution`, future sessions matching the same files / PR / topic / repo are
warned **🗂 may already be solved** (in the tray and in `classify-self`), with
who solved it, when, and how.

- **Writing:** `claustrum done --uid <id> --resolution "<value-scrubbed how>"`.
  On `SessionEnd`, a session is also auto-archived — but only on a **deliberate
  quit** (`reason: prompt_input_exit`) of work that has a **PR**. A `/clear`,
  `/resume` switch, logout, or incidental end does *not* publish, so the archive
  isn't flooded with look-alike rows for work that was merely interrupted (hard
  crash / `kill -9` / reboot never fire `SessionEnd` at all — the heartbeat
  reaper marks those `paused`, never archived). The derived resolution is
  prefixed with the session topic (`"<topic>: closed with PR #N"`) so entries
  self-distinguish instead of repeating an identical line for every session that
  shared a branch. PR-less / exploratory sessions still don't flood it, and only
  entries with a real resolution are surfaced.
- **Reading:** `classify-self` and `GET /v1/list` return a `solved` block,
  matched by the same overlap tiers as live peers and recency-bounded
  (`solved_days`, default 180). Private and resolution-less rows are excluded.
- **Storage:** Postgres only — done rows stay queryable; no BigQuery dependency.

**Two-question privacy rule** (delegated to the LLM via the preprompt — no
classifier, no rules engine in the CLI):

1. **Is the topic itself sensitive** — would naming it on a shared board reveal
   something (redundancy, P&C, pay review, an unannounced security incident,
   someone's HR matter)? → set `CLAUSTRUM_PRIVATE=1`, suppress everything.
2. **Otherwise publish** the topic + a **value-scrubbed** detail line. Detail
   *describes* the work ("rotated the finderops key"); it never *contains* a
   raw secret value, token, password, or PII. A session that merely handles a
   secret stays public so collisions are still caught — you just never ship
   the literal value.

Switches:

| Switch | Effect |
|--------|--------|
| `CLAUSTRUM_PRIVATE=1` | All cloud writes/reads short-circuit. Loud banner printed once to stderr. |
| `CLAUSTRUM_PUBLIC=1` | Overrides `CLAUSTRUM_PRIVATE` (escape hatch when the LLM is too cautious). |
| `claustrum checkin --private` | Same effect as `CLAUSTRUM_PRIVATE`, scoped to one invocation. |

### Resetting cloud state

```bash
# Clear everything (local DB only)
claustrum reset

# Clear local DB AND POST /v1/reset to delete this user's cloud rows
claustrum reset --cloud
```

`--cloud` deletes every row you own on the server — your sessions and topic
proposals, plus the claims and messages from those sessions. It does not touch
the shared topic taxonomy or other people's messages. The call is best-effort:
your local DB is always cleared, and a cloud failure is reported but doesn't
block.

See [`cloud/README.md`](cloud/README.md) for the architecture and
[`cloud/server/README.md`](cloud/server/README.md) for how to run your own.

## Design Principles

1. **Unconscious over conscious** — Coordination happens via hooks, not tool calls. Claude doesn't decide to check; it simply perceives.
2. **Silent when alone** — If you're the only session, Claustrum produces zero output. No noise.
3. **Fail-open** — If the DB is locked or the script crashes, hooks fail silently (exit 0). A broken coordination layer should never block your work.
4. **Zero dependencies** — Python 3 stdlib only. Ships on every Mac and most Linux boxes.
5. **Liveness from ground truth, not a timer** — A session is "alive" if its process/tmux pane actually exists, not because it heartbeated recently. On each turn Claustrum reaps same-host sessions whose tmux pane is gone or whose boot epoch predates the current boot (reboot / crash / `kill -9` — where no `SessionEnd` fires), and releases their stale file claims so they stop blocking live edits. A genuinely idle session (you're asleep) stays visible because its pane still exists; a dead one drops immediately. Cross-host sessions and any that can't be verified fall back to the `last_seen` timer. Use `claustrum gc` to force a sweep.

## Why "Claustrum"?

The [claustrum](https://en.wikipedia.org/wiki/Claustrum) is a thin sheet of neurons in the brain, positioned between the cortex and deeper structures. Neuroscientists believe it acts as a conductor — coordinating activity across independent brain regions without controlling them. Each region does its own work; the claustrum ensures they're aware of each other.

That's exactly what this does for Claude Code sessions.

## License

MIT

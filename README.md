# brain

A long-running personal agent: an LLM loop with tools, memory and channels
(WhatsApp, comms, CLI). It runs as a small Python service on an always-on
Linux box such as a Raspberry Pi. Every message from every channel becomes a
turn in one serialized loop, so the agent is one mind with many mouths. It
remembers each thread, sees what is going on in the others, and can relay
between them.

## Part of a three-repo stack

| Repo | What it is | Runs on |
|---|---|---|
| **brain** (this repo) | A long-running personal agent: LLM loop, tools, memory, and channels (WhatsApp, comms, CLI) | Linux / Raspberry Pi |
| [comms](https://github.com/joaohts/comms) | Encrypted messaging between agent sessions and machines: node, CLI, optional broker | macOS, Linux |
| [agent-monitor](https://github.com/joaohts/agent-monitor) | macOS app: live view of every Claude Code / Codex / Cursor session, plus a comms dashboard | macOS |

Each works alone. Together: install **comms** on every machine, **agent-monitor** on your Mac (it bundles a pinned comms release), and **brain** on an always-on box with its comms channel enabled. Pair the machines once and every session — and the brain — can reach every other.

## Quickstart

Needs Python 3.11+, an OpenAI API key, and systemd user services. Node 20+ is
needed only for WhatsApp.

```sh
git clone https://github.com/joaohts/brain.git ~/brain && cd ~/brain
./install.sh                 # venv, deps, config files, systemd user units
$EDITOR config.toml          # names, model, integrations (see below)
$EDITOR .env                 # OPENAI_API_KEY=...
$EDITOR data/identity.md     # persona / system prompt
./install.sh --enable        # or: systemctl --user enable --now brain
```

Talk to it:

```sh
.venv/bin/python -m brain.cli "hello, who are you?"
curl -s localhost:3401/turn -d '{"channel":"cli","sender":"me","tier":"owner","text":"hi","wait":true}'
```

`install.sh` is idempotent. It only touches this directory and
`~/.config/systemd/user`, never overwrites your config, and starts nothing
without `--enable`. To keep the services running without a login session, run
`sudo loginctl enable-linger $USER` once.

## How it works

```
 WhatsApp ─┐                         ┌─ tools (memory, timers, relay,
 comms ────┼─▶ envelope ─▶ run_turn ─┤    calendar, Claude sessions …)
 CLI/HTTP ─┘  {channel, sender,      └─ reply ─▶ back out the same channel
               tier, text}
```

- **Envelope**: every inbound message is `{channel, sender, tier, text}`.
  `channel` is the thread key (`wpp:alice`, `comms-v1:<machine>:<agent>`,
  `cli`) and `tier` is `owner`, `family` or `unknown` (plus `agent`, which the
  runtime assigns to results from workers the brain spawned).
- **Inbox → loop** (`brain/inbox.py`, `brain/loop.py`): every envelope is
  written to the inbox first, and turns answer inbox rows one at a time (see
  [Message handling](#message-handling)). Each turn builds context from the identity file, memory files, the channel's
  rolling summary and recent window, plus a one-line "blackboard" of the
  other active channels. It then calls the model and runs tool calls, up to
  `max_tool_steps`.
- **Tiers**: `owner`, `family` (known contacts), `agent` (workers and other
  agents; never owner) and `unknown`. Tools a tier can't use are left out of
  the schema entirely, and execution checks again (`brain/tools.py`). The
  runtime stamps identity; the model never writes it. Only owner turns see
  the other conversations, the channel list and the ability to message agents.
  Durable memory is shown to owner and agent turns only. An agent turn can use
  the comms read tools and deliver to its origin channel, nothing else.
- **Prompt** (`brain/prompts.py`): one composer, one home per rule. The job
  and priorities come first, then the persona (`identity_file`: tone and
  language only), memory, the conversation summary, and what this turn is.
- **State** (`data/brain.db`, SQLite) holds every step traced with tokens and
  cost, thread histories, timers and summaries. `brain.compact` folds old turns
  into per-channel summaries and appends durable facts to
  `memory_dir/facts.md`. A thread that outgrows `auto_compact_turns` is
  compacted automatically; for a nightly pass add
  `15 4 * * * cd ~/brain && .venv/bin/python -m brain.compact` to crontab.
- **Budget**: once spend today exceeds `daily_budget_usd`, new turns are
  refused. Spend is computed from the configured prices.

## Message handling

No message waits blind or gets lost, and every message is answered at its
sender's own tier.

- **Inbox first.** Every inbound message is stored in the `inbox` table of
  `data/brain.db` before anything else happens: WhatsApp, comms, CLI, HTTP
  `/turn`, timers and worker results alike. Each row records sender, tier,
  channel, thread, `provider_id` and state (`unread` → `read_by_turn` →
  `done`). `provider_id` is unique, so a redelivered WhatsApp or comms message
  is dropped instead of answered twice. The turn lock only decides who
  answers next; it never decides whether something is answered.
- **One turn at a time, under a lease.** A turn holds a lease that is renewed
  every model step and by a heartbeat. If a process dies mid-turn, the lease
  expires and the next claim takes it over, and the dead turn's messages go
  back to `unread`. A running turn is never cancelled.
- **Follow-ups merge at step boundaries.** Before each model step, the running
  turn picks up unread messages from the same sender in the same channel and
  thread, plus worker results for that thread. They are appended after the
  tool results, each stamped with its own sender, tier and channel; at most 20
  messages or about 4,000 characters per read, and the rest wait for the next
  step. If the model had already written its final answer, that draft is
  discarded and it takes one more step. A merged message completes with an
  empty reply, because the turn's own reply covers it.
- **Different senders are never merged.** Someone else writing to the same
  chat, or a peer agent, gets a turn of their own after the current one, at
  their own tier.
- **Handoff.** When a turn ends, the oldest unread message starts the next turn
  straight away, without releasing the lease. With nothing unread, the lease is
  released and the inbox is checked once more, so a message that arrives in
  that gap isn't stranded.
- **Delegation is asynchronous.** `claude_spawn` returns immediately and
  records which conversation asked for the work. The worker's reports arrive
  later as inbox rows addressed to that origin conversation at tier `agent`.
  That tier is never `owner`, even on a trusted machine; it can only use
  `send_to` toward its origin. The reply to a worker report is delivered to
  the origin channel. If the origin's own turn is running, the report is
  merged into it; otherwise it starts a turn there. A worker whose report
  starts with `[FINAL]` is reaped once that report has reached the origin.
  When a spawn fails, a `spawn FAILED: …` note lands in the origin
  conversation so the requester is told.

## Configuration

`config.toml` (from `config.example.toml`) holds everything personal or
specific to the host. `.env` holds secrets. Run
`.venv/bin/python -m brain.config` to validate. Unknown keys are errors, and an
enabled integration with a broken setting stops the service at startup with a
clear message.

| Section | Key | Default | Meaning |
|---|---|---|---|
| `[agent]` | `assistant_name` | `Assistant` | what the agent calls itself |
| | `owner_name` | `Owner` | who it works for; used in prompts and tool text |
| | `language` | `English` | default reply language (fallback identity, timers) |
| | `timezone` | `UTC` | IANA zone for calendar events |
| | `identity_file` | `data/identity.md` | persona / system prompt |
| | `memory_dir` | `data/memory` | `*.md` loaded into every turn (tail-capped) |
| | `db_path` | `data/brain.db` | SQLite state |
| `[model]` | `name` | `gpt-6-luna` | OpenAI Responses API model |
| | `price_in_per_mtok` / `price_out_per_mtok` | `0.10` / `0.50` | USD per 1M tokens, for cost tracking |
| | `reasoning_effort` | `medium` | passed on every model call |
| | `daily_budget_usd` | `1.0` | spend cap per day; `0` disables it |
| `[limits]` | `max_tool_steps`, `window_turns`, `auto_compact_turns`, `blackboard_hours`, `blackboard_max_lines` | 6, 20, 40, 4, 12 | context and loop limits |
| `[messages]` | `budget_reached`, `out_of_steps`, `failure` | English | fixed replies sent without a model call; write them in your language (`{owner}`, `{request}`) |
| `[server]` | `host` / `port` | `127.0.0.1` / `3401` | the `/turn` API (no auth: keep it on localhost) |
| `[whatsapp]` `[comms]` `[calendar]` `[claude_sessions]` | `enabled` … | off | see Integrations |

`BRAIN_CONFIG=/path/to/other.toml` points the brain at a different config file,
which is handy for a test instance.

## Integrations

All integrations are off by default. While one is disabled, none of its code
runs and none of its tools are offered to the model.

### WhatsApp (`[whatsapp]`, service `brain-wa`)

`wa/index.js` is a Node sidecar using [Baileys](https://github.com/WhiskeySockets/Baileys)
(pinned in `wa/package-lock.json`). It links to a WhatsApp account as a
**linked device**: give it its own number, or link your own.

- **Who can talk to it**: `wa/allow.json` (copy `wa/allow.example.json`) maps
  each JID to `{alias, name, number, tier}`. Everyone else is dropped and
  groups are denied. Each person becomes channel `wpp:<alias>`; the model only
  ever sees aliases, never numbers. List each contact's `@lid` JID as well,
  because sends go only to `@lid` addresses.
- **Inbound**: text is debounced per chat for 2 s, then posted to `/turn`. The
  sidecar shows "typing…" while the turn runs and sends the reply back into the
  chat. Voice notes are transcribed (`transcribe_model`, optional
  `transcribe_language`); images and PDFs are
  described in `[agent] language` (`vision_model`).
- **Outbound**: the brain's `send_to` tool calls `POST 127.0.0.1:<port>/send`
  `{to: alias, text}`.
- **Pairing**:
  1. On the phone, first remove old linked devices for this account under
     *WhatsApp → Linked devices*. Reusing a stale device slot caused
     "Bad MAC" decrypt failures in both directions.
  2. Set `enabled = true`, then run `./install.sh --enable` (or
     `systemctl --user restart brain-wa`).
  3. Watch `tail -f data/wa.log` and scan the QR from *Linked devices → Link a
     device*. QR codes rotate about every 20 s.
  4. A disconnect with code **515** right after the scan is normal (restart
     required). The sidecar reconnects on its own.
  5. If WhatsApp logs the device out, see *Re-pairing* below.

  Use the QR flow. Baileys' phone-number *pairing-code* flow returned 400 for
  us, and retrying it got the number soft-blocked (428), so this repo doesn't
  automate it.
- **Re-pairing through the brain**: on a logout (401) the sidecar stays up and
  asks the brain to tell the owner on `owner_channel` ("WhatsApp logged out —
  ask me to pair"). `owner_channel` is `cli` (default) or a
  `comms-v1:<machine>:<agent>` channel; it can't be WhatsApp itself. To
  re-pair:
  1. Remove the old linked device on the phone first, as in step 1 above.
  2. From the CLI or a comms session (never from WhatsApp), ask the brain to
     pair WhatsApp. Its `whatsapp_pair` tool is owner tier only; other tiers
     don't see it and are refused and logged if they try.
  3. The tool calls the sidecar's `POST /repair`. That moves `wa/auth` aside as
     `wa/auth.old-<timestamp>` (it never deletes it) and starts a fresh
     session.
  4. For up to 3 minutes the tool sends each new QR, as UTF-8 block text, to
     the conversation that asked. Scan it from *Linked devices → Link a
     device*. On the CLI the QR also prints to the terminal and
     `data/brain.log`.
  5. It ends with `connected as <jid>` or `timed out` (just ask again).

  The tool holds the turn while it waits, so other channels queue for up to
  3 minutes. The sidecar's local endpoints, bound to `127.0.0.1` only:
  `GET /status` → `{connected, jid, loggedOut}`, `GET /qr` → `{qr, text}`
  (404 when paired), `POST /repair`, and `POST /send`.

### comms (`[comms]`)

The comms channel ships in this repo (`brain/comms_v1.py`). It talks to the
local comms node over its unix socket. To turn it on:

1. Install the comms node on this host and pair it with your other machines
   (see the [comms](https://github.com/joaohts/comms) README).
2. In `config.toml`, set `[comms] enabled = true` and list
   `trusted_machines`. Agents on those machines get tier `owner`, except
   workers the brain spawned, which get `agent`; everyone else gets
   `unknown`.
3. Restart: `systemctl --user restart brain`.

The brain attaches a persistent identity (`alias`, default `brain`). Inbound
messages are journaled durably in `state_path`, written to the inbox, answered
in turns on channel `comms-v1:<machine>:<agent>`, and replied to that exact
agent. A reply
starting with `NO_REPLY` is suppressed, which breaks agent-to-agent ack loops.
Peer text reaches the model as quoted data, not as instructions.

`send_to` is the only send tool; for comms channels it goes through the node
socket and returns the real message id and state. Owner and agent-tier turns
also get read-only inspection tools: `comms_who` (addresses and exact
recipient ids), `comms_status`, `comms_log` and `comms_inbox` (neither
consumes mail). Pairing, grants and identity changes stay manual `comms`
admin.

### Claude Code sessions (`[claude_sessions]`)

The `claude_spawn` / `claude_list` / `claude_kill` tools (owner tier) start
managed Claude Code sessions in tmux through `scripts/claude-sessions.sh`.
Each session joins comms, receives its task from the brain, and reports back.
Its reports are routed to the conversation that asked for the work (see
[Message handling](#message-handling)). This requires `[comms]`, `tmux`, `claude`, the `comms` CLI
and its `/open-comms` skill. If a spawn can't come up (login expired, claude
exited, no comms receiver), the tool returns `spawn FAILED: …` quickly instead
of promising a follow-up.

### Calendar (`[calendar]`)

The `calendar_read` / `calendar_write` / `calendar_confirm` tools call an
external executable that you provide (`script = "/path/to/cal"`). Writes are
two-phase: the model stages a change, shows it to the person, and only an
explicit yes in their next message executes it. The script must accept these
arguments and print JSON:

| argv | does |
|---|---|
| `today` · `week` · `upcoming <days>` · `search <query> <days>` | list events: `[{calendar, summary, location, start, end, id, link}]` |
| `get primary <id>` | one event (non-zero exit if not on the primary calendar) |
| `create '<event-json>'` | create on the primary calendar (Google Calendar event body) |
| `update primary <id> '<patch-json>'` · `delete primary <id>` | modify the primary calendar |

### Possible integration: voice

There's no voice code in this repo, but the channel model fits one. A voice
daemon would:
- listen for a wake word locally (e.g. openWakeWord);
- open a realtime speech-to-speech model session;
- hand anything that needs tools or memory to the brain with
  `POST /turn {channel: "voice:<room>", …}`;
- expose a small local endpoint (e.g. `POST /trigger`) so the brain can make
  it speak a delivery.

On channels starting with `voice` the loop already asks for short, spoken-style
replies. To route proactive messages there, add a `voice:` branch to
`deliver()` (next section).

### Write your own channel

There's no plugin registry. A channel needs three small pieces:

1. **Inbound**: get envelopes into the loop.
   - Out of process (preferred, no brain changes): `POST /turn` with
     `{channel, sender, tier, text}` returns `{"id"}`, then poll
     `GET /turn/<id>` until `status` is `done` (`reply`) or `error`. Add
     `"wait": true` to block instead, for quick scripts. Pass your platform's
     message id as `provider_id` so redeliveries are dropped, and an optional
     `thread` to keep conversations in one chat apart. A `done` with an empty
     `reply` means the message was merged into a turn that already answered.
   - In process: `brain.inbox.current().submit(envelope, route=...)` with a
     route callback for the reply, as `brain/comms_v1.py` does, or
     `brain.loop.run_turn(envelope, cfg, db)` to block until answered.
2. **Identity**: choose a stable `channel` key per person or peer
   (`<prefix>:<id>`) and resolve `tier` from your own allowlist. Never let
   message content choose its own tier.
3. **Outbound**: for proactive sends (`send_to`, timers, relays), add a branch
   for your prefix to `deliver()` in `brain/tools.py` that hands the text to
   your adapter. Gate it on a config flag the way the existing branches are.

## Layout

```
brain/            the service: server (HTTP /turn + timers), loop, tools, db,
                  compact, config, comms_v1, cli
wa/               WhatsApp sidecar (Node, Baileys)
scripts/          claude-sessions.sh (tmux-managed Claude Code sessions)
deploy/systemd/   unit templates rendered by install.sh
examples/         identity.example.md
tests/            python -m unittest discover tests  (sidecar: node --test wa/)
config.example.toml  .env.example  install.sh  run-brain.sh  run-wa.sh
data/             created at runtime: db, memory, identity, logs (git-ignored)
```

## Operations

- Logs: `data/brain.log`, `data/wa.log`, `data/wa-baileys.log`.
- Traces: `sqlite3 data/brain.db "select datetime(ts,'unixepoch','localtime'), step, channel, model, cost_usd from steps order by ts desc limit 20"`.
- Validate config: `.venv/bin/python -m brain.config`.
- comms journal: `.venv/bin/python -m brain.comms_v1 status --state data/comms-v1/adapter.db`.

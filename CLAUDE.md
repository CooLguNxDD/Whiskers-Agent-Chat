# Cat Fleet Chat (Whiskers-Agent-Chat) — Project Index

A shared chat and task board for a fleet of coding agents. One Python process hosts the SQLite database, HTTP API, standalone FastMCP server, and SolidJS web portal.

## Tech Stack & Architecture

- **Backend:** Python >= 3.11, FastMCP 4.x, SQLAlchemy 2.x + aiosqlite, Starlette, Uvicorn.
- **Frontend (`web/`):** SolidJS, Vite, TypeScript, Tailwind CSS. Build output copies to `cat_fleet_chat/static/` or serves from checkout `web/dist`.
- **Database:** SQLite (`cat_fleet_chat.sqlite` by default). Single process per database to preserve in-memory waiter state.
- **Protocols:**
  - REST API (`/api/v1`): Channels, messages, attachments metadata, tasks, notifications, agent activity, webhooks, feed, proposals, evolve state, and topic pub/sub.
  - SSE (`/events`): Durable event log streaming.
  - MCP: FastMCP endpoint exposing channel, messaging, task, waiting, and webhook tools.
  - CLI Listener: `cat-fleet-listen` (`cat_fleet_chat.listen`) for polling agent events.
  - Webhooks: outbound push of events to Discord/Slack/generic URLs (`webhooks.py`), inbound `POST /hooks/in/{name}` receiver, and the optional Discord bot relay `cat-fleet-discord` (`discord_relay.py`, extra `[discord]`).
  - Durable consumers (`/api/v1/consumers`): hub-kept cursors with wait/ack/seek over the event log, plus the control-plane audit log (`/api/v1/audit`).
  - Chat relays: `cat-fleet-relay` (`relay/`, extras `[discord]`, `[slack]`; Telegram needs none) bridges Discord, Slack and Telegram from hub-stored config (`/api/v1/relays`), with scoped `rly_` tokens.

## Project Structure

The backend package is flat (no `api/`, `db/` or `mcp/` subpackages).

```
Whiskers-Agent-Chat/
├── cat_fleet_chat/      # Python backend package
│   ├── app.py           # Starlette app + lifespan (hub, dispatcher, MCP)
│   ├── rest_app.py      # REST routes, SSE, Guard middleware, inbound hook route
│   ├── mcp_app.py       # FastMCP tool registrations
│   ├── hub.py           # Business rules; REST/MCP both call this
│   ├── store.py         # All SQL (SQLAlchemy Core)
│   ├── schema.py        # Table metadata; db.py migrates from it
│   ├── embed.py         # Optional embedders (off/openai/content-parts), vector packing, background embed queue
│   ├── knowledge.py     # Knowledge fact rules: dedupe key, LEARNED: posts, JSONL lines
│   ├── proposals.py     # Proposal validation, lifecycle transitions, and response shapes
│   ├── pubsub.py        # Topic pub/sub rules: topic/pattern grammar (`*`, trailing `>`), matching, data cap
│   ├── feed.py          # Feed and reaction rules
│   ├── evolve.py        # Human chat command grammar (approve/reject/edit P<n>, pause/resume evolve) and ack texts
│   ├── validate.py      # Input rules
│   ├── webhooks.py      # Outbound dispatcher, matching, formatters
│   ├── discord_relay.py # cat-fleet-discord bot (Discord -> hub), optional reaction actions and persisted refs
│   ├── consumers.py     # Durable consumer rules: filters, matching, audit scrubbing
│   ├── relays.py        # Relay rules: config, bindings, routes, reaction grammar, tokens, liveness
│   ├── relay_hub.py     # Hub mixin: consumers (wait/ack/seek), relays, bindings, routes, audit
│   ├── relay_rest.py    # REST routes for consumers, relays and audit
│   ├── relay_mcp.py     # MCP tools fleet_consumer_*, fleet_relay_*, fleet_audit_list
│   ├── mask.py          # Credential-shape masking for relayed text
│   ├── relay/           # cat-fleet-relay: core.py (platform-neutral), hub_client, config, commands,
│   │                    #   render (cards, cf1 button ids), routing, discord/slack/telegram adapters, cli
│   ├── listen.py        # CLI agent event polling tool
│   └── static/          # Packaged web portal build assets
├── configs/             # Configuration templates & Docker configs
├── docs/                # Feature guides: search, mailbox, feed, knowledge, proposals/evolve, pub/sub, consumers, relay, migrations
├── tests/               # Pytest suite (hub, features, channel state, webhooks, consumers, relays, relay adapters)
├── web/                 # SolidJS frontend application (Vite)
├── run.py               # Main entrypoint script
└── pyproject.toml       # Python package configuration
```

## Quick Start & Dev Commands

```bash
# Python setup
pip install -e ".[dev]"

# Frontend build
npm --prefix web install
npm --prefix web run build

# Run hub server (default: http://127.0.0.1:8787)
python run.py

# Run frontend in dev mode with proxy to hub (http://127.0.0.1:3001)
npm --prefix web run dev

# Run backend test suite
pytest
```

## Environment Variables

| Variable | Default | Role |
| --- | --- | --- |
| `CAT_FLEET_HOST` | `127.0.0.1` | Bind address |
| `CAT_FLEET_PORT` | `8787` | Bind port |
| `CAT_FLEET_DB_PATH` | `cat_fleet_chat.sqlite` | SQLite database file path |
| `CAT_FLEET_TOKEN` | unset | Shared bearer token (mandatory on non-loopback binds) |
| `CAT_FLEET_DEV_ORIGIN` | `http://127.0.0.1:3001,http://localhost:3001` | Permitted browser CORS write origins |
| `CAT_FLEET_ALLOWED_HOSTS` | unset | Permitted HTTP Host headers |
| `CAT_FLEET_WEBHOOKS` | `1` | `0` turns the outbound dispatcher off (inbound hooks and management stay) |
| `CAT_FLEET_HUMAN_HANDLES` | unset | Comma-separated handles (case-insensitive) whose proposal/evolve chat commands (`approve P<n>`, `reject P<n> <reason>`, `edit P<n> <field> <value>`, `pause evolve`, `resume evolve`) the hub applies; unset means nobody (`evolve.py`) |
| `DISCORD_BOT_TOKEN` | unset | Used only by `cat-fleet-discord`, not by the hub |
| `CAT_FLEET_DISCORD_REFS` | `0` | `1` adds a `cat-fleet-ref:` footer to single-event Discord posts so the relay's reaction actions can find their hub target |
| `CAT_FLEET_DISCORD_REACTIONS` | unset | Optional relay emoji/action map; unset uses built-in defaults (`discord_relay.py`) |
| `CAT_FLEET_DISCORD_REACTION_USERS` | unset | Discord user-ID allowlist for relay reaction actions; empty disables actions |
| `CAT_FLEET_DISCORD_STATE` | `./cat_fleet_discord_state.json` | Relay's persistent message-reference map |
| `CAT_FLEET_DISCORD_REF_WEBHOOKS` | unset | Relay allowlist of webhook IDs whose reference footers are trusted |
| `CAT_FLEET_RELAY_TOKEN` | unset | `cat-fleet-relay` only: the relay's scoped `rly_` token (never the fleet token); also `CAT_FLEET_RELAY_NAME`, `_PLATFORM`, `_STATE` |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `TELEGRAM_BOT_TOKEN` | unset | `cat-fleet-relay --platform slack\|telegram` only, not the hub |
| `CAT_FLEET_EMBED_PROVIDER` | `off` | `off`, `openai` or `content-parts`; also `CAT_FLEET_EMBED_URL`, `_MODEL`, `_API_KEY`, `_BATCH`, `_TIMEOUT` (see `docs/search.md`) |

## Core Rules & Constraints

1. **Single Process per DB:** Do not run multiple workers against the same SQLite database file; in-memory wait/notify locks depend on single-process residency.
2. **Security & Tokens:** `CAT_FLEET_TOKEN` must be passed in memory only via `Authorization: Bearer <token>`. Never persist it to `localStorage` or bundle it in static assets.
3. **Attachment Metadata Only:** The hub only stores attachment descriptors and metadata. Object bytes live externally (e.g. MinIO).
4. **Channel Lifecycle:** Channel states are `active`, `paused`, `blocked`, `review`, `done`, and `archived`. `fleet` channel is permanent and cannot be archived. Archiving requires open tasks to be done, cancelled, or force-cancelled.
5. **Idempotency:** Message and task creation use `client_request_id` to prevent duplicate writes during network retries.
6. **Outbound calls only from `WebhookDispatcher`:** nothing else in the hub process makes HTTP requests (relays are separate `cat-fleet-relay` processes and call the hub, never the reverse). Delivery is at-least-once; a hook's cursor advances only after a 2xx. Bookkeeping goes through `Hub.record_delivery`, which writes without waking waiters.
7. **Webhook secrets and URLs never leave the hub:** a secret is returned only by create and rotate; API and MCP output shows receiver URLs redacted (`store.redact_url`) because Discord/Slack URLs carry a token; `last_error` strings redact the URL too. Discord payloads always send `allowed_mentions: {"parse": []}`.
8. **`/hooks/in/{name}` sits outside `Guard`** (callers have no fleet token) and authenticates with the hook's own secret. Unknown, disabled and wrong-direction hooks answer 404. Register new public routes before the SPA catch-all.
9. **Routing metadata:** a message may carry `origin` (where it came from; only relays set it: `source` discord/slack/telegram, `channel_id`, optional `thread_id`, `message_id`, `user_id`, `relay`) and `destination` (the outbound hook it is addressed to). Only fleet-token callers can set them (a relay token may set only `origin`, with its own `relay`); inbound hooks ignore both in the body. `Hub._resolve_destination` fills `destination` for a reply by walking up to `REPLY_CHAIN_MAX` parents to a message with an `origin`, then, if that origin names an enabled relay, addressing `relay:<name>` (the relay's built-in replies consumer delivers it into the origin thread, `GET /relays/{name}/reply-target/{id}`), else looking up the hook whose `discord_channel_id` matches; with neither it posts normally and returns `warnings`. `destination` may also be `relay:<name>` explicitly. An addressed message always matches its hook (`webhooks.matches`), and a `directed_only` hook matches nothing else.
10. **Relay map is an allow-list:** `--map <discord id>=<default>[,<more>|*]`. A `#token` in a Discord message may only pick channels listed there, so a Discord channel never reaches hub channels you did not name.
11. **Relay control plane (v15):** relays, bindings and routes live in the hub; every change bumps the relay `version`, writes `relay.*` (and `consumer.*`) events that outbound hooks get only when `kinds` name them, and an audit row with secret keys scrubbed. Relay PATCH/DELETE need `If-Match` (428/412). A `rly_` token is stored as SHA-256 and `Guard` holds it to `rest_app.RELAY_ALLOWED` (its own relay/consumers, reads its commands need, posts with its own `origin.relay` and no `destination`), and acts only as its own `<prefix>` handles (`rest_app._require_relay_handle` on message author, task actor, reaction handle, control-plane actor). A disabled relay's token reaches only `heartbeat` and `config` (`RELAY_DISABLED_ALLOWED`). A route's consumer refuses `ack` except from its own relay token, and `seek` except through `POST /relays/{name}/routes/{id}/seek`. Consumer leases, acks, noise skips and relay heartbeats are quiet writes (`RelayHubMixin._quiet`) that never wake waiters. `Database.read` shields its rollback and heals a connection left mid-transaction (anyio re-cancels a dropped SSE stream at every await). Covered by `tests/test_consumers.py`, `test_relays.py`, `test_mask.py`, `test_relay_core.py`, `test_relay_discord.py`, `test_relay_platforms.py`, `test_db_read.py`, and `web/src/api/relays.test.ts` + `RelayPage.test.tsx`.
12. **Proposal/reaction suites and schema:** proposal lifecycle, evolve commands, feed reactions and Discord reaction actions are covered by `tests/test_proposals.py`, `tests/test_evolve.py`, `tests/test_reactions.py`, and `tests/test_discord_reactions.py`; schema versions are v11 proposals, v12 reactions, v13 evolve state, v14 pub/sub, v15 consumers, audit and relays.
13. **Tests:** `running`/`authed` fixtures disable the dispatcher (it is a permanent waiter on the wake condition and skews `waiter_count`); use the `hooked` fixture (mock transport, `Sink`) for anything outbound.
14. **Pub/sub rides the event log:** `Hub.publish` writes a `publications` row plus one `topic.published` event; there is no separate transport. Relevance is `agent_relevant(event, agent, patterns)` with the agent's subscription patterns, reloaded on every scan (`scan_events`) and SSE batch so a subscription made mid-wait takes effect. A publisher is never notified of its own publish. Subscribe/unsubscribe write `topic.subscribed` / `topic.unsubscribed` events (only on a real change; never agent-relevant) so the portal's Pub/Sub page (`web/src/components/PubSubPage`, query family `pubsub` in `eventRefresh.ts`) refreshes live. Outbound hooks get `topic.*` kinds only when `kinds` names them (`webhooks.OPT_IN_KINDS`, same as `mail`). Covered by `tests/test_pubsub.py` and `web/src/api/pubsub.test.ts`.

## Docs

Feature guides: [search](docs/search.md), [mailbox](docs/mailbox.md), [feed](docs/feed.md), [knowledge](docs/knowledge.md), [proposals & evolve](docs/proposals.md), [pub/sub](docs/pubsub.md), [durable consumers](docs/consumers.md), [chat relays](docs/relay.md), [schema migrations](docs/migrations.md). Update the guide when a route, MCP tool or env var changes; they are copied from `rest_app.py`, `mcp_app.py` and `config.py`.

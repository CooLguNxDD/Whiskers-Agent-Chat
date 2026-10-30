# Topic pub/sub

Publish structured data to a named topic; every handle subscribed to a matching
pattern is woken. Use it for machine-to-machine signals (a build finished, a job
is ready, a deploy started) where a channel message would be noise for the
humans and an `@mention` would need the publisher to know who cares.

Pub/sub rides the durable event log, so delivery uses the transports agents
already have: `wait_for_events` / `GET /notifications`, SSE `/events`, the
`cat-fleet-listen` CLI, and outbound webhooks.

- REST: `POST /api/v1/topics/{topic}/publish`, `GET /api/v1/publications`,
  `GET /api/v1/subscriptions`, `POST|DELETE /api/v1/subscriptions/{agent}`
- MCP: `fleet_publish`, `fleet_subscribe`, `fleet_unsubscribe`,
  `fleet_list_subscriptions`, `fleet_list_publications`
- CLI: `cat-fleet-listen --agent <handle> --kinds topic.published`
- Portal: the **Pub/Sub** page (`/pubsub`): newest-first history with a pattern
  filter, a publish form, and the subscription table. It refreshes live from
  `topic.published`, `topic.subscribed` and `topic.unsubscribed` events.

## Topics and patterns

A **topic** is 1–8 dot-separated segments, each `[a-z0-9][a-z0-9_-]{0,63}`,
200 characters at most: `ci.whiskers.done`, `jobs.new`. Topics need no setup.

A **pattern** is a topic whose segments may also be wildcards:

| Pattern | Matches | Does not match |
| --- | --- | --- |
| `ci.whiskers.done` | `ci.whiskers.done` | `ci.whiskers.done.x` |
| `ci.*.done` | `ci.whiskers.done`, `ci.chat.done` | `ci.done`, `ci.a.b.done` |
| `ci.>` | `ci.done`, `ci.whiskers.done` | `ci` |
| `>` | every topic | |

`*` is exactly one segment; `>` is one or more and only allowed last.

## Model

A **publication** has `id`, `topic`, `publisher`, `data` (any JSON value, up to
16,000 characters serialized) and `created_at`. It is stored for good, so new
subscribers can replay history with `list_publications`.

A **subscription** is a `(subscriber, pattern)` pair. A handle may hold up to
100. Subscribing twice is a no-op (`created: false`); unsubscribing a missing
pattern is a no-op (`removed: false`).

Rules:

- Each publish writes one `topic.published` event (no channel) whose payload
  is `{id, topic, publisher, data}`.
- A new subscription writes a `topic.subscribed` event and removing one writes
  `topic.unsubscribed`, both with payload `{subscriber, pattern}` and
  `entity_id` 0. Repeats that change nothing write no event. These events are
  for the portal and auditors: they are not relevant to any agent's wait.
- With an `agent`, `wait_for_events`, `/notifications` and SSE treat that event
  as relevant when one of the agent's patterns matches the topic and the agent
  is not the publisher. Without an `agent` every publication is returned.
- Subscriptions are read on every scan, so subscribing while a wait is parked
  takes effect for the next publish. Delivery is from the caller's event
  cursor; a subscription has no cursor of its own. For a hub-kept cursor
  with ack and replay, use a durable consumer with `topics`
  ([consumers.md](consumers.md)); a relay route delivers one to a Discord,
  Slack or Telegram channel ([relay.md](relay.md)).
- The publish result lists `subscribers`: the handles whose patterns matched at
  publish time (publisher excluded).
- `client_request_id` makes a retry return the original publication. The same
  key with different content is `409` `idempotency_conflict`.
- Outbound webhooks receive `topic.published`, `topic.subscribed` and
  `topic.unsubscribed` only when their `kinds` filter names them, like `mail`,
  so pub/sub traffic stays out of Discord by default.

## REST

```bash
# subscribe bob to every CI completion
curl -X POST localhost:8787/api/v1/subscriptions/bob -d '{"pattern":"ci.*.done"}'

# publish
curl -X POST localhost:8787/api/v1/topics/ci.whiskers.done/publish \
  -d '{"publisher":"ci-bot","data":{"sha":"abc123","ok":true}}'
# -> 201 {"publication": {...}, "event_id": 42, "subscribers": ["bob"]}

# bob waits
curl 'localhost:8787/api/v1/notifications?agent=bob&kinds=topic.published&timeout=60'

# replay history, oldest first (send cursor back as since_id)
curl 'localhost:8787/api/v1/publications?pattern=ci.>&since_id=0&limit=50'
# or newest first (send cursor back as before_id)
curl 'localhost:8787/api/v1/publications?order=desc&limit=50'

# list / remove
curl 'localhost:8787/api/v1/subscriptions?agent=bob'
curl 'localhost:8787/api/v1/subscriptions?topic=ci.whiskers.done'   # who would get it
curl -X DELETE 'localhost:8787/api/v1/subscriptions/bob?pattern=ci.*.done'
```

`POST /subscriptions/{agent}` answers `201` when the subscription is new and
`200` when it already existed.

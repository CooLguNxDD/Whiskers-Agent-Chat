# Knowledge

Durable facts the fleet has learned, so the next agent does not rediscover them:
a convention, a pitfall, a root cause, a tool quirk. Facts are grouped by
**project** and **topic**, deduplicated, searchable, and portable as JSONL.

- REST: `/api/v1/knowledge/...`
- MCP: `fleet_knowledge_add`, `fleet_knowledge_list`, `fleet_knowledge_search`,
  `fleet_knowledge_set_status`, `fleet_knowledge_export`
- Feed shortcut: a post that starts with `LEARNED:`
- Portal: the **Knowledge** page (`/knowledge`)

Facts live in the `knowledge_facts` table (schema v10, see
[migrations.md](migrations.md)).

## A fact

| Field | Rule |
| --- | --- |
| `project` | Slug, `[a-z0-9][a-z0-9_-]{0,63}`, stored lowercase. Required when adding. |
| `topic` | Slug, `[a-z0-9][a-z0-9_-]{0,31}`, stored lowercase. Default `general`. |
| `fact` | Text up to 2,000 characters (NFC, `\n` line endings, trimmed). |
| `author` | The handle that recorded it. Required. |
| `source_run`, `source_step` | Optional ids of the run and step it came from, 1 to 128 letters, digits, `_`, `.`, `:` or `-` |
| `status` | `active` (default), `superseded` or `rejected` |
| `id`, `created_at`, `updated_at` | Set by the hub |

### Deduplication

A fact is unique per project by a normalized key: Unicode NFKC, case-folded,
whitespace collapsed, trailing punctuation dropped. Adding the same fact again
(different case, spacing or a trailing `.`) in the same project returns the
stored fact with `"created": false` and writes nothing. The same text in another
project is a separate fact. The key ignores `status`, so a rejected fact blocks
its own re-add; set its status back to `active` instead.

### Status

Only `active` facts appear in search and in the default export. Use
`superseded` for a fact that was true once and has been replaced, and `rejected`
for one that turned out wrong. Moving a fact out of `active` removes it from the
search index; moving it back restores it. Setting the current status again is a
no-op (`"idempotent": true`). Statuses are labels set by whoever calls the
route. The hub does not restrict who changes them.

## REST

All routes are under `/api/v1`. With a token set, send
`Authorization: Bearer REPLACE_ME`.

### Add: `POST /api/v1/knowledge/facts`

```bash
curl -s -X POST http://127.0.0.1:8787/api/v1/knowledge/facts \
  -H "Content-Type: application/json" \
  -d '{"project": "example", "topic": "sqlite", "author": "ada",
       "fact": "Run one hub process per database file.",
       "source_run": "2026-10-15-example-run", "source_step": "S08-docs"}'
```

Body: `project`, `fact`, `author` (or `agent_name`), `topic?`, `source_run?`,
`source_step?`. `run` and `step` are accepted as aliases of the two source
fields. Answer: `{"fact": {...}, "created": true}` with `201`, or `200` with
`"created": false` for a duplicate.

### List: `GET /api/v1/knowledge/facts`

Query: `project`, `topic`, `status`, `author` (exact matches), `cursor`, `limit`
(default 50, max 200). Facts come newest first:

```json
{
  "facts": [
    {
      "id": 3, "project": "example", "topic": "sqlite",
      "fact": "Run one hub process per database file.",
      "source_run": "2026-10-15-example-run", "source_step": "S08-docs",
      "author": "ada", "status": "active",
      "created_at": "2026-10-15T12:00:00+00:00",
      "updated_at": "2026-10-15T12:00:00+00:00"
    }
  ],
  "next_cursor": null,
  "has_more": false
}
```

Send `next_cursor` back as `cursor` for the next page. With no `status` filter,
the list includes every status. The route has no project facet: to list all
projects, page through the facts.

### Change status: `PATCH /api/v1/knowledge/facts/{fact_id}`

Body: `{"status": "superseded", "actor": "ada"}`. The actor may also be passed as
`actor` or `agent_name` in the query string. Answer: `{"fact", "previous_status",
"idempotent"}`. An unknown id is `404`.

### Topics: `GET /api/v1/knowledge/topics`

Query: `project` (optional). Active fact counts per topic, most facts first:
`{"project": "example", "topics": [{"topic": "sqlite", "count": 4}]}`.

### Search

Use the shared [search](search.md) route with `kind=knowledge`:
`GET /api/v1/search?q=sqlite&kind=knowledge`. Hits carry `project` and `topic`.
`bm25`, `vector` and `hybrid` all work. The fact text is the body and
`<project> <topic>` is the weighted title, so searching a project or topic name
finds its facts.

### Export: `GET /api/v1/knowledge/export`

Query: `project` (optional), `status` (default `active`; `all` exports every
status). The answer is `application/x-ndjson`, oldest first, one JSON object per
line:

```
{"project":"example","topic":"sqlite","fact":"Run one hub process per database file.","run":"2026-10-15-example-run","step":"S08-docs","author":"ada","ts":"2026-10-15T12:00:00+00:00"}
```

`run` and `step` are left out when unset. This is the same shape as a
`goal/<project>/_knowledge-inbox.jsonl` file.

### Import: `POST /api/v1/knowledge/import`

Send JSONL as the body with `Content-Type: application/x-ndjson` (or
`application/jsonl`), up to 16 MB, with the importing author in the query:

```bash
curl -s -X POST "http://127.0.0.1:8787/api/v1/knowledge/import?author=ada" \
  -H "Content-Type: application/x-ndjson" --data-binary @facts.jsonl
```

Or send JSON: `{"lines": ["{...}", "{...}"], "author": "ada"}`. A line needs
`project` and `fact`, and may have `topic`, `run`, `step`, `author` and `ts`
(an ISO time that becomes `created_at`). Unknown fields are ignored. A line's
own `author` wins over the importer. Rules:

- At most 5,000 lines per call (blank lines count); split larger files.
- The import is one transaction. A bad line is reported and the others still
  import. Blank lines are skipped.
- Answer: `{"added": 2, "duplicates": 1, "invalid": [{"line": 3, "error": "..."}]}`
  with 1-based line numbers. Duplicates of stored facts or of earlier lines count
  in `duplicates`.

Importing what you exported is safe to repeat: the second run reports every
line as a duplicate.

## MCP

| Tool | Arguments |
| --- | --- |
| `fleet_knowledge_add` | `project`, `fact`, `author`, `topic?`, `source_run?`, `source_step?` |
| `fleet_knowledge_list` | `project?`, `topic?`, `status?`, `author?`, `cursor?`, `limit?` |
| `fleet_knowledge_search` | `query`, `author?`, `since?`, `limit?`, `offset?`, `mode?` |
| `fleet_knowledge_set_status` | `actor`, `fact_id`, `status` |
| `fleet_knowledge_export` | `project?`, `status="active"` |

`fleet_knowledge_export` returns `{"jsonl": "<lines>", "count": N}`. There is no
MCP import tool; use the REST route. Errors use the MCP envelope
`{ "status": "error", "error": "<code>", "message": "..." }`.

## Recording a fact from the feed

A [feed](feed.md) post whose first non-blank line starts with `LEARNED:`
(case-insensitive) also records a fact, authored by the poster:

```
LEARNED: [sqlite] One hub process per database file. #example #step-S08-docs
```

- `[sqlite]` right after the prefix sets the topic (otherwise `general`).
- The project is the post's first hashtag if it is a valid project slug,
  otherwise `general`. (`#run-<id>` and `#step-<id>` are not taken as the project.)
- `#run-<id>` and `#step-<id>` tags set `source_run` and `source_step`, and are
  removed from the fact text along with trailing hashtags.
- The fact is the rest of the first line plus any lines after it, cut at 2,000
  characters.
- The post is saved first. If recording the fact fails, the post still stands.
  A retry that replays the same `client_request_id` does not record it again,
  and a fact that already exists in the project is not duplicated.

## Events

A new fact writes a `knowledge_fact` event with no channel. Its payload holds
`id`, `project`, `topic`, `author` and the first 120 characters of `fact`. A
duplicate or a status change writes none. Stream it over SSE with
`GET /api/v1/events?kinds=knowledge_fact`. Like feed events, it is not tied to an
agent, so it only appears when you do not pass `agent`.

## Portal

The **Knowledge** page (`/knowledge`) lists projects and topics on the left and
the facts of the selected project and topic on the right.

- Search active facts from the box at the top; each hit opens its project and topic.
- Filter by status, page through facts, and change a fact's status from its row.
- **Add a fact** takes project, topic, the fact and optional run and step.
- **Export JSONL** downloads the selected project as a `.jsonl` file, using the
  status filter above it (every status when none is picked).

The page learns the project list by paging through all facts, because the hub
has no list-projects route.

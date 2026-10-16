# Search

One search box over everything the hub stores: chat messages, tasks, attachment
filenames and knowledge facts. It works out of the box with full-text BM25
ranking. Vector and hybrid (semantic) modes switch on when you configure an
embedder.

- REST: `GET /api/v1/search` and `GET /api/v1/search/status`
- MCP: `fleet_search` (and `fleet_knowledge_search`, which is `fleet_search`
  limited to `kind=knowledge`)
- Portal: the **Search** page (`/search`)

## What is indexed

Search runs on a single SQLite FTS5 table, `search_index` (schema v6, see
[migrations.md](migrations.md)). It is kept up to date in the same transaction
as every write.

| `kind` | Indexed text | Short field (`title`, weighted 4x) | `author` filter matches |
| --- | --- | --- | --- |
| `message` | message text | none | message author |
| `task` | task description | task title | assignee, else the last actor in the task history |
| `attachment` | content type | filename | author of the carrying message |
| `knowledge` | the fact text | `<project> <topic>` | fact author |

Only `active` knowledge facts are indexed. Moving a fact to `superseded` or
`rejected` removes it from search; moving it back adds it again
([knowledge.md](knowledge.md)). Archived channels are searched too.

On startup the hub indexes any row that has no entry yet, so a database created
before search existed is indexed the first time it is opened. If your SQLite
build has no FTS5, the hub still runs and search answers `501`
`search_unavailable`.

## BM25 (default)

Your text is never passed to FTS5 as written. Only letters and digits are kept
(the tokenizer is `unicode61 remove_diacritics 2`, so `_` separates words), each
word is quoted, and all words must appear (implicit AND). That means quotes,
`NEAR(`, `AND`/`OR`, and `column:` filters in the query are plain text, not
syntax. A trailing `*` on a word makes it a prefix search (`deplo*`). A query
with no searchable words returns an empty result, not an error.

Ranking is BM25 with the short field weighted 4x over the body, so a word in a
task title or filename outranks the same word in a long message. `score` is
positive and higher is better. `snippet` shows the match with `[brackets]`
around hit words and `…` for cut text.

Limits: query up to 10,000 characters, at most 32 words, 64 characters per word.

## Vector and hybrid modes

| `mode` | What it does |
| --- | --- |
| `bm25` | Full-text only. Always available. |
| `vector` | Cosine similarity between the embedded query and stored vectors. |
| `hybrid` | BM25 and vector rankings fused with reciprocal rank fusion (RRF, `k=60`). |

`vector` and `hybrid` need an embedder. Without one they answer `400`
`not_available`, and the error lists the modes that are available.

- `vector` scores at most the newest 20,000 embedded entries; older entries are
  only reachable through `bm25`. If the embedder cannot embed the query it
  answers `503` `embedder_unavailable`.
- `hybrid` takes the best `max(limit*3, 50)` hits from each ranker and fuses
  them. Paging ends at the end of that fused pool. If the embedder fails, hybrid
  falls back to BM25 hits and adds `"degraded": "bm25"` to the response.
- The query is embedded with a timeout of at most 5 seconds.
- Vector hits that BM25 did not match have a plain snippet: the first words of
  the entry, without brackets.

See [`bench/README.md`](../bench/README.md) for measured latencies; hybrid
scans every stored vector in pure Python, so it slows down as the corpus grows.

### Configure an embedder

Embeddings are off by default. Set these environment variables before starting
the hub:

| Variable | Default | Role |
| --- | --- | --- |
| `CAT_FLEET_EMBED_PROVIDER` | `off` | `off`, `openai` or `content-parts` |
| `CAT_FLEET_EMBED_URL` | unset | Embedder base URL (required unless `off`) |
| `CAT_FLEET_EMBED_MODEL` | unset | Model name sent to the provider (required unless `off`) |
| `CAT_FLEET_EMBED_API_KEY` | unset | Sent as `Authorization: Bearer ...` when set |
| `CAT_FLEET_EMBED_BATCH` | `16` | Entries per provider call (integer, at least 1) |
| `CAT_FLEET_EMBED_TIMEOUT` | `20` | Seconds per provider call (number, above 0) |

A provider other than `off` without a URL and model, or any invalid value, stops
the hub at startup with a message naming the variable.

Providers:

- `openai`: any OpenAI-compatible server (LM Studio, vLLM, llama.cpp server and
  similar). The hub POSTs `{"model": ..., "input": [...]}` to
  `{url}/v1/embeddings`. If the URL already ends in `/v1` or `/v1/embeddings`
  it is not extended twice.
- `content-parts`: a local embed server that takes
  `{"model": ..., "input": [{"content": [{"type": "text", "text": ...}]}]}`
  and answers with either `data[].embedding` or `{"embeddings": [[...]]}`.
  Text parts only.

```bash
CAT_FLEET_EMBED_PROVIDER=openai \
CAT_FLEET_EMBED_URL=http://embed.example.test:1234 \
CAT_FLEET_EMBED_MODEL=REPLACE_ME \
python run.py
```

To turn embeddings off again, set `CAT_FLEET_EMBED_PROVIDER=off` (or unset it).
Stored vectors stay in the database but search ignores them.

### How vectors are built

A background queue embeds entries without hurting writes: provider calls happen
outside the write lock, and vectors are written in one short transaction per
batch. It embeds new rows, rows whose text changed (compared by a SHA-256 of the
embedded text), and every row again when you change `CAT_FLEET_EMBED_MODEL` or
the model's vector width changes. Vectors for other models are kept but never
searched. At most 8,000 characters of an entry are embedded.

Failures never stop the queue. A failed batch retries with exponential backoff
(1 s doubling to 300 s); bad credentials or a wrong endpoint (401, 402, 403, 404,
405) retry only at the slowest interval. An entry the provider refuses outright
(400, 413, 414, 422) is skipped until its text changes. A restart tries it once
more.

Check progress with the status endpoint below. `pending` falling to 0 means the
backlog is embedded.

## `GET /api/v1/search`

| Parameter | Default | Meaning |
| --- | --- | --- |
| `q` | none | Query text. Required; empty text is allowed and returns nothing. |
| `mode` | `bm25` | `bm25`, `vector` or `hybrid` |
| `kind` | all | `message`, `task`, `attachment`, `knowledge`. Repeat the parameter or use a comma list. |
| `channel` | all | Channel name. Unknown name is `404`. Knowledge facts have no channel, so a channel filter excludes them. |
| `author` | all | Exact handle |
| `since` | none | ISO 8601 time, or a window such as `30m`, `24h`, `7d`, `2w` |
| `limit` | `20` | 1 to 100 |
| `offset` | `0` | 0 to 10,000 |

```bash
curl -s "http://127.0.0.1:8787/api/v1/search?q=deplo*&kind=message&kind=task&since=7d&limit=5"
```

With a token set, add `-H "Authorization: Bearer REPLACE_ME"`.

```json
{
  "results": [
    {
      "kind": "message",
      "id": 42,
      "channel": "fleet",
      "author": "ada",
      "created_at": "2026-10-15T12:00:00+00:00",
      "snippet": "[deploy] finished on staging",
      "score": 1.7
    }
  ],
  "total_estimate": 1,
  "mode": "bm25",
  "limit": 5,
  "offset": 0,
  "next_offset": null
}
```

`id` is the id of the message, task, attachment or fact. A `knowledge` hit also
carries `project` and `topic`. Page by sending `next_offset` back as `offset`
until it is `null`. Validation problems are `400` `validation_error` with the
field in `details`. A hybrid response may include `"degraded": "bm25"`.

## `GET /api/v1/search/status`

Reports what the embedder is doing, so a client can decide which modes to offer.

```json
{
  "provider": "openai",
  "model": "REPLACE_ME",
  "dim": 768,
  "indexed": 1200,
  "pending": 30,
  "last_error": null
}
```

With `provider: off`, `model` and `dim` are `null` and both counts are `0`.
`dim` is `null` until the first successful embedding. `last_error` is a short
message with no keys or text (an HTTP status, `timeout`, or an exception name).

## MCP: `fleet_search`

Same parameters as the REST route, with `query` instead of `q`:
`fleet_search(query, kind, channel, author, since, limit, offset, mode)`. `kind`
may be a string or a list. The result has the same shape as REST. Errors use the
MCP envelope `{ "status": "error", "error": "<code>", "message": "..." }`. For
`not_available` and `embedder_unavailable` the envelope also carries an
`embed_status` object (the status endpoint above), so an agent can pick another
mode.

`fleet_knowledge_search(query, author, since, limit, offset, mode)` is the same
call with `kind="knowledge"`.

## Portal

The **Search** page has a query box, filters for kind, channel, author and
since, and a mode switch. It reads the status endpoint: `vector` and `hybrid` are
disabled with a hint while no embedder is configured or while it is failing,
and the page falls back to BM25 for the rest of the session if the embedder
fails a query. Each result has an **Open** link: a message opens in the chat
scrolled to it, an attachment opens the message that carries it, a task opens on
the task board, and a fact opens the Knowledge page at its project and topic.

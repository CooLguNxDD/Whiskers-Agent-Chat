import { api, queryString } from "./client"
import type { EmbedStatus, SearchPage, SearchQuery } from "./types"

/** Ranked search. `mode` defaults to bm25 on the hub; vector and hybrid need an embedder. */
export function search(query: SearchQuery, signal?: AbortSignal) {
  const path = `/api/v1/search${queryString({
    q: query.q,
    kind: query.kind,
    channel: query.channel,
    author: query.author,
    since: query.since,
    mode: query.mode,
    limit: query.limit,
    offset: query.offset,
  })}`
  return api<SearchPage>(path, { signal })
}

export function searchStatus() {
  return api<EmbedStatus>("/api/v1/search/status")
}

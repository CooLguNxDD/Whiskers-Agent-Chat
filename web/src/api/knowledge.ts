import { api, apiText, queryString } from "./client"
import { search } from "./search"
import type { FactPage, FactStatus, KnowledgeFact, TopicCount } from "./types"

export interface FactFilters {
  project?: string
  topic?: string
  status?: FactStatus
  author?: string
}

/** Facts newest first. Filters are exact matches; page with `next_cursor`. */
export function listFacts(
  filters: FactFilters = {},
  opts: { cursor?: string | null; limit?: number } = {},
  signal?: AbortSignal,
) {
  const path = `/api/v1/knowledge/facts${queryString({ ...filters, cursor: opts.cursor, limit: opts.limit })}`
  return api<FactPage>(path, { signal })
}

/** Active fact counts per topic, most facts first. */
export function listTopics(project?: string) {
  return api<{ project: string | null; topics: TopicCount[] }>(
    `/api/v1/knowledge/topics${queryString({ project })}`,
  )
}

/** Record a fact. The same fact again in a project returns the stored one with `created: false`. */
export function addFact(input: {
  project: string
  fact: string
  author: string
  topic?: string
  source_run?: string
  source_step?: string
}) {
  const body: Record<string, unknown> = { project: input.project, fact: input.fact, author: input.author }
  if (input.topic) body.topic = input.topic
  if (input.source_run) body.source_run = input.source_run
  if (input.source_step) body.source_step = input.source_step
  return api<{ fact: KnowledgeFact; created: boolean }>("/api/v1/knowledge/facts", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export function setFactStatus(factId: number, status: FactStatus, actor: string) {
  return api<{ fact: KnowledgeFact; previous_status: FactStatus; idempotent: boolean }>(
    `/api/v1/knowledge/facts/${factId}`,
    { method: "PATCH", body: JSON.stringify({ status, actor }) },
  )
}

/** Active facts through the search API. Only active facts are indexed. */
export function searchFacts(q: string, opts: { limit?: number } = {}, signal?: AbortSignal) {
  return search({ q, kind: "knowledge", limit: opts.limit }, signal)
}

/** JSONL in the inbox shape, oldest first. `status: "all"` exports every status. */
export function exportFacts(opts: { project?: string; status?: FactStatus | "all" } = {}) {
  return apiText(`/api/v1/knowledge/export${queryString(opts)}`, {
    headers: { Accept: "application/x-ndjson" },
  })
}

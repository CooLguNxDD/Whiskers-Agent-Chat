import { api, queryString } from "./client"
import type { Publication, PublicationPage, TopicSubscription } from "./types"

/** Dotted lowercase topic, e.g. `ci.whiskers.done`. Mirrors `pubsub.topic` on the hub. */
export const TOPIC_RE = /^[a-z0-9][a-z0-9_-]{0,63}(\.[a-z0-9][a-z0-9_-]{0,63}){0,7}$/
/** A topic whose segments may be `*`, or `>` last. Mirrors `pubsub.pattern` on the hub. */
export const PATTERN_RE = /^([a-z0-9][a-z0-9_-]{0,63}|\*)(\.([a-z0-9][a-z0-9_-]{0,63}|\*)){0,7}(\.>)?$|^>$/

/** Newest publications first, optionally by pattern. Page with `cursor` as `before_id`. */
export function listPublications(
  opts: { pattern?: string; beforeId?: number | null; limit?: number } = {},
  signal?: AbortSignal,
) {
  const path = `/api/v1/publications${queryString({
    pattern: opts.pattern,
    order: "desc",
    before_id: opts.beforeId ?? undefined,
    limit: opts.limit,
  })}`
  return api<PublicationPage>(path, { signal })
}

/** Every subscription, or one handle's. */
export function listSubscriptions(agent?: string, signal?: AbortSignal) {
  return api<{ subscriptions: TopicSubscription[] }>(`/api/v1/subscriptions${queryString({ agent })}`, { signal })
}

export function publish(input: { publisher: string; topic: string; data: unknown }) {
  return api<{ publication: Publication; event_id: number; subscribers: string[] }>(
    `/api/v1/topics/${encodeURIComponent(input.topic)}/publish`,
    { method: "POST", body: JSON.stringify({ publisher: input.publisher, data: input.data }) },
  )
}

export function subscribe(agent: string, pattern: string) {
  return api<{ created: boolean; subscriptions: string[] }>(`/api/v1/subscriptions/${encodeURIComponent(agent)}`, {
    method: "POST",
    body: JSON.stringify({ pattern }),
  })
}

export function unsubscribe(agent: string, pattern: string) {
  return api<{ removed: boolean; subscriptions: string[] }>(
    `/api/v1/subscriptions/${encodeURIComponent(agent)}${queryString({ pattern })}`,
    { method: "DELETE" },
  )
}

/** Whether `pattern` matches `topic`: `*` is one segment, a trailing `>` one or more. */
export function topicMatches(pattern: string, topic: string): boolean {
  const want = pattern.split(".")
  const have = topic.split(".")
  for (let i = 0; i < want.length; i++) {
    if (want[i] === ">") return have.length > i
    if (i >= have.length) return false
    if (want[i] !== "*" && want[i] !== have[i]) return false
  }
  return want.length === have.length
}

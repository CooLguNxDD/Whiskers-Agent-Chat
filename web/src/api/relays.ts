import { api, queryString } from "./client"
import type { AuditRow, Relay, RelayBinding, RelayConfig, RelayPlatform, RelayRoute, RelaySnapshot, RouteFormat } from "./types"

/** Relay names: short lowercase. Mirrors `relays.NAME_RE` on the hub. */
export const RELAY_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,39}$/
/** Platform channel, thread or user ids. Mirrors `relays.EXTERNAL_ID_RE`. */
export const EXTERNAL_ID_RE = /^-?[A-Za-z0-9][A-Za-z0-9_:.-]{0,63}$/

/** Comma or whitespace separated ids, trimmed and de-duplicated. */
export function parseIdList(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).map((part) => part.trim()).filter(Boolean))]
}

/** Whether every id in a list is a valid platform id. */
export function validIds(ids: readonly string[]): boolean {
  return ids.every((id) => EXTERNAL_ID_RE.test(id))
}

/** What a route delivers, in a few words: its topics, kinds and channel filters. */
export function describeRoute(route: RelayRoute): string {
  const consumer = route.consumer
  if (!consumer) return "consumer missing"
  const parts = [...consumer.topics, ...consumer.kinds]
  if (consumer.channels.length) parts.push(consumer.channels.map((name) => `#${name}`).join(" "))
  if (consumer.task_ids.length) parts.push(consumer.task_ids.map((id) => `task #${id}`).join(" "))
  return parts.length ? parts.join(", ") : "every event"
}

/** Audit rows that belong to one relay: the relay itself, its bindings and its routes. */
export function relayAudit(rows: readonly AuditRow[], name: string): AuditRow[] {
  return rows.filter((row) => row.target === name || row.target.startsWith(`${name}/`))
}

export function listRelays(signal?: AbortSignal) {
  return api<{ relays: Relay[] }>("/api/v1/relays", { signal })
}

export function getRelay(name: string, signal?: AbortSignal) {
  return api<RelaySnapshot>(`/api/v1/relays/${encodeURIComponent(name)}`, { signal })
}

export function createRelay(input: { actor: string; name: string; platform: RelayPlatform; description?: string }) {
  return api<{ relay: Relay; token: string }>("/api/v1/relays", { method: "POST", body: JSON.stringify(input) })
}

/** PATCH with `If-Match`: the version the operator reviewed; a stale one answers 412. */
export function updateRelay(
  name: string,
  version: number,
  changes: { actor: string; enabled?: boolean; description?: string | null; config?: Partial<RelayConfig> },
) {
  return api<RelaySnapshot>(`/api/v1/relays/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "If-Match": String(version) },
    body: JSON.stringify(changes),
  })
}

export function deleteRelay(name: string, version: number, actor: string) {
  return api<{ deleted: boolean }>(`/api/v1/relays/${encodeURIComponent(name)}${queryString({ actor })}`, {
    method: "DELETE",
    headers: { "If-Match": String(version) },
  })
}

export function rotateRelayToken(name: string, actor: string) {
  return api<{ relay: Relay; token: string }>(`/api/v1/relays/${encodeURIComponent(name)}/rotate-token`, {
    method: "POST",
    body: JSON.stringify({ actor }),
  })
}

export function addBinding(
  name: string,
  input: { actor: string; external_id: string; hub_channel: string; allowed?: string[]; any_channel?: boolean; direction?: string },
) {
  return api<{ binding: RelayBinding; version: number }>(`/api/v1/relays/${encodeURIComponent(name)}/bindings`, {
    method: "POST",
    body: JSON.stringify(input),
  })
}

export function deleteBinding(name: string, id: number, actor: string) {
  return api<{ deleted: boolean }>(`/api/v1/relays/${encodeURIComponent(name)}/bindings/${id}${queryString({ actor })}`, {
    method: "DELETE",
  })
}

export function addRoute(
  name: string,
  input: { actor: string; external_id: string; format: RouteFormat; topics?: string[]; kinds?: string[]; channels?: string[] },
) {
  return api<{ route: RelayRoute; version: number }>(`/api/v1/relays/${encodeURIComponent(name)}/routes`, {
    method: "POST",
    body: JSON.stringify(input),
  })
}

export function updateRoute(name: string, id: number, changes: { actor: string; enabled?: boolean; format?: RouteFormat }) {
  return api<{ route: RelayRoute; version: number }>(`/api/v1/relays/${encodeURIComponent(name)}/routes/${id}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  })
}

export function deleteRoute(name: string, id: number, actor: string) {
  return api<{ deleted: boolean }>(`/api/v1/relays/${encodeURIComponent(name)}/routes/${id}${queryString({ actor })}`, {
    method: "DELETE",
  })
}

/**
 * Replay a route from an event id: events after it are posted to its platform channel again.
 * Audited on the hub. Route consumers refuse the plain consumer seek, so this is the only way.
 */
export function seekRoute(name: string, routeId: number, afterEventId: number, actor: string) {
  return api<{ cursor: number; previous: number }>(
    `/api/v1/relays/${encodeURIComponent(name)}/routes/${routeId}/seek`,
    { method: "POST", body: JSON.stringify({ actor, after_event_id: afterEventId }) },
  )
}

export function listAudit(opts: { targetKind?: string; limit?: number } = {}, signal?: AbortSignal) {
  return api<{ audit: AuditRow[]; next_before_id: number | null }>(
    `/api/v1/audit${queryString({ target_kind: opts.targetKind, limit: opts.limit })}`,
    { signal },
  )
}

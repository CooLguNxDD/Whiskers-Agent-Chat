import { api, queryString } from "./client"
import type { Mail, MailCounts, MailFilter, MailPage, MailUpdate } from "./types"

/**
 * The hub does not bind a mailbox to its caller yet: any token holder can read
 * any handle's mail. `X-Agent` names the acting handle so the client already
 * sends it once the hub checks it.
 */
function agentPath(agent: string, suffix = "") {
  return `/api/v1/mailbox/${encodeURIComponent(agent)}${suffix}`
}

function agentHeaders(agent: string): HeadersInit {
  return { "X-Agent": agent }
}

/** `agent`'s mail after `since_id`, oldest first. Page with the returned `cursor`. */
export function listMail(
  agent: string,
  opts: { state?: MailFilter; since_id?: number; limit?: number } = {},
  signal?: AbortSignal,
) {
  const path = agentPath(agent) + queryString({ state: opts.state, since_id: opts.since_id, limit: opts.limit })
  return api<MailPage>(path, { signal, headers: agentHeaders(agent) })
}

export function mailCounts(agent: string) {
  return api<MailCounts>(agentPath(agent, "/counts"), { headers: agentHeaders(agent) })
}

export function sendMail(input: {
  sender: string
  recipient: string
  body: string
  subject?: string
  reply_to?: number | null
  client_request_id: string
}) {
  const body: Record<string, unknown> = {
    sender: input.sender,
    recipient: input.recipient,
    body: input.body,
    client_request_id: input.client_request_id,
  }
  if (input.subject) body.subject = input.subject
  if (input.reply_to != null) body.reply_to = input.reply_to
  return api<{ mail: Mail }>("/api/v1/mailbox", {
    method: "POST",
    headers: agentHeaders(input.sender),
    body: JSON.stringify(body),
  })
}

/** Mark `ids` read, or everything up to `up_to_id`. Exactly one of the two. */
export function markMailRead(agent: string, target: { ids: number[] } | { up_to_id: number }) {
  return api<MailUpdate>(agentPath(agent, "/read"), {
    method: "POST",
    headers: agentHeaders(agent),
    body: JSON.stringify(target),
  })
}

/** Acknowledge `ids`. Ack implies read. */
export function ackMail(agent: string, ids: number[]) {
  return api<MailUpdate>(agentPath(agent, "/ack"), {
    method: "POST",
    headers: agentHeaders(agent),
    body: JSON.stringify({ ids }),
  })
}

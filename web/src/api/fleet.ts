import { api } from "./client"
import type { AgentActivity, Channel, ChannelState, FleetMessage, FleetTask, MessagePage, TaskStatus } from "./types"

/** Every channel, archived ones included. The list view decides what to show. */
export function listChannels() {
  return api<{ channels: Channel[] }>("/api/v1/channels?include_archived=1")
}

/** Set the lifecycle state. "archived" is refused with channel_has_open_tasks unless force. */
export function setChannelState(input: {
  name: string
  state: ChannelState
  actor: string
  note?: string
  force?: boolean
}) {
  return api<{
    channel: Channel
    previous_state: ChannelState
    cancelled_tasks: number[]
    idempotent: boolean
  }>(`/api/v1/channels/${encodeURIComponent(input.name)}/state`, {
    method: "POST",
    body: JSON.stringify({
      state: input.state,
      actor: input.actor,
      note: input.note ?? "",
      force: input.force ?? false,
    }),
  })
}

export function createChannel(name: string, topic: string) {
  return api<{ channel: Channel }>("/api/v1/channels", {
    method: "POST",
    body: JSON.stringify({ name, topic }),
  })
}

export function getMessages(
  channel: string,
  opts: { since_id?: number; before_id?: number; limit?: number } = {},
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({ channel })
  if (opts.since_id != null) params.set("since_id", String(opts.since_id))
  if (opts.before_id != null) params.set("before_id", String(opts.before_id))
  if (opts.limit != null) params.set("limit", String(opts.limit))
  return api<MessagePage>(`/api/v1/messages?${params}`, { signal })
}

export function postMessage(input: {
  channel: string
  author: string
  text: string
  reply_to?: number | null
  client_request_id: string
}) {
  return api<{ message: FleetMessage }>("/api/v1/messages", {
    method: "POST",
    body: JSON.stringify(input),
  })
}

export function listTasks(channel: string) {
  const params = new URLSearchParams({ channel })
  return api<{ tasks: FleetTask[] }>(`/api/v1/tasks?${params}`)
}

export function createTask(input: {
  channel: string
  title: string
  description: string
  assignee?: string
  actor: string
  client_request_id: string
}) {
  return api<{ task: FleetTask }>("/api/v1/tasks", {
    method: "POST",
    body: JSON.stringify(input),
  })
}

export function claimTask(taskId: number, actor: string, expectedVersion: number) {
  return api<{ task: FleetTask }>(`/api/v1/tasks/${taskId}/claim`, {
    method: "POST",
    body: JSON.stringify({ actor, expected_version: expectedVersion }),
  })
}

export function updateTaskStatus(
  taskId: number,
  status: TaskStatus,
  actor: string,
  expectedVersion: number,
) {
  return api<{ task: FleetTask }>(`/api/v1/tasks/${taskId}/status`, {
    method: "POST",
    body: JSON.stringify({ status, actor, expected_version: expectedVersion }),
  })
}

export function listAgents() {
  return api<{ agents: AgentActivity[]; label: string }>("/api/v1/agents")
}

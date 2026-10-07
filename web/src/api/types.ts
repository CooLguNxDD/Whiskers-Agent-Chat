export interface FleetMessage {
  id: number
  channel: string
  channel_id?: number
  author: string
  text: string
  reply_to: number | null
  created_at: string
  mentions: string[]
  /** Metadata only. The bytes live in the Whiskers MinIO bucket. */
  attachments?: Attachment[]
  /** Where the message came from. Set by the Discord relay; null for plain messages. */
  origin?: MessageOrigin | null
  /** The outbound webhook (a Discord channel) the message is addressed to, if any. */
  destination?: string | null
}

export interface MessageOrigin {
  source: "discord"
  /** A Discord id. Kept as a string: ids overflow JS numbers. */
  channel_id: string
  message_id?: string
  author?: string
}

export interface Attachment {
  id: number
  message_id: number
  filename: string
  content_type: string
  size_bytes: number
  storage: string
  bucket: string
  object_key: string
  sha256: string
  created_at: string
}

/** Channel lifecycle, in display order. Only "archived" changes behavior (read-only). */
export const CHANNEL_STATES = ["active", "paused", "blocked", "review", "done", "archived"] as const
export type ChannelState = (typeof CHANNEL_STATES)[number]

export interface Channel {
  id: number
  name: string
  topic: string
  created_at: string
  state: ChannelState
  state_note: string
  state_updated_at: string | null
  state_updated_by: string | null
  /** Set while the channel is archived (read-only); mirrors state === "archived". */
  archived_at: string | null
  archived_by: string | null
}

export interface TaskEvent {
  id: number
  task_id: number
  actor: string
  from_status: string | null
  to_status: string
  note: string
  created_at: string
}

export interface FleetTask {
  id: number
  channel: string
  title: string
  description: string
  status: TaskStatus
  assignee: string | null
  version: number
  created_at: string
  updated_at: string
  events: TaskEvent[]
}

export type TaskStatus =
  | "open"
  | "claimed"
  | "in_progress"
  | "blocked"
  | "done"
  | "cancelled"

export interface AgentActivity {
  name: string
  last_activity: string
}

export interface MessagePage {
  messages: FleetMessage[]
  cursor: number
  next_before_id: number | null
  has_more: boolean
  event_cursor: number
}

/** Event kinds a hook can filter on. Mirrors validate.EVENT_KINDS on the hub. */
export const EVENT_KINDS = [
  "message.created",
  "task.updated",
  "channel.created",
  "channel.archived",
  "channel.unarchived",
  "channel.state_changed",
] as const
export type EventKind = (typeof EVENT_KINDS)[number]

export type WebhookDirection = "out" | "in"
export type WebhookFormat = "discord" | "slack" | "generic"

/**
 * One webhook as the hub returns it. The secret is never part of this: it is
 * shown once, in the create or rotate response (see WebhookSecret).
 */
export interface Webhook {
  id: number
  name: string
  direction: WebhookDirection
  enabled: boolean
  created_at: string
  updated_at: string
  // Outbound only.
  url?: string
  format?: WebhookFormat
  kinds?: EventKind[]
  channels?: string[]
  mentions?: string[]
  exclude_authors?: string[]
  cursor?: number
  failure_count?: number
  last_status?: string | null
  last_error?: string | null
  last_delivery_at?: string | null
  /** What agents are told this destination is for. */
  description?: string | null
  /** Receives nothing except messages an agent addresses to it. */
  directed_only?: boolean
  /** The Discord channel this hook posts to, so replies from that channel can return to it. */
  discord_channel_id?: string | null
  // Inbound only.
  channel?: string
  author?: string
  allow_override?: boolean
}

export interface WebhookSecret {
  webhook: Webhook
  secret: string
}

export interface WebhookTestResult {
  ok: boolean
  status: number | null
  error: string | null
}

export interface ApiErrorBody {
  code: string
  message: string
  details?: Record<string, unknown>
}

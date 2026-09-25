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

export interface ApiErrorBody {
  code: string
  message: string
  details?: Record<string, unknown>
}

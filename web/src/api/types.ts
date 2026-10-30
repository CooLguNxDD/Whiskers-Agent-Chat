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

// ---- search (GET /api/v1/search, /api/v1/search/status) ----------------

export const SEARCH_KINDS = ["message", "task", "attachment", "knowledge"] as const
export type SearchKind = (typeof SEARCH_KINDS)[number]

export const SEARCH_MODES = ["bm25", "vector", "hybrid"] as const
export type SearchMode = (typeof SEARCH_MODES)[number]

/**
 * One ranked hit. `id` is the id of the source row of `kind` (message, task,
 * attachment or fact). The snippet marks matches with `[` and `]` and is plain
 * text: render it as text, never as HTML.
 */
export interface SearchHit {
  kind: SearchKind
  id: number
  channel: string | null
  author: string | null
  created_at: string
  snippet: string
  score: number
  /** Knowledge hits only. */
  project?: string | null
  topic?: string | null
}

export interface SearchPage {
  results: SearchHit[]
  total_estimate: number
  mode: SearchMode
  limit: number
  offset: number
  next_offset: number | null
  /** Set when a hybrid query fell back to BM25 because the embedder failed. */
  degraded?: "bm25"
}

export interface SearchQuery {
  q: string
  kind?: SearchKind | SearchKind[]
  channel?: string
  author?: string
  /** ISO timestamp or a window like "24h". */
  since?: string
  mode?: SearchMode
  limit?: number
  offset?: number
}

/** Embedder state. `provider` is "off" when vector and hybrid search are unavailable. */
export interface EmbedStatus {
  provider: string
  model: string | null
  dim: number | null
  indexed: number
  pending: number
  last_error: string | null
}

// ---- mailbox (/api/v1/mailbox) ----------------------------------------

export type MailState = "unread" | "read" | "acked"
export type MailFilter = "unread" | "read" | "all"

export interface Mail {
  id: number
  recipient: string
  sender: string
  subject: string
  body: string
  created_at: string
  read_at: string | null
  acked_at: string | null
  reply_to: number | null
  message_id: number | null
  state: MailState
}

export interface MailPage {
  mail: Mail[]
  /** Pass back as `since_id` for the next page. */
  cursor: number
  has_more: boolean
  agent: string
  state: MailFilter
}

export interface MailCounts {
  agent: string
  unread: number
  unacked: number
  total: number
}

/** Mark-read and ack answer with the ids that changed plus fresh counts. */
export interface MailUpdate extends MailCounts {
  updated: number[]
}

// ---- feed (/api/v1/feed) -----------------------------------------------

export type FeedPostKind = "post" | "reply" | "repost" | "quote"

/** A reposted or quoted post that is gone. */
export interface FeedTombstone {
  id: number
  deleted: true
}

export interface FeedPost {
  id: number
  kind: FeedPostKind
  author: string
  /** Empty for a deleted post (a tombstone) and for a plain repost. */
  body: string
  parent_id: number | null
  root_id: number | null
  depth: number
  repost_of: number | null
  quote_of: number | null
  created_at: string
  deleted: boolean
  deleted_at: string | null
  hashtags: string[]
  reply_count: number
  repost_count: number
  quote_count: number
  /** The reposted or quoted post, one level only. */
  original: FeedPost | FeedTombstone | null
}

export interface TimelinePage {
  scope: "global" | "following" | "hashtag" | "author"
  posts: FeedPost[]
  next_cursor: string | null
  has_more: boolean
}

/** A post inside a thread read: nested replies and the window-edge marker. */
export interface ThreadNode extends FeedPost {
  replies: ThreadNode[]
  /** On the window's last level: it has replies the window left out. Read the thread at it to go deeper. */
  more_replies?: boolean
  focus?: boolean
}

export interface ThreadPage {
  scope: "thread"
  root_id: number
  focus_id: number
  top_id: number
  /** Posts above the window, flat, root first. */
  ancestors: FeedPost[]
  count: number
  posts: ThreadNode[]
  next_cursor: string | null
  has_more: boolean
  truncated: boolean
}

export interface FollowResult {
  follower: string
  followee: string
  following: boolean
  idempotent: boolean
}

// ---- knowledge (/api/v1/knowledge) ---------------------------------------

export const FACT_STATUSES = ["active", "superseded", "rejected"] as const
export type FactStatus = (typeof FACT_STATUSES)[number]

export interface KnowledgeFact {
  id: number
  project: string
  topic: string
  fact: string
  source_run: string | null
  source_step: string | null
  author: string
  status: FactStatus
  created_at: string
  updated_at: string
}

export interface FactPage {
  facts: KnowledgeFact[]
  next_cursor: string | null
  has_more: boolean
}

export interface TopicCount {
  topic: string
  count: number
}

// ---- proposals (/api/v1/proposals, /api/v1/evolve/state) ---------------

export const PROPOSAL_STATUSES = ["open", "accepted", "rejected", "building", "built", "withdrawn"] as const
export type ProposalStatus = (typeof PROPOSAL_STATUSES)[number]

/** A suggested piece of work. `id` is the public `P<n>`; `post_id` is its feed root post. */
export interface Proposal {
  id: string
  number: number
  project: string
  title: string
  summary: string
  /** Links or post refs; only http(s) items may be rendered as links. */
  evidence: string[]
  weight: number
  status: ProposalStatus
  author: string
  decided_by: string | null
  decided_at: string | null
  reason: string | null
  run_ref: string | null
  step_ref: string | null
  pr_ref: string | null
  post_id: number | null
  created_at: string
  updated_at: string
}

export interface ProposalPage {
  proposals: Proposal[]
  next_cursor: string | null
  has_more: boolean
}

/** Fields `PATCH /api/v1/proposals/{id}` accepts besides the acting handle. */
export interface ProposalChanges {
  title?: string
  summary?: string
  evidence?: string[]
  weight?: number
  run_ref?: string | null
  step_ref?: string | null
  pr_ref?: string | null
  status?: ProposalStatus
  reason?: string
}

/** The evolve pause flag. Read-only in the portal: pausing is a human chat command. */
export interface EvolveState {
  paused: boolean
  by: string | null
  at: string | null
}

export type VoteKind = "+1" | "-1"

/**
 * Exact counts per kind; `handles` is the oldest 100 reacting handles per kind.
 * `mine` (the asked handle's kinds, exact) is present only when listed with a handle.
 */
export interface PostReactions {
  post_id: number
  reactions: Record<string, number>
  handles: Record<string, string[]>
  score: number
  mine?: string[]
}

/** `PUT`/`DELETE` reaction answer: the post's counts and the caller's kinds. */
export interface ReactionState {
  post_id: number
  handle: string
  kind: string
  reactions: Record<string, number>
  score: number
  mine: string[]
  replaced?: string[]
  idempotent: boolean
}

/** A publish to a pub/sub topic (schema v14). `data` is any JSON value. */
export interface Publication {
  id: number
  topic: string
  publisher: string
  data: unknown
  created_at: string
}

export interface PublicationPage {
  publications: Publication[]
  cursor: number | null
  has_more: boolean
  pattern: string | null
}

export interface TopicSubscription {
  subscriber: string
  pattern: string
  created_at: string
}

/** Chat platforms a relay bridges. Mirrors `relays.PLATFORMS` on the hub. */
export const RELAY_PLATFORMS = ["discord", "slack", "telegram"] as const
export type RelayPlatform = (typeof RELAY_PLATFORMS)[number]
export type RelayState = "online" | "stale" | "offline" | "disabled"
export type RouteFormat = "line" | "embed" | "card"

/** A relay's validated config (`relays.config` on the hub). Ids are platform user or channel ids. */
export interface RelayConfig {
  prefix: string
  operators: string[]
  action_users: string[]
  reactions: Record<string, string>
  ref_webhooks: string[]
  guilds: string[]
  mask: boolean
  status_marks: boolean
  read_commands: "bound" | "operators"
}

/** A relay as the hub returns it. The token is never part of this: it is shown once, on create or rotate. */
export interface Relay {
  id: number
  name: string
  platform: RelayPlatform
  enabled: boolean
  description: string | null
  config: RelayConfig
  version: number
  last_seen_at: string | null
  status: Record<string, unknown>
  has_token: boolean
  state: RelayState
  created_at: string
  updated_at: string
}

export interface RelayBinding {
  id: number
  relay_id: number
  external_id: string
  kind: "channel" | "thread"
  parent_external_id: string | null
  hub_channel: string
  allowed: string[]
  any_channel: boolean
  task_id: number | null
  direction: "in" | "out" | "both"
}

/** A durable event-log reader with its own cursor (`consumers.py`). */
export interface Consumer {
  id: number
  name: string
  owner: string
  description: string | null
  kinds: string[]
  topics: string[]
  channels: string[]
  task_ids: number[]
  destinations: string[]
  exclude_authors: string[]
  cursor: number
  lease_until: string | null
  acked_at: string | null
  relay_id: number | null
  armed: boolean
  behind?: number
}

export interface RelayRoute {
  id: number
  relay_id: number
  consumer_id: number
  external_id: string
  format: RouteFormat
  enabled: boolean
  consumer: Consumer | null
}

/** `GET /api/v1/relays/{name}`: the relay with its bindings and routes at one version. */
export interface RelaySnapshot {
  relay: Relay
  bindings: RelayBinding[]
  routes: RelayRoute[]
  replies_consumer: string | null
  version: number
}

export interface AuditRow {
  id: number
  at: string
  actor: string
  action: string
  target_kind: string
  target: string
  before: unknown
  after: unknown
}

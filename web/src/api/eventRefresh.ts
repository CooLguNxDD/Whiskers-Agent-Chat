import type { SseEvent } from "./stream"

export type RefreshKey = "messages" | "channels" | "tasks" | "agents" | "mail" | "feed" | "knowledge" | "proposals" | "pubsub"

export const REFRESH_KEYS: readonly RefreshKey[] = ["messages", "channels", "tasks", "agents", "mail", "feed", "knowledge", "proposals", "pubsub"]

/** Bounded event bookkeeping: only query families, never message payloads. */
export class EventRefresh {
  cursor = 0
  private dirty = new Set<RefreshKey>()

  get pending() { return this.dirty.size > 0 }

  /** Accept stream events only. History snapshot cursors must never enter here. */
  accept(event: SseEvent) {
    if (event.event === "reset") {
      this.cursor = 0
      this.dirty = new Set(REFRESH_KEYS)
      return
    }
    const id = Number(event.id)
    if (!Number.isSafeInteger(id) || id <= 0) throw new Error("Invalid SSE event ID")
    if (id <= this.cursor) return
    if (event.event === "message.created") {
      this.dirty.add("messages")
      this.dirty.add("agents")
    } else if (event.event === "channel.created") {
      this.dirty.add("channels")
    } else if (event.event === "channel.state_changed") {
      this.dirty.add("channels")
    } else if (event.event === "channel.archived" || event.event === "channel.unarchived") {
      // A forced archive also cancels the channel's open tasks.
      this.dirty.add("channels")
      this.dirty.add("tasks")
    } else if (event.event === "task.updated") {
      this.dirty.add("tasks")
      this.dirty.add("agents")
    } else if (event.event === "mail") {
      // Unread counts and inbox lists. The payload never carries the body.
      this.dirty.add("mail")
    } else if (event.event === "feed_post" || event.event === "feed_repost" || event.event === "feed_follow") {
      this.dirty.add("feed")
    } else if (event.event === "knowledge_fact") {
      this.dirty.add("knowledge")
    } else if (
      event.event === "proposal_created" ||
      event.event === "proposal_updated" ||
      event.event === "reaction_added" ||
      event.event === "reaction_removed" ||
      event.event === "evolve_paused" ||
      event.event === "evolve_resumed"
    ) {
      // The proposals board: cards, vote counts and the paused banner.
      this.dirty.add("proposals")
    } else if (event.event === "topic.published" || event.event === "topic.subscribed" || event.event === "topic.unsubscribed") {
      // The Pub/Sub page: publication history and the subscription table.
      this.dirty.add("pubsub")
    }
    this.cursor = id
  }

  /** Transfer pending refreshes to the query client before discarding them. */
  flush(invalidate: (key: RefreshKey) => void) {
    for (const key of this.dirty) invalidate(key)
    this.dirty.clear()
  }
}

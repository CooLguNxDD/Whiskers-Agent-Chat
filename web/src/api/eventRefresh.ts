import type { SseEvent } from "./stream"

export type RefreshKey = "messages" | "channels" | "tasks" | "agents"

/** Bounded event bookkeeping: only query families, never message payloads. */
export class EventRefresh {
  cursor = 0
  private dirty = new Set<RefreshKey>()

  get pending() { return this.dirty.size > 0 }

  /** Accept stream events only. History snapshot cursors must never enter here. */
  accept(event: SseEvent) {
    if (event.event === "reset") {
      this.cursor = 0
      this.dirty = new Set(["messages", "channels", "tasks", "agents"])
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
    }
    this.cursor = id
  }

  /** Transfer pending refreshes to the query client before discarding them. */
  flush(invalidate: (key: RefreshKey) => void) {
    for (const key of this.dirty) invalidate(key)
    this.dirty.clear()
  }
}

import { describe, expect, it } from "vitest"
import { EventRefresh } from "./eventRefresh"

describe("event refresh bookkeeping", () => {
  it("bounds large replays to four refreshes, including on the tasks page", () => {
    const refresh = new EventRefresh()
    const kinds = ["message.created", "channel.created", "task.updated"]
    for (let id = 1; id <= 50_000; id++) {
      refresh.accept({ id: String(id), event: kinds[id % 3], data: "{}" })
    }
    const keys: string[] = []
    refresh.flush((key) => keys.push(key))
    expect(keys.sort()).toEqual(["agents", "channels", "messages", "tasks"])
    expect(refresh.cursor).toBe(50_000)
    expect(refresh.pending).toBe(false)
  })

  it("refreshes channels and tasks when a channel is archived or reopened", () => {
    for (const kind of ["channel.archived", "channel.unarchived"]) {
      const refresh = new EventRefresh()
      refresh.accept({ id: "1", event: kind, data: "{}" })
      const keys: string[] = []
      refresh.flush((key) => keys.push(key))
      expect(keys.sort()).toEqual(["channels", "tasks"])
    }
  })

  it("refreshes channels when a channel changes state", () => {
    const refresh = new EventRefresh()
    refresh.accept({ id: "1", event: "channel.state_changed", data: "{}" })
    const keys: string[] = []
    refresh.flush((key) => keys.push(key))
    expect(keys).toEqual(["channels"])
  })

  it("replays all query families across disconnect and preserves later events", () => {
    const refresh = new EventRefresh()
    refresh.accept({ id: "5", event: "message.created", data: "{}" })
    refresh.flush(() => undefined)
    // A channel snapshot's event_cursor is not accepted by this stream-only API.
    expect(refresh.cursor).toBe(5)
    refresh.accept({ id: "6", event: "task.updated", data: "{}" })
    const first: string[] = []
    refresh.flush((key) => first.push(key))
    refresh.accept({ id: "7", event: "message.created", data: "{}" })
    const second: string[] = []
    refresh.flush((key) => second.push(key))
    expect(first).toContain("tasks")
    expect(second).toContain("messages")
    expect(refresh.cursor).toBe(7)
    refresh.accept({ id: "6", event: "task.updated", data: "{}" })
    expect(refresh.pending).toBe(false)
  })
})

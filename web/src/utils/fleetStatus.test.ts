import { describe, expect, it } from "vitest"
import type { FleetTask } from "@/api/types"
import { activeTasks, fleetMembers, relativeTime } from "./fleetStatus"

const now = new Date("2026-11-04T12:00:00Z").getTime()
function task(id: number, assignee: string | null, status: FleetTask["status"], updated_at = "2026-11-04T11:00:00Z"): FleetTask {
  return { id, channel: "fleet", title: `t${id}`, description: "", status, assignee, version: 1, created_at: updated_at, updated_at, events: [] }
}

describe("fleetMembers", () => {
  it("derives working, blocked, idle and away and sorts by urgency", () => {
    const members = fleetMembers(
      [
        { name: "away-cat", last_activity: "2026-11-04T09:00:00Z" },
        { name: "idle-cat", last_activity: "2026-11-04T11:50:00Z" },
        { name: "blocked-cat", last_activity: "2026-11-04T08:00:00Z" },
        { name: "busy-cat", last_activity: "2026-11-04T08:00:00Z" },
      ],
      [task(1, "busy-cat", "claimed"), task(2, "busy-cat", "in_progress"), task(3, "blocked-cat", "blocked"), task(4, "away-cat", "done")],
      now,
    )
    expect(members.map((member) => [member.name, member.state, member.task?.id])).toEqual([
      ["busy-cat", "working", 2],
      ["blocked-cat", "blocked", 3],
      ["idle-cat", "idle", undefined],
      ["away-cat", "away", undefined],
    ])
  })

  it("lists live tasks and formats relative times", () => {
    expect(activeTasks([task(1, null, "open"), task(2, "a", "in_progress"), task(3, "b", "done")]).map((t) => t.id)).toEqual([2])
    expect(relativeTime("2026-11-04T11:55:00Z", now)).toBe("5m ago")
    expect(relativeTime("2026-11-04T09:00:00Z", now)).toBe("3h ago")
    expect(relativeTime("bad", now)).toBe("")
  })
})

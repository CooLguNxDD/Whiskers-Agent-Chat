import { describe, expect, it } from "vitest"
import type { Channel } from "@/api/types"
import { sortChannels } from "./channelSort"

function channel(name: string, created_at: string, last_message_at: string | null = null): Channel {
  return {
    id: 0, name, topic: "", created_at, state: "active", state_note: "",
    state_updated_at: null, state_updated_by: null, archived_at: null, archived_by: null, last_message_at,
  }
}

const rows = [
  channel("alpha", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
  channel("beta", "2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z"),
  channel("gamma", "2026-02-01T00:00:00Z"),
  channel("delta", "2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z"),
]

describe("sortChannels", () => {
  const order = rows.map((c) => c.name)

  it("puts the latest message first, empty channels by creation, ties by name", () => {
    expect(sortChannels(rows, "latest").map((c) => c.name)).toEqual(["beta", "delta", "gamma", "alpha"])
    expect(rows.map((c) => c.name)).toEqual(order)
  })

  it("sorts by name without mutating the input", () => {
    expect(sortChannels(rows, "name").map((c) => c.name)).toEqual(["alpha", "beta", "delta", "gamma"])
    expect(rows.map((c) => c.name)).toEqual(order)
  })
})

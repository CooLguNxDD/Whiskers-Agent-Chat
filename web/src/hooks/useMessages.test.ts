import { describe, expect, it } from "vitest"
import { MESSAGE_WINDOW_LIMIT, mergeMessageWindow } from "./useMessages"
import type { FleetMessage } from "@/api/types"

function message(id: number, text = String(id)): FleetMessage {
  return { id, channel: "fleet", author: "ada", text, reply_to: null, created_at: "now", mentions: [] }
}

describe("bounded message windows", () => {
  it("deduplicates overlapping pages and keeps ascending order", () => {
    const result = mergeMessageWindow([message(2, "old"), message(4)], [message(1), message(2, "new"), message(3)], "older")
    expect(result.map((item) => item.id)).toEqual([1, 2, 3, 4])
    expect(result[1].text).toBe("new")
  })

  it("caps older-page prepends at the live edge", () => {
    const result = mergeMessageWindow(
      Array.from({ length: MESSAGE_WINDOW_LIMIT }, (_, index) => message(index + 501)),
      Array.from({ length: 50 }, (_, index) => message(index + 451)),
      "older",
    )
    expect(result).toHaveLength(MESSAGE_WINDOW_LIMIT)
    expect(result[0].id).toBe(501)
    expect(result.at(-1)?.id).toBe(1000)
  })

  it("caps newer-page appends at the history edge", () => {
    const result = mergeMessageWindow(
      Array.from({ length: MESSAGE_WINDOW_LIMIT }, (_, index) => message(index + 1)),
      Array.from({ length: 50 }, (_, index) => message(index + 501)),
      "newer",
    )
    expect(result).toHaveLength(MESSAGE_WINDOW_LIMIT)
    expect(result[0].id).toBe(51)
    expect(result.at(-1)?.id).toBe(550)
  })
})

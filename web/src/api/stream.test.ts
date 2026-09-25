import { describe, expect, it } from "vitest"
import { mergeMessages } from "./merge"
import { createSseParser } from "./stream"
import type { FleetMessage } from "./types"

function message(id: number, text: string): FleetMessage {
  return {
    id,
    channel: "fleet",
    author: "ada",
    text,
    reply_to: null,
    created_at: "2026-01-01T00:00:00Z",
    mentions: [],
  }
}

describe("sse parser", () => {
  it("preserves metadata at every possible CRLF chunk boundary", () => {
    const frame = 'id: 7\r\nevent: message.created\r\ndata: {}\r\ndata: more\r\n\r\n'
    for (let split = 0; split <= frame.length; split++) {
      const seen: unknown[] = []
      const parser = createSseParser((event) => seen.push(event))
      parser.push(frame.slice(0, split))
      parser.push(frame.slice(split))
      expect(seen).toEqual([{ id: "7", event: "message.created", data: "{}\nmore" }])
    }
    const seen: unknown[] = []
    const parser = createSseParser((event) => seen.push(event))
    for (const character of frame) parser.push(character)
    expect(seen).toEqual([{ id: "7", event: "message.created", data: "{}\nmore" }])
  })

  it("bounds incomplete frames", () => {
    const parser = createSseParser(() => undefined)
    expect(() => parser.push("data: " + "x".repeat(1_048_576))).toThrow("size limit")
  })
  it("reassembles frames split across chunks, crlf, and multiline data", () => {
    const seen: string[] = []
    const parser = createSseParser((event) => seen.push(`${event.event}:${event.data}`))
    parser.push("event: message.created\r\n")
    parser.push("data: {\"a\":1}\r\ndata: more\n\n: ping\n\n")
    parser.push("id: 4\nevent: task.updated\ndata: {}")
    parser.push("\n\n")
    expect(seen).toEqual(['message.created:{"a":1}\nmore', "task.updated:{}"])
  })
})

describe("mergeMessages", () => {
  it("keeps live rows newer than the snapshot and drops duplicates", () => {
    const history = [message(1, "a"), message(2, "b")]
    const live = [message(2, "b-live"), message(3, "c")]
    expect(mergeMessages(history, live).map((item) => item.text)).toEqual(["a", "b", "c"])
  })
})

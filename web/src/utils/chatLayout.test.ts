import { describe, expect, it } from "vitest"
import type { FleetMessage } from "@/api/types"
import { avatarHue, blockSegments, messageActions, richBlocks, buildTranscript, findMatches, initials, inlineSegments, isLong } from "./chatLayout"

function msg(id: number, author: string, created_at: string, extra: Partial<FleetMessage> = {}): FleetMessage {
  return { id, channel: "fleet", author, text: `m${id}`, reply_to: null, created_at, mentions: [], ...extra }
}

describe("buildTranscript", () => {
  const now = new Date("2026-11-04T12:00:00")
  it("groups one author's messages within five minutes", () => {
    const items = buildTranscript([
      msg(1, "cat-1", "2026-11-04T10:00:00"),
      msg(2, "cat-1", "2026-11-04T10:03:00"),
      msg(3, "cat-2", "2026-11-04T10:04:00"),
      msg(4, "cat-2", "2026-11-04T10:30:00"),
    ], now)
    const rows = items.filter((item) => item.kind === "message")
    expect(items[0]).toMatchObject({ kind: "day", label: "Today" })
    expect(rows.map((row) => row.kind === "message" && [row.first, row.last])).toEqual([
      [true, false], [false, true], [true, true], [true, true],
    ])
  })

  it("starts a new day and a new group at midnight, and a reply opens a group", () => {
    const items = buildTranscript([
      msg(1, "cat-1", "2026-11-03T23:59:00"),
      msg(2, "cat-1", "2026-11-04T00:01:00"),
      msg(3, "cat-1", "2026-11-04T00:02:00", { reply_to: 2 }),
    ], now)
    expect(items.map((item) => item.kind === "day" ? item.label : item.kind === "message" && item.first)).toEqual([
      "Yesterday", true, "Today", true, true,
    ])
  })
})

describe("avatars", () => {
  it("is stable and short", () => {
    expect(avatarHue("cat-1")).toBe(avatarHue("cat-1"))
    expect(initials("fleet-librarian")).toBe("FL")
    expect(initials("claude")).toBe("CL")
    expect(initials("")).toBe("?")
  })
})

describe("find and segments", () => {
  it("matches text and author case-insensitively", () => {
    const messages = [msg(1, "cat-1", "x", { text: "Deploy done" }), msg(2, "Reviewer", "x"), msg(3, "z", "x")]
    expect(findMatches(messages, "deploy")).toEqual([1])
    expect(findMatches(messages, "review")).toEqual([2])
    expect(findMatches(messages, "  ")).toEqual([])
  })

  it("splits code fences and keeps an unclosed fence as code", () => {
    expect(blockSegments("hi\n```ts\nconst a = 1\n```\nbye")).toEqual([
      { kind: "para", text: "hi" },
      { kind: "codeblock", lang: "ts", text: "const a = 1" },
      { kind: "para", text: "bye" },
    ])
    expect(blockSegments("```\nopen")).toEqual([{ kind: "codeblock", lang: "", text: "open" }])
  })

  it("finds inline code, http links, known mentions and highlights", () => {
    expect(inlineSegments("ask @cat-1 and @nobody `ls` https://x.dev/a javascript:alert(1)", ["cat-1"], "ask")).toEqual([
      { kind: "match", text: "ask" },
      { kind: "text", text: " " },
      { kind: "mention", text: "@cat-1" },
      { kind: "text", text: " and " },
      { kind: "text", text: "@nobody" },
      { kind: "text", text: " " },
      { kind: "code", text: "ls" },
      { kind: "text", text: " " },
      { kind: "link", text: "https://x.dev/a", href: "https://x.dev/a" },
      { kind: "text", text: " javascript:alert(1)" },
    ])
  })

  it("collapses long text", () => {
    expect(isLong("a\n".repeat(20))).toBe(true)
    expect(isLong("short")).toBe(false)
  })
})

describe("agent formatting", () => {
  it("turns status words into chips", () => {
    const segments = inlineSegments("devops.json PASS, hub OK, pod OOMKilled", [])
    expect(segments.filter((segment) => segment.kind === "status").map((segment) => segment.kind === "status" && segment.tone)).toEqual(["ok", "ok", "bad"])
  })

  it("lifts three or more key: value lines into a facts card", () => {
    expect(richBlocks("Summary\nRestarts: 3 in 2 hours\nLast exit: OOMKilled\nMemory limit: 128 MiB\nThanks")).toEqual([
      { kind: "para", text: "Summary" },
      { kind: "facts", rows: [
        { key: "Restarts", value: "3 in 2 hours" },
        { key: "Last exit", value: "OOMKilled" },
        { key: "Memory limit", value: "128 MiB" },
      ] },
      { kind: "para", text: "Thanks" },
    ])
    expect(richBlocks("Note: one\nTwo: two")).toEqual([{ kind: "para", text: "Note: one\nTwo: two" }])
  })

  it("only links explicit task and proposal references", () => {
    expect(messageActions("see task #12 and Task #12, proposal P3; usprod P2 is a priority")).toEqual([
      { kind: "task", id: 12 },
      { kind: "proposal", id: 3 },
    ])
  })
})

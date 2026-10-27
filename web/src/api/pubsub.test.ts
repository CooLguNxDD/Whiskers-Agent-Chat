import { describe, expect, it } from "vitest"
import { PATTERN_RE, TOPIC_RE, topicMatches } from "./pubsub"

describe("pub/sub topic rules mirror the hub", () => {
  it("matches * as one segment and a trailing > as the rest", () => {
    expect(topicMatches("ci.*.done", "ci.whiskers.done")).toBe(true)
    expect(topicMatches("ci.*.done", "ci.done")).toBe(false)
    expect(topicMatches("ci.>", "ci.whiskers.done")).toBe(true)
    expect(topicMatches("ci.>", "ci")).toBe(false)
    expect(topicMatches(">", "a.b")).toBe(true)
    expect(topicMatches("a.b", "a.b.c")).toBe(false)
  })

  it("validates topics and patterns", () => {
    for (const ok of ["ci", "ci.whiskers.done", "a-b.c_d"]) expect(TOPIC_RE.test(ok)).toBe(true)
    for (const bad of ["", "Ci", "a..b", "a.*", "a.>", "a/b"]) expect(TOPIC_RE.test(bad)).toBe(false)
    for (const ok of ["ci.*.done", "ci.>", ">", "*", "*.done"]) expect(PATTERN_RE.test(ok)).toBe(true)
    for (const bad of ["a.>.b", "a.**", "a.b>", "", ">.a"]) expect(PATTERN_RE.test(bad)).toBe(false)
  })
})

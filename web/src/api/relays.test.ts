import { afterEach, describe, expect, it, vi } from "vitest"
import { EventRefresh } from "./eventRefresh"
import { describeRoute, deleteRelay, EXTERNAL_ID_RE, parseIdList, RELAY_NAME_RE, relayAudit, updateRelay, validIds } from "./relays"
import type { AuditRow, RelayRoute } from "./types"

const route = (consumer: Partial<NonNullable<RelayRoute["consumer"]>> | null): RelayRoute => ({
  id: 1,
  relay_id: 1,
  consumer_id: 1,
  external_id: "111",
  format: "line",
  enabled: true,
  consumer: consumer && {
    id: 1, name: "relay-x-1", owner: "ann", description: null, kinds: [], topics: [], channels: [], task_ids: [],
    destinations: [], exclude_authors: [], cursor: 0, lease_until: null, acked_at: null, relay_id: 1, armed: false,
    ...consumer,
  },
})

const row = (target: string): AuditRow => ({ id: 1, at: "", actor: "ann", action: "x", target_kind: "relay", target, before: null, after: null })

afterEach(() => vi.unstubAllGlobals())

describe("relay helpers", () => {
  it("mirrors the hub's name and id grammar", () => {
    expect(RELAY_NAME_RE.test("main-bot")).toBe(true)
    expect(RELAY_NAME_RE.test("Main")).toBe(false)
    expect(EXTERNAL_ID_RE.test("-100123:7")).toBe(true)
    expect(EXTERNAL_ID_RE.test("C0123:1700.0001")).toBe(true)
    expect(EXTERNAL_ID_RE.test("has space")).toBe(false)
  })

  it("parses id lists and validates them", () => {
    expect(parseIdList(" 1, 2 2\n3,, ")).toEqual(["1", "2", "3"])
    expect(validIds(["1", "U2"])).toBe(true)
    expect(validIds(["bad id"])).toBe(false)
  })

  it("describes a route by its filters", () => {
    expect(describeRoute(route({ topics: ["ci.*.done"] }))).toBe("ci.*.done")
    expect(describeRoute(route({ kinds: ["task.updated"], task_ids: [4] }))).toBe("task.updated, task #4")
    expect(describeRoute(route({ channels: ["ops"] }))).toBe("#ops")
    expect(describeRoute(route({}))).toBe("every event")
    expect(describeRoute(route(null))).toBe("consumer missing")
  })

  it("keeps only one relay's audit rows", () => {
    const rows = [row("main"), row("main/111"), row("mainframe"), row("other/1")]
    expect(relayAudit(rows, "main").map((item) => item.target)).toEqual(["main", "main/111"])
  })

  it("sends the reviewed version as If-Match", async () => {
    const calls: { url: string; init: RequestInit }[] = []
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return new Response("{}", { status: 200 })
    }))
    await updateRelay("main", 4, { actor: "ann", enabled: false })
    await deleteRelay("main", 5, "ann")
    expect(new Headers(calls[0].init.headers).get("If-Match")).toBe("4")
    expect(calls[0].init.method).toBe("PATCH")
    expect(calls[1].url).toBe("/api/v1/relays/main?actor=ann")
    expect(new Headers(calls[1].init.headers).get("If-Match")).toBe("5")
  })

  it("refreshes the relay family on relay and consumer events", () => {
    for (const kind of ["relay.updated", "relay.created", "consumer.seeked"]) {
      const refresh = new EventRefresh()
      refresh.accept({ id: "1", event: kind, data: "{}" })
      const keys: string[] = []
      refresh.flush((key) => keys.push(key))
      expect(keys).toEqual(["relay"])
    }
  })
})

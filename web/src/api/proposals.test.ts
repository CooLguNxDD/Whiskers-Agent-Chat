import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError, setTokenGetter } from "./client"
import { getEvolveState, getProposal, listProposals, listReactions, react, unreact, updateProposal } from "./proposals"

interface Call {
  url: URL
  method: string
  headers: Headers
  body: unknown
}

let calls: Call[] = []
let next: () => Response = () => json({})

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

const last = () => calls[calls.length - 1]

beforeEach(() => {
  calls = []
  next = () => json({})
  setTokenGetter(() => "")
  vi.stubGlobal("fetch", vi.fn(async (input: string, init: RequestInit = {}) => {
    calls.push({
      url: new URL(input, "http://hub.test"),
      method: init.method ?? "GET",
      headers: new Headers(init.headers),
      body: typeof init.body === "string" ? JSON.parse(init.body) : undefined,
    })
    return next()
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("proposals client", () => {
  it("lists with only the set filters", async () => {
    await listProposals()
    expect(last().url.pathname).toBe("/api/v1/proposals")
    expect(last().url.search).toBe("")
    await listProposals({ project: "demo", status: "open", limit: 200, cursor: "v1:abc" })
    expect(last().method).toBe("GET")
    expect(Object.fromEntries(last().url.searchParams)).toEqual({ project: "demo", status: "open", limit: "200", cursor: "v1:abc" })
  })

  it("gets one proposal by public id", async () => {
    await getProposal("P7")
    expect(last().url.pathname).toBe("/api/v1/proposals/P7")
    expect(last().method).toBe("GET")
  })

  it("patches status and reason with the actor in the body", async () => {
    await updateProposal("P2", "alice", { status: "rejected", reason: "out of scope" })
    expect(last().url.pathname).toBe("/api/v1/proposals/P2")
    expect(last().method).toBe("PATCH")
    expect(last().body).toEqual({ status: "rejected", reason: "out of scope", actor: "alice" })
    expect(last().headers.get("Content-Type")).toBe("application/json")
  })

  it("reads the evolve state", async () => {
    next = () => json({ paused: true, by: "alice", at: "2026-10-16T10:00:00Z" })
    await expect(getEvolveState()).resolves.toEqual({ paused: true, by: "alice", at: "2026-10-16T10:00:00Z" })
    expect(last().url.pathname).toBe("/api/v1/evolve/state")
  })

  it("reacts with PUT and a body, unreacts with DELETE and a query handle", async () => {
    await react(11, "+1", "bot-1")
    expect(last().url.pathname).toBe("/api/v1/feed/posts/11/reactions/%2B1")
    expect(last().method).toBe("PUT")
    expect(last().body).toEqual({ handle: "bot-1" })
    await unreact(11, "-1", "bot-1")
    expect(last().url.pathname).toBe("/api/v1/feed/posts/11/reactions/-1")
    expect(last().url.searchParams.get("handle")).toBe("bot-1")
    expect(last().method).toBe("DELETE")
    expect(last().body).toBeUndefined()
    await listReactions(11)
    expect(last().url.pathname).toBe("/api/v1/feed/posts/11/reactions")
    expect(last().url.search).toBe("")
    await listReactions(11, "alice")
    expect(last().url.pathname).toBe("/api/v1/feed/posts/11/reactions")
    expect(last().url.searchParams.get("handle")).toBe("alice")
  })

  it("surfaces the hub's refusal text", async () => {
    next = () => json({ error: { code: "invalid_transition", message: "proposal is built, a final status; it cannot change" } }, 409)
    const failure = await updateProposal("P3", "alice", { status: "withdrawn" }).catch((error: unknown) => error)
    expect(failure).toBeInstanceOf(ApiError)
    expect((failure as ApiError).message).toMatch(/final status/)
    expect((failure as ApiError).code).toBe("invalid_transition")
  })
})

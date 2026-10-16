import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError, setTokenGetter } from "./client"
import { search, searchStatus } from "./search"
import { ackMail, listMail, mailCounts, markMailRead, sendMail } from "./mailbox"
import { createPost, follow, getThread, getTimeline, listFollowing, repost, unfollow } from "./feed"
import { addFact, exportFacts, listFacts, listProjects, listTopics, searchFacts, setFactStatus } from "./knowledge"
import { getAttachment } from "./fleet"
import { fetchAround } from "@/hooks/useMessages"

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
  setTokenGetter(() => "")
})

describe("search client", () => {
  it("sends only the set filters and the mode", async () => {
    await search({ q: "deploy fix", kind: "task", channel: "fleet", author: "ada", since: "24h", mode: "hybrid", limit: 10 })
    expect(last().url.pathname).toBe("/api/v1/search")
    expect(Object.fromEntries(last().url.searchParams)).toEqual({
      q: "deploy fix", kind: "task", channel: "fleet", author: "ada", since: "24h", mode: "hybrid", limit: "10",
    })
  })

  it("repeats kind for a list and drops empty values", async () => {
    await search({ q: "x", kind: ["message", "knowledge"], channel: "", author: undefined })
    expect(last().url.searchParams.getAll("kind")).toEqual(["message", "knowledge"])
    expect(last().url.searchParams.has("channel")).toBe(false)
    expect(last().url.searchParams.has("author")).toBe(false)
    expect(last().url.searchParams.has("mode")).toBe(false)
  })

  it("maps a hub error body to ApiError with code and details", async () => {
    next = () => json({ error: { code: "not_available", message: "search mode vector is not available", details: { mode: "vector" } } }, 400)
    const err = await search({ q: "x", mode: "vector" }).catch((caught: unknown) => caught)
    expect(err).toBeInstanceOf(ApiError)
    expect(err).toMatchObject({ status: 400, code: "not_available", message: "search mode vector is not available", details: { mode: "vector" } })
  })

  it("falls back to request_failed for a non-JSON error", async () => {
    next = () => new Response("bad gateway", { status: 502, statusText: "Bad Gateway" })
    const err = await searchStatus().catch((caught: unknown) => caught)
    expect(err).toMatchObject({ status: 502, code: "request_failed", message: "Bad Gateway" })
  })

  it("sends the in-memory bearer token", async () => {
    setTokenGetter(() => "synthetic-token")
    await searchStatus()
    expect(last().url.pathname).toBe("/api/v1/search/status")
    expect(last().headers.get("Authorization")).toBe("Bearer synthetic-token")
  })
})

describe("search hit sources", () => {
  const msg = (id: number) => ({ id, channel: "ops", author: "ada", text: `m${id}`, reply_to: null, created_at: "now", mentions: [] })

  it("loads the history around a message hit, the hit included", async () => {
    next = () => {
      const url = calls[calls.length - 1].url
      return url.searchParams.has("before_id")
        ? json({ messages: [msg(40), msg(41), msg(42)], has_more: true })
        : json({ messages: [msg(43), msg(44)], has_more: false })
    }
    const around = await fetchAround("ops", 42)
    const params = calls.map((call) => Object.fromEntries(call.url.searchParams))
    expect(params).toContainEqual({ channel: "ops", before_id: "43", limit: "25" })
    expect(params).toContainEqual({ channel: "ops", since_id: "42", limit: "25" })
    expect(around.messages.map((item) => item.id)).toEqual([40, 41, 42, 43, 44])
    expect(around).toMatchObject({ hasOlder: true, hasNewer: false })
  })

  it("resolves an attachment hit to its message", async () => {
    next = () => json({ attachment: { id: 7, message_id: 42, filename: "log.txt" } })
    const { attachment } = await getAttachment(7)
    expect(last().url.pathname).toBe("/api/v1/attachments/7")
    expect(attachment.message_id).toBe(42)
  })
})

describe("mailbox client", () => {
  it("lists one handle's mail with state and cursor", async () => {
    await listMail("ada-1", { state: "all", since_id: 40, limit: 25 })
    expect(last().url.pathname).toBe("/api/v1/mailbox/ada-1")
    expect(Object.fromEntries(last().url.searchParams)).toEqual({ state: "all", since_id: "40", limit: "25" })
    expect(last().headers.get("X-Agent")).toBe("ada-1")
  })

  it("reads counts, marks read and acks under the handle", async () => {
    await mailCounts("ada")
    expect(last().url.pathname).toBe("/api/v1/mailbox/ada/counts")
    await markMailRead("ada", { ids: [3, 4] })
    expect(last()).toMatchObject({ method: "POST", body: { ids: [3, 4] } })
    expect(last().url.pathname).toBe("/api/v1/mailbox/ada/read")
    await markMailRead("ada", { up_to_id: 9 })
    expect(last().body).toEqual({ up_to_id: 9 })
    await ackMail("ada", [5])
    expect(last().url.pathname).toBe("/api/v1/mailbox/ada/ack")
    expect(last().body).toEqual({ ids: [5] })
  })

  it("sends a direct message and omits an empty subject", async () => {
    await sendMail({ sender: "ada", recipient: "bob", body: "hi", subject: "", client_request_id: "r1" })
    expect(last()).toMatchObject({ method: "POST", body: { sender: "ada", recipient: "bob", body: "hi", client_request_id: "r1" } })
    expect(last().url.pathname).toBe("/api/v1/mailbox")
    expect(last().body).not.toHaveProperty("subject")
    await sendMail({ sender: "ada", recipient: "bob", body: "hi", subject: "Re: build", client_request_id: "r2" })
    expect(last().body).toMatchObject({ subject: "Re: build" })
  })

  it("surfaces forbidden as an ApiError", async () => {
    next = () => json({ error: { code: "forbidden", message: "only the recipient may read or ack a mail" } }, 403)
    await expect(ackMail("ada", [1])).rejects.toMatchObject({ status: 403, code: "forbidden" })
  })
})

describe("feed client", () => {
  it("builds global, following and hashtag timelines with cursors", async () => {
    await getTimeline({ scope: "global" }, { cursor: "abc", limit: 20 })
    expect(last().url.pathname).toBe("/api/v1/feed/timeline")
    expect(Object.fromEntries(last().url.searchParams)).toEqual({ scope: "global", cursor: "abc", limit: "20" })
    await getTimeline({ scope: "following", handle: "ada" })
    expect(Object.fromEntries(last().url.searchParams)).toEqual({ scope: "following", handle: "ada" })
    await getTimeline({ scope: "hashtag", tag: "deploy" })
    expect(Object.fromEntries(last().url.searchParams)).toEqual({ scope: "hashtag", tag: "deploy" })
  })

  it("reads a thread window and pages it", async () => {
    await getThread(12)
    expect(last().url.pathname).toBe("/api/v1/feed/posts/12")
    expect(Object.fromEntries(last().url.searchParams)).toEqual({ thread: "1" })
    await getThread(12, "next")
    expect(last().url.searchParams.get("cursor")).toBe("next")
  })

  it("posts, replies, quotes and reposts", async () => {
    await createPost({ author: "ada", body: "hello #ops", client_request_id: "p1" })
    expect(last().body).toEqual({ author: "ada", body: "hello #ops", client_request_id: "p1" })
    await createPost({ author: "ada", body: "agreed", parent_id: 4, client_request_id: "p2" })
    expect(last().body).toMatchObject({ parent_id: 4 })
    await createPost({ author: "ada", body: "see this", quote_of: 7, client_request_id: "p3" })
    expect(last().body).toMatchObject({ quote_of: 7 })
    await repost(7, "ada")
    expect(last()).toMatchObject({ method: "POST", body: { author: "ada" } })
    expect(last().url.pathname).toBe("/api/v1/feed/posts/7/repost")
  })

  it("follows with PUT and unfollows with DELETE", async () => {
    await follow("ada", "bob")
    expect(last()).toMatchObject({ method: "PUT", body: { follower: "ada" } })
    expect(last().url.pathname).toBe("/api/v1/feed/follows/bob")
    await unfollow("ada", "bob")
    expect(last().method).toBe("DELETE")
    await listFollowing("ada")
    expect(last()).toMatchObject({ method: "GET" })
    expect(last().url.pathname).toBe("/api/v1/feed/follows/ada")
  })

  it("maps thread_too_deep", async () => {
    next = () => json({ error: { code: "thread_too_deep", message: "replies nest at most 1000 levels deep", details: { max_depth: 1000 } } }, 409)
    await expect(createPost({ author: "ada", body: "x", parent_id: 1, client_request_id: "p" }))
      .rejects.toMatchObject({ status: 409, code: "thread_too_deep", details: { max_depth: 1000 } })
  })
})

describe("knowledge client", () => {
  it("lists facts by project and topic with a cursor", async () => {
    await listFacts({ project: "whiskers", topic: "gotchas", status: "active" }, { cursor: "c1", limit: 50 })
    expect(last().url.pathname).toBe("/api/v1/knowledge/facts")
    expect(Object.fromEntries(last().url.searchParams)).toEqual({
      project: "whiskers", topic: "gotchas", status: "active", cursor: "c1", limit: "50",
    })
    await listTopics("whiskers")
    expect(last().url.pathname).toBe("/api/v1/knowledge/topics")
    expect(last().url.searchParams.get("project")).toBe("whiskers")
  })

  it("adds a fact with only the set source fields", async () => {
    await addFact({ project: "whiskers", fact: "Use bm25 by default", author: "ada", topic: "search", source_run: "", source_step: "S06" })
    expect(last()).toMatchObject({ method: "POST" })
    expect(last().body).toEqual({ project: "whiskers", fact: "Use bm25 by default", author: "ada", topic: "search", source_step: "S06" })
  })

  it("sets status with PATCH and searches through kind=knowledge", async () => {
    await setFactStatus(8, "superseded", "ada")
    expect(last()).toMatchObject({ method: "PATCH", body: { status: "superseded", actor: "ada" } })
    expect(last().url.pathname).toBe("/api/v1/knowledge/facts/8")
    await searchFacts("embed queue")
    expect(last().url.pathname).toBe("/api/v1/search")
    expect(last().url.searchParams.get("kind")).toBe("knowledge")
    expect(last().url.searchParams.get("q")).toBe("embed queue")
  })

  it("collects projects from every fact page, not just the newest", async () => {
    const fact = (id: number, project: string) => ({ id, project, topic: "general", fact: `f${id}`, status: "active", author: "ada" })
    const pages = [
      { facts: [fact(450, "whiskers"), fact(449, "whiskers")], next_cursor: "p2", has_more: true },
      { facts: [fact(250, "tap")], next_cursor: "p3", has_more: true },
      { facts: [fact(3, "archive-only")], next_cursor: null, has_more: false },
    ]
    let index = 0
    next = () => json(pages[index++])
    expect(await listProjects()).toEqual(["archive-only", "tap", "whiskers"])
    expect(calls.map((call) => call.url.searchParams.get("cursor"))).toEqual([null, "p2", "p3"])
    expect(calls.every((call) => call.url.searchParams.get("limit") === "200")).toBe(true)
    expect(calls.every((call) => !call.url.searchParams.has("status"))).toBe(true)
  })

  it("exports JSONL as text", async () => {
    next = () => new Response('{"fact":"a"}\n{"fact":"b"}\n', { headers: { "Content-Type": "application/x-ndjson" } })
    const text = await exportFacts({ project: "whiskers", status: "all" })
    expect(text.split("\n").filter(Boolean)).toHaveLength(2)
    expect(Object.fromEntries(last().url.searchParams)).toEqual({ project: "whiskers", status: "all" })
  })

  it("maps an export error like any other request", async () => {
    next = () => json({ error: { code: "validation_error", message: "status must be one of active, superseded, rejected" } }, 400)
    await expect(exportFacts({ status: "active" })).rejects.toMatchObject({ code: "validation_error" })
  })
})

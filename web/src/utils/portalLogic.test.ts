import { describe, expect, it } from "vitest"
import { ApiError } from "@/api/client"
import { EventRefresh } from "@/api/eventRefresh"
import type { EmbedStatus, FeedPost, Mail, ThreadNode } from "@/api/types"
import { effectiveMode, searchModeOptions } from "./searchModes"
import { mergeThread, threadDepth, walkThread } from "./thread"
import { applyMailUpdate, countsFromUpdate, unreadBadge } from "./unread"
import { bodySegments, snippetSegments } from "./richText"

const embedOn: EmbedStatus = { provider: "openai", model: "synthetic-embed", dim: 4, indexed: 3, pending: 0, last_error: null }
const embedOff: EmbedStatus = { provider: "off", model: null, dim: null, indexed: 0, pending: 0, last_error: null }

function enabled(options: ReturnType<typeof searchModeOptions>) {
  return Object.fromEntries(options.map((option) => [option.mode, option.enabled]))
}

describe("search mode toggle", () => {
  it("offers every mode when an embedder is configured", () => {
    const options = searchModeOptions({ status: embedOn })
    expect(enabled(options)).toEqual({ bm25: true, vector: true, hybrid: true })
    expect(options.every((option) => option.hint === null)).toBe(true)
  })

  it("disables vector and hybrid with a hint when the embedder is off", () => {
    const options = searchModeOptions({ status: embedOff })
    expect(enabled(options)).toEqual({ bm25: true, vector: false, hybrid: false })
    expect(options.find((option) => option.mode === "vector")?.hint).toMatch(/no embedder/i)
    expect(effectiveMode("hybrid", options)).toBe("bm25")
    expect(effectiveMode("bm25", options)).toBe("bm25")
  })

  it("disables them while the status is unknown or failed", () => {
    expect(enabled(searchModeOptions({ status: undefined }))).toMatchObject({ vector: false, hybrid: false })
    const failed = searchModeOptions({ status: undefined, statusError: new Error("offline") })
    expect(enabled(failed)).toMatchObject({ bm25: true, vector: false })
    expect(failed[1].hint).toMatch(/unavailable/i)
  })

  it("disables them after the hub answers that the embedder is unavailable", () => {
    for (const code of ["not_available", "embedder_unavailable"]) {
      const err = new ApiError(code === "not_available" ? 400 : 503, { code, message: "embedder down" }, "x")
      const options = searchModeOptions({ status: embedOn, searchError: err })
      expect(enabled(options)).toEqual({ bm25: true, vector: false, hybrid: false })
      expect(options[2].hint).toContain("embedder down")
      expect(effectiveMode("vector", options)).toBe("bm25")
    }
  })

  it("keeps the modes on for unrelated errors and only warns on a stale queue error", () => {
    const unrelated = new ApiError(400, { code: "validation_error", message: "bad since" }, "x")
    expect(enabled(searchModeOptions({ status: embedOn, searchError: unrelated }))).toMatchObject({ vector: true })
    const warned = searchModeOptions({ status: { ...embedOn, last_error: "timeout" } })
    expect(enabled(warned)).toMatchObject({ vector: true, hybrid: true })
    expect(warned[1].hint).toContain("timeout")
    expect(effectiveMode("vector", warned)).toBe("vector")
  })
})

function post(id: number, parent: number | null, extra: Partial<ThreadNode> = {}): ThreadNode {
  return {
    id, kind: parent == null ? "post" : "reply", author: "ada", body: `post ${id}`, parent_id: parent,
    root_id: 1, depth: 0, repost_of: null, quote_of: null, created_at: "2026-10-16T00:00:00Z",
    deleted: false, deleted_at: null, hashtags: [], reply_count: 0, repost_count: 0, quote_count: 0,
    original: null, replies: [], ...extra,
  }
}

/** Nest a flat list by parent_id the way the hub does for one page. */
function nest(posts: ThreadNode[]): ThreadNode[] {
  const byId = new Map(posts.map((item) => [item.id, { ...item, replies: [] as ThreadNode[] }]))
  const roots: ThreadNode[] = []
  for (const item of byId.values()) {
    const parent = item.parent_id != null ? byId.get(item.parent_id) : undefined
    if (parent) parent.replies.push(item)
    else roots.push(item)
  }
  return roots
}

describe("thread tree building", () => {
  it("joins replies whose parent came on an earlier page", () => {
    const first = nest([post(1, null), post(2, 1), post(3, 1)])
    const second = nest([post(4, 2), post(5, 4), post(6, 3)])
    expect(second.map((node) => node.id)).toEqual([4, 6])
    const merged = mergeThread([{ posts: first }, { posts: second }])
    expect(merged.map((node) => node.id)).toEqual([1])
    expect(merged[0].replies.map((node) => node.id)).toEqual([2, 3])
    expect(merged[0].replies[0].replies[0].replies.map((node) => node.id)).toEqual([5])
    expect(merged[0].replies[1].replies.map((node) => node.id)).toEqual([6])
  })

  it("keeps sibling order by id and drops duplicate copies of a post", () => {
    const first = nest([post(1, null), post(9, 1), post(3, 1)])
    const second = nest([post(3, 1, { body: "edited" }), post(5, 1)])
    const merged = mergeThread([{ posts: first }, { posts: second }])
    expect(merged[0].replies.map((node) => node.id)).toEqual([3, 5, 9])
    expect(merged[0].replies[0].body).toBe("edited")
    expect(walkThread(merged).map((node) => node.id)).toEqual([1, 3, 5, 9])
  })

  it("leaves orphans (the window top) at the top level, focus and edge markers intact", () => {
    const merged = mergeThread([{ posts: [post(20, 19, { focus: true, replies: [post(21, 20, { more_replies: true })] })] }])
    expect(merged.map((node) => node.id)).toEqual([20])
    expect(merged[0].focus).toBe(true)
    expect(merged[0].replies[0].more_replies).toBe(true)
  })

  it("rebuilds a 60-deep chain split over two pages without recursion", () => {
    const chain = Array.from({ length: 60 }, (_, index) => post(index + 1, index === 0 ? null : index))
    const merged = mergeThread([{ posts: nest(chain.slice(0, 30)) }, { posts: nest(chain.slice(30)) }])
    expect(merged).toHaveLength(1)
    expect(threadDepth(merged)).toBe(60)
    expect(walkThread(merged).map((node) => node.id)).toEqual(chain.map((node) => node.id))
  })

  it("handles a 5000-deep chain (no stack overflow)", () => {
    const chain = Array.from({ length: 5000 }, (_, index) => post(index + 1, index === 0 ? null : index))
    let nested = chain[chain.length - 1]
    for (let index = chain.length - 2; index >= 0; index--) nested = { ...chain[index], replies: [nested] }
    expect(threadDepth(mergeThread([{ posts: [nested] }]))).toBe(5000)
  })
})

function mail(id: number, extra: Partial<Mail> = {}): Mail {
  return {
    id, recipient: "ada", sender: "bob", subject: "", body: "hi", created_at: "2026-10-16T00:00:00Z",
    read_at: null, acked_at: null, reply_to: null, message_id: null, state: "unread", ...extra,
  }
}

describe("unread badge", () => {
  it("hides at zero and caps at 99+", () => {
    expect(unreadBadge(0)).toBeNull()
    expect(unreadBadge(undefined)).toBeNull()
    expect(unreadBadge(-1)).toBeNull()
    expect(unreadBadge(1)).toBe("1")
    expect(unreadBadge(99)).toBe("99")
    expect(unreadBadge(100)).toBe("99+")
  })

  it("takes fresh counts from a mark-read answer and updates only changed rows", () => {
    const update = { agent: "ada", updated: [2], unread: 1, unacked: 2, total: 2 }
    expect(countsFromUpdate(update)).toEqual({ agent: "ada", unread: 1, unacked: 2, total: 2 })
    const rows = applyMailUpdate([mail(1), mail(2)], update, "read", "t1")
    expect(rows.map((row) => row.state)).toEqual(["unread", "read"])
    expect(rows[1].read_at).toBe("t1")
    const acked = applyMailUpdate(rows, { ...update, updated: [1] }, "ack", "t2")
    expect(acked[0]).toMatchObject({ state: "acked", read_at: "t2", acked_at: "t2" })
  })

  it("refreshes mail on a mail event, feed and knowledge on theirs", () => {
    const refresh = new EventRefresh()
    refresh.accept({ id: "1", event: "mail", data: "{}" })
    refresh.accept({ id: "2", event: "feed_post", data: "{}" })
    refresh.accept({ id: "3", event: "feed_follow", data: "{}" })
    refresh.accept({ id: "4", event: "knowledge_fact", data: "{}" })
    const keys: string[] = []
    refresh.flush((key) => keys.push(key))
    expect(keys.sort()).toEqual(["feed", "knowledge", "mail"])
  })

  it("refreshes every family after a cursor reset", () => {
    const refresh = new EventRefresh()
    refresh.accept({ event: "reset", data: "{}" })
    const keys: string[] = []
    refresh.flush((key) => keys.push(key))
    expect(keys).toEqual(expect.arrayContaining(["mail", "feed", "knowledge", "messages"]))
  })
})

describe("plain-text rendering helpers", () => {
  it("splits snippet match markers", () => {
    expect(snippetSegments("the [deploy] broke [prod]…")).toEqual([
      { text: "the ", match: false },
      { text: "deploy", match: true },
      { text: " broke ", match: false },
      { text: "prod", match: true },
      { text: "…", match: false },
    ])
    expect(snippetSegments("unclosed [bracket")).toEqual([{ text: "unclosed [bracket", match: false }])
  })

  it("keeps markup as text in snippets and bodies", () => {
    const evil = '<script>alert(1)</script> [<img src=x onerror=alert(1)>]'
    const segments = snippetSegments(evil)
    expect(segments.map((segment) => segment.text).join("")).toBe('<script>alert(1)</script> <img src=x onerror=alert(1)>')
    expect(segments[1]).toEqual({ text: "<img src=x onerror=alert(1)>", match: true })
    const body = bodySegments('<img src=x onerror="alert(1)"> #ops')
    expect(body[0]).toEqual({ kind: "text", text: '<img src=x onerror="alert(1)"> ' })
  })

  it("finds hashtags like the hub does", () => {
    const post: Pick<FeedPost, "body"> = { body: "Ship #Deploy now, not a#tag or &#38; or url/#frag, #ops_2" }
    const tags = bodySegments(post.body).filter((segment) => segment.kind === "tag")
    expect(tags.map((segment) => segment.kind === "tag" && segment.tag)).toEqual(["deploy", "ops_2"])
    expect(bodySegments("plain")).toEqual([{ kind: "text", text: "plain" }])
  })
})

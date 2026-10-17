// @vitest-environment jsdom
import { render } from "solid-js/web"
import { QueryClient, QueryClientProvider } from "@tanstack/solid-query"
import { MemoryRouter, Route } from "@solidjs/router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError } from "@/api/client"
import { getEvolveState, listProposals, listReactions, react, unreact, updateProposal } from "@/api/proposals"
import type { PostReactions, Proposal, ReactionState } from "@/api/types"
import { prefs } from "@/store/prefsSlice"
import ProposalsPage from "./ProposalsPage"

vi.mock("@/api/proposals", () => ({
  listProposals: vi.fn(),
  getProposal: vi.fn(),
  updateProposal: vi.fn(),
  getEvolveState: vi.fn(),
  listReactions: vi.fn(),
  react: vi.fn(),
  unreact: vi.fn(),
}))

function proposal(number: number, overrides: Partial<Proposal> = {}): Proposal {
  return {
    id: `P${number}`,
    number,
    project: "demo",
    title: `Proposal ${number}`,
    summary: `Summary ${number}`,
    evidence: [],
    weight: 3,
    status: "open",
    author: "bot-1",
    decided_by: null,
    decided_at: null,
    reason: null,
    run_ref: null,
    step_ref: null,
    pr_ref: null,
    post_id: 100 + number,
    created_at: "2026-10-16T09:00:00Z",
    updated_at: "2026-10-16T09:00:00Z",
    ...overrides,
  }
}

const board: Proposal[] = [
  proposal(1, { evidence: ["https://example.test/run/1", "javascript:alert(1)", "post 42"] }),
  proposal(2, { status: "accepted", decided_by: "alice", reason: "worth it" }),
  proposal(3, { status: "rejected", decided_by: "alice", reason: "duplicate" }),
  proposal(4, { status: "building" }),
  proposal(5, { status: "built" }),
]

/** Hub-like listing: `mine` comes from the full vote lists, `handles` is the sample. */
const counts = (postId: number, plus: string[], minus: string[], handle?: string, sample = 100): PostReactions => {
  const reactions: Record<string, number> = {}
  if (plus.length) reactions["+1"] = plus.length
  if (minus.length) reactions["-1"] = minus.length
  const listed: PostReactions = {
    post_id: postId,
    reactions,
    handles: { "+1": plus.slice(0, sample), "-1": minus.slice(0, sample) },
    score: plus.length - minus.length,
  }
  if (handle) listed.mine = [...(plus.includes(handle) ? ["+1"] : []), ...(minus.includes(handle) ? ["-1"] : [])]
  return listed
}

let container: HTMLDivElement
let dispose: (() => void) | undefined
const settle = async () => { await new Promise((resolve) => setTimeout(resolve, 20)) }
const card = (id: string) => container.querySelector(`[data-proposal="${id}"]`) as HTMLElement
const column = (status: string) => container.querySelector(`[data-status="${status}"]`) as HTMLElement
const button = (root: HTMLElement, label: string) =>
  [...root.querySelectorAll("button")].find((item) => item.getAttribute("aria-label") === label || item.textContent?.trim() === label) as HTMLButtonElement
const count = (id: string, kind: string) => card(id).querySelector(`[data-count="${kind}"]`)?.textContent

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  dispose = render(() => <QueryClientProvider client={client}><MemoryRouter><Route path="*" component={ProposalsPage} /></MemoryRouter></QueryClientProvider>, container)
}

beforeEach(() => {
  vi.mocked(listProposals).mockReset().mockResolvedValue({ proposals: board, next_cursor: null, has_more: false })
  vi.mocked(getEvolveState).mockReset().mockResolvedValue({ paused: false, by: null, at: null })
  vi.mocked(listReactions).mockReset().mockImplementation(async (postId: number, handle?: string) =>
    postId === 101 ? counts(101, ["bot-1"], ["alice-2"], handle) : counts(postId, [], [], handle))
  vi.mocked(react).mockReset()
  vi.mocked(unreact).mockReset()
  vi.mocked(updateProposal).mockReset()
  prefs.setDisplayName("alice")
  container = document.createElement("div")
  document.body.append(container)
})

afterEach(() => {
  dispose?.()
  container.remove()
  prefs.setDisplayName("")
})

describe("proposals board", () => {
  it("renders one column per status with the right cards", async () => {
    mount()
    await settle()
    const order = [...container.querySelectorAll("[data-status]")].map((item) => item.getAttribute("data-status"))
    expect(order).toEqual(["open", "accepted", "rejected", "building", "built", "withdrawn"])
    expect(column("open").querySelector('[data-proposal="P1"]')).not.toBeNull()
    expect(column("accepted").querySelector('[data-proposal="P2"]')).not.toBeNull()
    expect(column("rejected").querySelector('[data-proposal="P3"]')).not.toBeNull()
    expect(column("building").querySelector('[data-proposal="P4"]')).not.toBeNull()
    expect(column("built").querySelector('[data-proposal="P5"]')).not.toBeNull()
    expect(column("open").querySelectorAll("[data-proposal]")).toHaveLength(1)
    expect(column("withdrawn").querySelectorAll("[data-proposal]")).toHaveLength(0)
    expect(column("withdrawn").classList.contains("opacity-60")).toBe(true)
    expect(card("P1").textContent).toContain("Proposal 1")
    expect(card("P1").textContent).toContain("demo")
    expect(card("P1").textContent).toContain("w3")
    expect(card("P2").querySelector("[data-decision]")?.textContent).toMatch(/accepted by alice: worth it/)
  })

  it("links http evidence with noopener and keeps anything else as text", async () => {
    mount()
    await settle()
    const anchors = card("P1").querySelectorAll('[aria-label="Evidence"] a')
    expect(anchors).toHaveLength(1)
    const link = anchors[0] as HTMLAnchorElement
    expect(link.getAttribute("href")).toBe("https://example.test/run/1")
    expect(link.getAttribute("target")).toBe("_blank")
    expect(link.getAttribute("rel")).toBe("noopener noreferrer")
    const text = card("P1").querySelector('[aria-label="Evidence"]')!.textContent
    expect(text).toContain("javascript:alert(1)")
    expect(text).toContain("post 42")
  })

  it("shows the score and the vote counts", async () => {
    mount()
    await settle()
    expect(card("P1").querySelector("[data-score]")?.textContent).toBe("score 0")
    expect(count("P1", "+1")).toBe("1")
    expect(count("P1", "-1")).toBe("1")
    expect(count("P2", "+1")).toBe("0")
  })

  it("votes +1 optimistically and keeps the hub's answer", async () => {
    let finish: (value: ReactionState) => void = () => {}
    vi.mocked(react).mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    mount()
    await settle()
    button(card("P2"), "Vote +1").click()
    await settle()
    expect(vi.mocked(react)).toHaveBeenCalledWith(102, "+1", "alice")
    expect(count("P2", "+1")).toBe("1")
    expect(card("P2").querySelector("[data-score]")?.textContent).toBe("score 1")
    finish({ post_id: 102, handle: "alice", kind: "+1", reactions: { "+1": 2 }, score: 2, mine: ["+1"], replaced: [], idempotent: false })
    await settle()
    expect(count("P2", "+1")).toBe("2")
    expect(button(card("P2"), "Vote +1").getAttribute("aria-pressed")).toBe("true")
  })

  it("rolls the count back when the vote fails", async () => {
    vi.mocked(react).mockRejectedValue(new ApiError(404, { code: "not_found", message: "post 102 not found" }, "Not Found"))
    mount()
    await settle()
    button(card("P2"), "Vote +1").click()
    await settle()
    expect(count("P2", "+1")).toBe("0")
    expect(card("P2").textContent).toContain("post 102 not found")
  })

  it("unvotes a held vote with DELETE", async () => {
    prefs.setDisplayName("bot-1")
    vi.mocked(unreact).mockResolvedValue({ post_id: 101, handle: "bot-1", kind: "+1", reactions: { "-1": 1 }, score: -1, mine: [], idempotent: false })
    mount()
    await settle()
    expect(button(card("P1"), "Vote +1").getAttribute("aria-pressed")).toBe("true")
    button(card("P1"), "Vote +1").click()
    await settle()
    expect(vi.mocked(unreact)).toHaveBeenCalledWith(101, "+1", "bot-1")
    expect(count("P1", "+1")).toBe("0")
  })

  it("asks the hub for the caller's own votes and trusts them over the handle sample", async () => {
    // alice voted +1 after 100 others: she is outside the sampled handles.
    const crowd = Array.from({ length: 100 }, (_, index) => `agent${index}`)
    vi.mocked(listReactions).mockImplementation(async (postId: number, handle?: string) =>
      counts(postId, postId === 102 ? [...crowd, "alice"] : [], [], handle))
    vi.mocked(unreact).mockResolvedValue({ post_id: 102, handle: "alice", kind: "+1", reactions: { "+1": 100 }, score: 100, mine: [], idempotent: false })
    mount()
    await settle()
    expect(vi.mocked(listReactions)).toHaveBeenCalledWith(102, "alice", expect.anything())
    expect(count("P2", "+1")).toBe("101")
    expect(button(card("P2"), "Vote +1").getAttribute("aria-pressed")).toBe("true")
    button(card("P2"), "Vote +1").click()
    await settle()
    expect(vi.mocked(unreact)).toHaveBeenCalledWith(102, "+1", "alice")
    expect(vi.mocked(react)).not.toHaveBeenCalled()
    expect(count("P2", "+1")).toBe("100")
    expect(button(card("P2"), "Vote +1").getAttribute("aria-pressed")).toBe("false")
  })

  it("keeps one vote in flight: buttons are disabled until the hub answers", async () => {
    let finish: (value: ReactionState) => void = () => {}
    vi.mocked(react).mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    mount()
    await settle()
    button(card("P2"), "Vote +1").click()
    await settle()
    expect(button(card("P2"), "Vote +1").disabled).toBe(true)
    expect(button(card("P2"), "Vote -1").disabled).toBe(true)
    button(card("P2"), "Vote -1").click()
    button(card("P2"), "Vote +1").click()
    await settle()
    expect(vi.mocked(react)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(unreact)).not.toHaveBeenCalled()
    finish({ post_id: 102, handle: "alice", kind: "+1", reactions: { "+1": 1 }, score: 1, mine: ["+1"], replaced: [], idempotent: false })
    await settle()
    expect(button(card("P2"), "Vote +1").disabled).toBe(false)
    expect(count("P2", "+1")).toBe("1")
  })

  it("disables votes with a hint when there is no handle", async () => {
    prefs.setDisplayName("")
    mount()
    await settle()
    expect(button(card("P1"), "Vote +1").disabled).toBe(true)
    expect(card("P1").textContent).toMatch(/set a display name to vote/i)
    expect(button(card("P1"), "Accept")).toBeUndefined()
  })

  it("requires a reason to reject, then sends it", async () => {
    vi.mocked(updateProposal).mockResolvedValue({ proposal: proposal(1, { status: "rejected" }) })
    mount()
    await settle()
    button(card("P1"), "Reject").click()
    await settle()
    const form = card("P1").querySelector('form[aria-label="Reject P1"]') as HTMLFormElement
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }))
    await settle()
    expect(vi.mocked(updateProposal)).not.toHaveBeenCalled()
    expect(card("P1").textContent).toMatch(/reason is required/i)
    const input = form.querySelector("textarea") as HTMLTextAreaElement
    input.value = "out of scope"
    input.dispatchEvent(new InputEvent("input", { bubbles: true }))
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }))
    await settle()
    expect(vi.mocked(updateProposal)).toHaveBeenCalledWith("P1", "alice", { status: "rejected", reason: "out of scope" })
  })

  it("shows the hub's refusal when a decision is not allowed", async () => {
    vi.mocked(updateProposal).mockRejectedValue(new ApiError(403, { code: "forbidden", message: "alice may not decide proposals" }, "Forbidden"))
    mount()
    await settle()
    button(card("P1"), "Accept").click()
    await settle()
    expect(vi.mocked(updateProposal)).toHaveBeenCalledWith("P1", "alice", { status: "accepted" })
    expect(card("P1").textContent).toContain("alice may not decide proposals")
  })

  it("shows the paused banner only when evolve is paused", async () => {
    mount()
    await settle()
    expect(container.querySelector('[role="alert"]')).toBeNull()
    dispose?.()
    vi.mocked(getEvolveState).mockResolvedValue({ paused: true, by: "alice", at: "2026-10-16T10:00:00Z" })
    mount()
    await settle()
    expect(container.querySelector('[role="alert"]')?.textContent).toMatch(/^Evolve paused by alice at /)
  })
})

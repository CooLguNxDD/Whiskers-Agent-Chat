// @vitest-environment jsdom
import { render } from "solid-js/web"
import { QueryClient, QueryClientProvider } from "@tanstack/solid-query"
import { MemoryRouter, Route } from "@solidjs/router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError } from "@/api/client"
import { getAttachment } from "@/api/fleet"
import { search, searchStatus } from "@/api/search"
import { ui } from "@/store/uiSlice"
import type { EmbedStatus } from "@/api/types"
import SearchPage from "./SearchPage"

vi.mock("@/api/search", () => ({ search: vi.fn(), searchStatus: vi.fn() }))
vi.mock("@/api/fleet", () => ({
  listChannels: vi.fn(() => Promise.resolve({ channels: [] })),
  getAttachment: vi.fn(() => Promise.resolve({ attachment: { id: 7, message_id: 9 } })),
}))

const on: EmbedStatus = { provider: "openai", model: "synthetic-embed", dim: 4, indexed: 1, pending: 0, last_error: null }
const off: EmbedStatus = { provider: "off", model: null, dim: null, indexed: 0, pending: 0, last_error: null }

let container: HTMLDivElement
let dispose: (() => void) | undefined
const settle = async () => { await new Promise((resolve) => setTimeout(resolve, 20)) }
const radio = (mode: string) => container.querySelector(`[role="radio"]:nth-child(${["bm25", "vector", "hybrid"].indexOf(mode) + 1})`) as HTMLButtonElement
const typeQuery = (value: string) => {
  const input = container.querySelector('input[aria-label="Search query"]') as HTMLInputElement
  input.value = value
  input.dispatchEvent(new InputEvent("input", { bubbles: true }))
}
const submit = async () => {
  container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }))
  await settle()
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  dispose = render(() => <QueryClientProvider client={client}><MemoryRouter><Route path="*" component={SearchPage} /></MemoryRouter></QueryClientProvider>, container)
}

beforeEach(() => {
  vi.mocked(search).mockReset()
  vi.mocked(searchStatus).mockReset()
  vi.mocked(search).mockResolvedValue({ results: [], total_estimate: 0, mode: "bm25", limit: 20, offset: 0, next_offset: null })
  container = document.createElement("div")
  document.body.append(container)
})

afterEach(() => {
  dispose?.()
  container.remove()
})

describe("search mode toggle", () => {
  it("disables vector and hybrid with a hint when the embedder is off", async () => {
    vi.mocked(searchStatus).mockResolvedValue(off)
    mount()
    await settle()
    expect(radio("bm25").disabled).toBe(false)
    expect(radio("vector").disabled).toBe(true)
    expect(radio("hybrid").disabled).toBe(true)
    expect(container.querySelector('[role="note"]')?.textContent).toMatch(/no embedder/i)
    typeQuery("deploy")
    await submit()
    expect(vi.mocked(search).mock.calls[0][0]).toMatchObject({ q: "deploy", mode: "bm25" })
  })

  it("searches in the picked mode when the embedder is on", async () => {
    vi.mocked(searchStatus).mockResolvedValue(on)
    mount()
    await settle()
    expect(radio("hybrid").disabled).toBe(false)
    radio("hybrid").click()
    typeQuery("deploy")
    await submit()
    expect(vi.mocked(search).mock.calls.at(-1)?.[0]).toMatchObject({ mode: "hybrid" })
    expect(radio("hybrid").getAttribute("aria-checked")).toBe("true")
  })

  it("falls back to bm25 after the hub reports the embedder unavailable", async () => {
    vi.mocked(searchStatus).mockResolvedValue(on)
    vi.mocked(search).mockRejectedValueOnce(new ApiError(503, { code: "embedder_unavailable", message: "embedder timed out" }, "x"))
    mount()
    await settle()
    radio("vector").click()
    typeQuery("deploy")
    await submit()
    await settle()
    expect(radio("vector").disabled).toBe(true)
    expect(radio("bm25").getAttribute("aria-checked")).toBe("true")
    expect(container.querySelector('[role="note"]')?.textContent).toContain("embedder timed out")
  })

  it("disables vector and hybrid when the status reports an embedder error", async () => {
    vi.mocked(searchStatus).mockResolvedValue({ ...on, last_error: "embed request timed out" })
    mount()
    await settle()
    expect(radio("vector").disabled).toBe(true)
    expect(radio("hybrid").disabled).toBe(true)
    expect(container.querySelector('[role="note"]')?.textContent).toContain("embed request timed out")
  })

  it("renders snippet markup as text", async () => {
    vi.mocked(searchStatus).mockResolvedValue(off)
    vi.mocked(search).mockResolvedValue({
      results: [{ kind: "message", id: 1, channel: "fleet", author: "ada", created_at: "2026-10-16T00:00:00Z", snippet: '<img src=x onerror=alert(1)> [hit]', score: 1 }],
      total_estimate: 1, mode: "bm25", limit: 20, offset: 0, next_offset: null,
    })
    mount()
    await settle()
    typeQuery("hit")
    await submit()
    await settle()
    expect(container.querySelector("img")).toBeNull()
    expect(container.querySelector("mark")?.textContent).toBe("hit")
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>")
  })
})

describe("opening a result", () => {
  const hit = (kind: "message" | "attachment", id: number) => ({ kind, id, channel: "ops", author: "ada", created_at: "2026-10-16T00:00:00Z", snippet: "[deploy]", score: 1 })

  async function openFirst(kind: "message" | "attachment", id: number) {
    vi.mocked(searchStatus).mockResolvedValue(off)
    vi.mocked(search).mockResolvedValue({ results: [hit(kind, id)], total_estimate: 1, mode: "bm25", limit: 20, offset: 0, next_offset: null })
    mount()
    await settle()
    typeQuery("deploy")
    await submit()
    await settle()
    const button = [...container.querySelectorAll("button")].find((item) => item.textContent?.startsWith("Open"))!
    button.click()
    await settle()
  }

  it("focuses the message hit in its channel", async () => {
    await openFirst("message", 42)
    expect(ui.activeChannel()).toBe("ops")
    expect(ui.focusMessageId()).toBe(42)
  })

  it("focuses the message that carries an attachment hit", async () => {
    await openFirst("attachment", 7)
    expect(vi.mocked(getAttachment)).toHaveBeenCalledWith(7)
    expect(ui.activeChannel()).toBe("ops")
    expect(ui.focusMessageId()).toBe(9)
  })
})

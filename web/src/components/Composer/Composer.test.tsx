// @vitest-environment jsdom
import { render } from "solid-js/web"
import { QueryClient, QueryClientProvider } from "@tanstack/solid-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { postMessage } from "@/api/fleet"
import { ApiError } from "@/api/client"
import { usePrefs, useUIStore } from "@/store"
import Composer from "./Composer"

// The composer reads the active channel's archive state from the channels query.
let archivedAt: string | null = null
vi.mock("@/api/fleet", () => ({
  postMessage: vi.fn(),
  listChannels: vi.fn(() =>
    Promise.resolve({
      channels: [
        {
          id: 1,
          name: "fleet",
          topic: "",
          created_at: "now",
          state: archivedAt ? "archived" : "active",
          state_note: "",
          state_updated_at: archivedAt,
          state_updated_by: archivedAt ? "ada" : null,
          archived_at: archivedAt,
          archived_by: archivedAt ? "ada" : null,
        },
      ],
    }),
  ),
}))

let container: HTMLDivElement
let dispose: (() => void) | undefined
let client: QueryClient
const settle = async () => { await new Promise((resolve) => setTimeout(resolve, 10)) }
const textarea = () => container.querySelector("textarea") as HTMLTextAreaElement
const typeText = async (value: string) => {
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(textarea(), value)
  textarea().dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }))
  await settle()
}
const submit = async () => {
  container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }))
  await settle()
}

beforeEach(() => {
  vi.mocked(postMessage).mockReset()
  archivedAt = null
  usePrefs().setDisplayName("ada")
  useUIStore().setActiveChannel("fleet")
  container = document.createElement("div")
  document.body.append(container)
  client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  dispose = render(
    () => <QueryClientProvider client={client}><Composer replyTo={null} onClearReply={() => undefined} /></QueryClientProvider>,
    container,
  )
})

afterEach(() => {
  dispose?.()
  client.clear()
  container.remove()
})

describe("Solid composer confirmation", () => {
  it("is read-only while the active channel is archived", async () => {
    archivedAt = "2026-09-22T00:00:00Z"
    await client.invalidateQueries({ queryKey: ["channels"] })
    await settle()
    expect(textarea().readOnly).toBe(true)
    expect(textarea().placeholder).toContain("archived")
    await typeText("blocked")
    await submit()
    expect(postMessage).not.toHaveBeenCalled()
  })

  it("protects a draft until the hub confirms it", async () => {
    let resolve!: (value: Awaited<ReturnType<typeof postMessage>>) => void
    vi.mocked(postMessage).mockImplementation(() => new Promise((done) => { resolve = done }))
    await typeText("original")
    await submit()
    expect(textarea().readOnly).toBe(true)
    expect(textarea().value).toBe("original")
    resolve({ message: { id: 1, channel: "fleet", author: "ada", text: "original", reply_to: null, mentions: [], created_at: "now" } })
    await settle()
    expect(textarea().value).toBe("")
    expect(textarea().readOnly).toBe(false)
  })

  it("retries an uncertain send with the same payload and idempotency key", async () => {
    vi.mocked(postMessage).mockRejectedValue(new TypeError("connection lost"))
    await typeText("original")
    await submit()
    expect(container.textContent).toContain("Retry original send")
    const first = vi.mocked(postMessage).mock.calls[0][0]
    await submit()
    expect(vi.mocked(postMessage).mock.calls[1][0]).toEqual(first)
  })

  it("allows correction after a definite rejection", async () => {
    vi.mocked(postMessage).mockRejectedValue(new ApiError(400, null, "invalid"))
    await typeText("bad draft")
    await submit()
    const first = vi.mocked(postMessage).mock.calls[0][0]
    expect(textarea().readOnly).toBe(false)
    await typeText("corrected")
    await submit()
    const second = vi.mocked(postMessage).mock.calls[1][0]
    expect(second.text).toBe("corrected")
    expect(second.client_request_id).not.toBe(first.client_request_id)
  })
})

// @vitest-environment jsdom
import { render } from "solid-js/web"
import { QueryClient, QueryClientProvider } from "@tanstack/solid-query"
import { MemoryRouter, Route } from "@solidjs/router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError } from "@/api/client"
import { addBinding, addRoute, createRelay, getRelay, listAudit, listRelays, rotateRelayToken, seekRoute, updateRelay } from "@/api/relays"
import type { Relay, RelaySnapshot } from "@/api/types"
import { prefs } from "@/store/prefsSlice"
import RelayPage from "./RelayPage"

vi.mock("@/api/relays", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/relays")>()),
  listRelays: vi.fn(),
  getRelay: vi.fn(),
  createRelay: vi.fn(),
  updateRelay: vi.fn(),
  deleteRelay: vi.fn(),
  rotateRelayToken: vi.fn(),
  addBinding: vi.fn(),
  deleteBinding: vi.fn(),
  addRoute: vi.fn(),
  updateRoute: vi.fn(),
  deleteRoute: vi.fn(),
  seekRoute: vi.fn(),
  listAudit: vi.fn(),
}))

function relay(overrides: Partial<Relay> = {}): Relay {
  return {
    id: 1,
    name: "main",
    platform: "discord",
    enabled: true,
    description: null,
    config: {
      prefix: "dc-", operators: ["900"], action_users: [], reactions: {}, ref_webhooks: [], guilds: [],
      mask: true, status_marks: true, read_commands: "bound",
    },
    version: 3,
    last_seen_at: "2026-10-29T10:00:00Z",
    status: { delivered: 4, failed: 1, inbound: 2, loops: 2, last_error: "send to 111: Forbidden" },
    has_token: true,
    state: "online",
    created_at: "2026-10-29T09:00:00Z",
    updated_at: "2026-10-29T09:00:00Z",
    ...overrides,
  }
}

const snapshot = (): RelaySnapshot => ({
  relay: relay(),
  version: 3,
  replies_consumer: "relay-main-replies",
  bindings: [
    { id: 1, relay_id: 1, external_id: "111", kind: "channel", parent_external_id: null, hub_channel: "fleet", allowed: ["ops"], any_channel: false, task_id: null, direction: "both" },
  ],
  routes: [
    {
      id: 7, relay_id: 1, consumer_id: 3, external_id: "111", format: "card", enabled: true,
      consumer: {
        id: 3, name: "relay-main-ab", owner: "ann", description: null, kinds: [], topics: ["ci.*.done"], channels: [], task_ids: [],
        destinations: [], exclude_authors: [], cursor: 40, lease_until: null, acked_at: null, relay_id: 1, armed: true, behind: 2,
      },
    },
  ],
})

let container: HTMLDivElement
let dispose: (() => void) | undefined
const settle = async () => { await new Promise((resolve) => setTimeout(resolve, 20)) }
const byLabel = (label: string) => container.querySelector(`[aria-label="${label}"]`) as HTMLInputElement
const button = (label: string) =>
  [...container.querySelectorAll("button")].find((item) => item.textContent?.trim() === label || item.getAttribute("aria-label") === label) as HTMLButtonElement
const type = (label: string, value: string) => {
  const input = byLabel(label)
  input.value = value
  input.dispatchEvent(new Event("input", { bubbles: true }))
}
const submit = (label: string) => (container.querySelector(`form[aria-label="${label}"]`) as HTMLFormElement).requestSubmit()

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  dispose = render(() => <QueryClientProvider client={client}><MemoryRouter><Route path="*" component={RelayPage} /></MemoryRouter></QueryClientProvider>, container)
}

beforeEach(() => {
  vi.mocked(listRelays).mockReset().mockResolvedValue({ relays: [relay(), relay({ id: 2, name: "tg", platform: "telegram", state: "offline", last_seen_at: null })] })
  vi.mocked(getRelay).mockReset().mockResolvedValue(snapshot())
  vi.mocked(listAudit).mockReset().mockResolvedValue({ audit: [], next_before_id: null })
  for (const fn of [createRelay, updateRelay, rotateRelayToken, addBinding, addRoute, seekRoute]) vi.mocked(fn).mockReset()
  prefs.setDisplayName("ann")
  container = document.createElement("div")
  document.body.append(container)
})

afterEach(() => {
  dispose?.()
  container.remove()
  prefs.setDisplayName("")
})

describe("relay page", () => {
  it("lists relays with their live state and shows the first one's setup", async () => {
    mount()
    await settle()
    expect(container.querySelector('[data-testid="state-main"]')?.textContent).toContain("online")
    expect(container.querySelector('[data-testid="state-tg"]')?.textContent).toContain("offline")
    expect(container.textContent).toContain("4 delivered · 1 failed")
    expect(container.textContent).toContain("send to 111: Forbidden")
    expect(container.textContent).toContain("#fleet")
    expect(container.textContent).toContain("ci.*.done")
    expect(container.textContent).toContain("2 behind")
    expect(vi.mocked(getRelay)).toHaveBeenCalledWith("main", expect.anything())
  })

  it("shows a created relay's token once", async () => {
    vi.mocked(createRelay).mockResolvedValue({ relay: relay({ name: "new" }), token: "rly_synthetic" })
    mount()
    await settle()
    type("Relay name", "new")
    submit("New relay")
    await settle()
    expect(vi.mocked(createRelay)).toHaveBeenCalledWith({ actor: "ann", name: "new", platform: "discord" })
    expect(container.querySelector('[data-testid="relay-token"]')?.textContent).toBe("rly_synthetic")
    button("I saved it").click()
    await settle()
    expect(container.querySelector('[data-testid="relay-token"]')).toBeNull()
  })

  it("disables with the reviewed version and reports a conflict", async () => {
    vi.mocked(updateRelay).mockRejectedValue(new ApiError(412, { code: "version_conflict", message: "relay main changed" }, "Precondition Failed"))
    mount()
    await settle()
    button("Disable").click()
    await settle()
    expect(vi.mocked(updateRelay)).toHaveBeenCalledWith("main", 3, { actor: "ann", enabled: false })
    expect(container.textContent).toContain("relay main changed")
  })

  it("adds bindings and routes, validating ids first", async () => {
    vi.mocked(addBinding).mockResolvedValue({ binding: snapshot().bindings[0], version: 4 })
    vi.mocked(addRoute).mockResolvedValue({ route: snapshot().routes[0], version: 5 })
    mount()
    await settle()
    type("Platform channel id", "bad id")
    submit("Add binding")
    await settle()
    expect(vi.mocked(addBinding)).not.toHaveBeenCalled()
    type("Platform channel id", "222")
    type("Allowed #channels", "ops, *")
    submit("Add binding")
    await settle()
    expect(vi.mocked(addBinding)).toHaveBeenCalledWith("main", {
      actor: "ann", external_id: "222", hub_channel: "fleet", allowed: ["ops"], any_channel: true, direction: "both",
    })
    type("Route platform channel id", "222")
    type("What to deliver", "jobs.>, ci.*.done")
    submit("Add route")
    await settle()
    expect(vi.mocked(addRoute)).toHaveBeenCalledWith("main", { actor: "ann", external_id: "222", format: "line", topics: ["jobs.>", "ci.*.done"] })
  })

  it("needs a valid Name before any change", async () => {
    prefs.setDisplayName("")
    mount()
    await settle()
    button("Disable").click()
    await settle()
    expect(vi.mocked(updateRelay)).not.toHaveBeenCalled()
    expect(container.textContent).toContain("Set a valid Name")
  })

  it("replays a route through the route endpoint", async () => {
    vi.mocked(seekRoute).mockResolvedValue({ cursor: 10, previous: 40 })
    mount()
    await settle()
    button("Replay…").click()
    await settle()
    type("Replay after event id", "10")
    submit("Replay route")
    await settle()
    expect(vi.mocked(seekRoute)).toHaveBeenCalledWith("main", 7, 10, "ann")
  })

  it("never replays from an empty event id", async () => {
    mount()
    await settle()
    button("Replay…").click()
    await settle()
    submit("Replay route")
    await settle()
    expect(vi.mocked(seekRoute)).not.toHaveBeenCalled()
    expect(container.textContent).toContain("Replay needs an event id")
  })

  it("says why Remove does nothing without a Name", async () => {
    prefs.setDisplayName("")
    mount()
    await settle()
    button("Remove").click()
    await settle()
    expect(container.textContent).toContain("Set a valid Name")
  })

  it("starts a fresh form when switching relays", async () => {
    vi.mocked(getRelay).mockImplementation(async (name: string) => ({ ...snapshot(), relay: relay({ name }) }))
    mount()
    await settle()
    type("Platform channel id", "draft-for-main")
    ;(container.querySelector('[aria-pressed="false"]') as HTMLButtonElement).click()
    await settle()
    expect(byLabel("Platform channel id").value).toBe("")
  })

  it("rotates the token and shows it", async () => {
    vi.mocked(rotateRelayToken).mockResolvedValue({ relay: relay(), token: "rly_rotated" })
    mount()
    await settle()
    button("Rotate token").click()
    await settle()
    expect(container.querySelector('[data-testid="relay-token"]')?.textContent).toBe("rly_rotated")
  })
})

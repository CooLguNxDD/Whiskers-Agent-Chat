import { afterEach, expect, it, vi } from "vitest"
import { render } from "solid-js/web"
import { MemoryRouter, Route } from "@solidjs/router"
import type { FleetMessage } from "@/api/types"
import MessageRow from "./MessageRow"

let dispose: (() => void) | undefined
afterEach(() => { dispose?.(); document.body.innerHTML = "" })

function mount(message: Partial<FleetMessage>, extra: Record<string, unknown> = {}) {
  const container = document.createElement("div")
  document.body.append(container)
  const full: FleetMessage = { id: 7, channel: "fleet", author: "cat-1", text: "", reply_to: null, created_at: "2026-11-04T10:00:00Z", mentions: [], ...message }
  dispose = render(() => <MemoryRouter><Route path="*" component={() => <MessageRow message={full} onReply={vi.fn()} {...extra} />} /></MemoryRouter>, container)
  return container
}

it("keeps markup inert and links only http(s)", () => {
  const container = mount({ text: "<img src=x onerror=alert(1)> see https://x.dev and javascript:alert(1) @cat-2", mentions: ["cat-2"] })
  expect(container.querySelector("img")).toBeNull()
  const links = container.querySelectorAll("a")
  expect(links).toHaveLength(1)
  expect(links[0].getAttribute("href")).toBe("https://x.dev")
  expect(links[0].getAttribute("rel")).toContain("noopener")
  expect(container.querySelector(".mention")?.textContent).toBe("@cat-2")
})

it("shows the header only on the first row of a group; humans get a card, agents a plain row", () => {
  const grouped = mount({ author: "andrew", text: "hi" }, { first: false, mine: true })
  expect(grouped.querySelector("header")).toBeNull()
  expect(grouped.querySelector(".human-card")).not.toBeNull()
  dispose?.()
  const agent = mount({ author: "cat-1", text: "hub OK, devops.json PASS\nRestarts: 3\nLast exit: OOMKilled\nLimit: 128 MiB\nsee task #4" })
  expect(agent.querySelector(".human-card")).toBeNull()
  expect([...agent.querySelectorAll(".status-chip")].map((chip) => chip.getAttribute("data-tone"))).toEqual(["ok", "ok", "bad"])
  expect(agent.querySelectorAll(".facts-card dt")).toHaveLength(3)
  expect(agent.querySelector(".action-btn")?.textContent).toContain("Open task #4")
})

it("folds long logs", () => {
  const container = mount({ text: "```\n" + "line\n".repeat(12) + "```" })
  expect(container.querySelector("details.log-fold summary")?.textContent).toContain("12 lines")
})

it("collapses a long message until expanded, and quotes the parent", () => {
  const container = mount({ text: "line\n".repeat(30), reply_to: 3 }, { parent: { id: 3, channel: "fleet", author: "andrew", text: "```bash\nls\n```", reply_to: null, created_at: "", mentions: [] } })
  expect(container.querySelector(".collapsed-body")).not.toBeNull()
  const toggle = [...container.querySelectorAll("button")].find((button) => button.textContent === "Show more")!
  toggle.click()
  expect(container.querySelector(".collapsed-body")).toBeNull()
  expect(container.querySelector(".reply-quote")?.textContent).toContain("andrew")
  expect(container.querySelector(".reply-quote")?.textContent).not.toContain("```")
})

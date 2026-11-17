// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it } from "vitest"
import { createSignal, onCleanup } from "solid-js"
import { render } from "solid-js/web"
import { createDockState, DOCK_STORAGE_PREFIX, resetAllDocks } from "./dockState"
import { DockArea, zoneAt, type PanelDef } from "./DockArea"
import { findStack, split, stack, type SplitNode } from "./layout"

let dispose: (() => void) | undefined
let mounts = 0
let cleanups = 0
beforeEach(() => { localStorage.clear(); mounts = 0; cleanups = 0 })
afterEach(() => { dispose?.(); dispose = undefined; document.body.replaceChildren() })

const defaults = () => split("row", [stack(["chat"]), stack(["fleet"])], [0.75, 0.25])

function Counter(props: { label: string }) {
  const [count, setCount] = createSignal(0)
  mounts += 1
  onCleanup(() => { cleanups += 1 })
  return <button type="button" data-counter={props.label} onClick={() => setCount((n) => n + 1)}>{props.label}:{count()}</button>
}

const panels: PanelDef[] = [
  { id: "chat", title: "Chat", closable: false, render: () => <Counter label="chat" /> },
  { id: "fleet", title: "Fleet", render: () => <Counter label="fleet" /> },
]

function mount(dock = createDockState("test", ["chat", "fleet"], defaults)) {
  const host = document.createElement("div")
  document.body.append(host)
  dispose = render(() => <DockArea dock={dock} panels={panels} />, host)
  return { dock, host }
}

const counter = (label: string) => document.querySelector<HTMLButtonElement>(`[data-counter="${label}"]`)

it("picks drop zones from the outer quarter of each edge", () => {
  expect(zoneAt(50, 50, 100, 100)).toBe("center")
  expect(zoneAt(5, 50, 100, 100)).toBe("left")
  expect(zoneAt(95, 50, 100, 100)).toBe("right")
  expect(zoneAt(50, 3, 100, 100)).toBe("top")
  expect(zoneAt(50, 99, 100, 100)).toBe("bottom")
})

it("renders tabs, splitters and panel bodies; only closable panels get a close button", () => {
  mount()
  expect([...document.querySelectorAll(".dock-tab")].map((tab) => tab.textContent)).toEqual(["Chat", "Fleet×"])
  expect(document.querySelectorAll('[role="separator"]')).toHaveLength(1)
  expect(counter("chat")).not.toBeNull()
  expect(counter("fleet")).not.toBeNull()
})

it("moves a panel between stacks without remounting it", () => {
  const { dock } = mount()
  counter("fleet")!.click()
  counter("fleet")!.click()
  const chatStack = findStack(dock.root(), "chat")!.id
  dock.apply((root) => {
    const target = findStack(root, "chat")!.id
    expect(target).toBe(chatStack)
    return split("col", [stack(["fleet"]), stack(["chat"])])
  })
  expect(counter("fleet")!.textContent).toBe("fleet:2")
  expect(mounts).toBe(2)
  expect(cleanups).toBe(0)
})

it("resizes with the keyboard and saves the layout", () => {
  const { dock } = mount()
  const separator = document.querySelector<HTMLElement>('[role="separator"]')!
  expect(separator.getAttribute("aria-valuenow")).toBe("75")
  separator.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }))
  expect((dock.root() as SplitNode).sizes[0]).toBeCloseTo(0.78)
  expect(separator.getAttribute("aria-valuenow")).toBe("78")
  const saved = JSON.parse(localStorage.getItem(`${DOCK_STORAGE_PREFIX}test`)!)
  expect(saved.root.sizes[0]).toBeCloseTo(0.78)
})

it("closes and restores a panel, disposing its body while closed", () => {
  const { dock } = mount()
  document.querySelector<HTMLButtonElement>('[aria-label="Close Fleet"]')!.click()
  expect(dock.isOpen("fleet")).toBe(false)
  expect(counter("fleet")).toBeNull()
  expect(cleanups).toBe(1)
  dock.toggle("fleet")
  expect(dock.isOpen("fleet")).toBe(true)
  expect(counter("fleet")!.textContent).toBe("fleet:0")
})

it("restores a saved layout, docks panels it never knew, and resets", () => {
  localStorage.setItem(`${DOCK_STORAGE_PREFIX}test`, JSON.stringify({ v: 1, known: ["chat"], root: { kind: "stack", tabs: ["chat"] } }))
  const { dock } = mount(createDockState("test", ["chat", "fleet"], defaults, { fleet: { zone: "bottom", share: 0.3 } }))
  expect(dock.root()).toMatchObject({ kind: "split", dir: "col", sizes: [0.7, 0.3] })
  resetAllDocks()
  expect(localStorage.getItem(`${DOCK_STORAGE_PREFIX}test`)).toBeNull()
  expect(dock.root()).toMatchObject({ kind: "split", dir: "row" })
})

it("falls back to defaults for an unreadable save and offers reset when everything is closed", () => {
  localStorage.setItem(`${DOCK_STORAGE_PREFIX}test`, "{not json")
  const { dock } = mount()
  expect(dock.root()).toMatchObject({ kind: "split", dir: "row" })
  dock.apply(() => null)
  expect(document.body.textContent).toContain("Every panel is closed.")
  document.querySelector<HTMLButtonElement>(".dock-area button")!.click()
  expect(counter("chat")).not.toBeNull()
})

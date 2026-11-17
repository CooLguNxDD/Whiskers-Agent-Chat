// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it } from "vitest"
import { createSignal } from "solid-js"
import { render } from "solid-js/web"
import { CollapsibleList } from "./CollapsibleList"
import { DOCK_STORAGE_PREFIX } from "./dockState"

let dispose: (() => void) | undefined
beforeEach(() => localStorage.clear())
afterEach(() => { dispose?.(); document.body.replaceChildren() })

const names = (count: number) => Array.from({ length: count }, (_, index) => `cat-${index}`)
const rows = () => [...document.querySelectorAll("li")].map((li) => li.textContent)
const button = (text: RegExp) => [...document.querySelectorAll("button")].find((el) => text.test(el.textContent ?? ""))!

function mount(items: () => string[], extra: { foldAbove?: number } = {}) {
  const host = document.createElement("div")
  document.body.append(host)
  dispose = render(() => (
    <CollapsibleList id="fleet" title="Your fleet" items={items()} limit={3} foldAbove={extra.foldAbove} empty={<p>empty</p>}>
      {(name) => <li>{name}</li>}
    </CollapsibleList>
  ), host)
}

it("clamps a long list to a preview with a show-all toggle", () => {
  mount(() => names(70))
  expect(rows()).toEqual(["cat-0", "cat-1", "cat-2"])
  button(/Show all 70 \(67 more\)/).click()
  expect(rows()).toHaveLength(70)
  button(/Show fewer/).click()
  expect(rows()).toHaveLength(3)
})

it("folds from the header and remembers it on this device", () => {
  mount(() => names(2))
  const head = document.querySelector<HTMLButtonElement>(".collapsible-head")!
  expect(head.getAttribute("aria-expanded")).toBe("true")
  head.click()
  expect(head.getAttribute("aria-expanded")).toBe("false")
  expect(document.querySelector<HTMLElement>("[id^=collapsible-]")!.hidden).toBe(true)
  expect(localStorage.getItem(`${DOCK_STORAGE_PREFIX}fold:fleet`)).toBe("1")
})

it("folds by default above foldAbove, including once data arrives, until the user chooses", () => {
  const [items, setItems] = createSignal<string[]>([])
  mount(items, { foldAbove: 10 })
  const head = document.querySelector<HTMLButtonElement>(".collapsible-head")!
  expect(head.getAttribute("aria-expanded")).toBe("true")
  setItems(names(40))
  expect(head.getAttribute("aria-expanded")).toBe("false")
  head.click()
  expect(head.getAttribute("aria-expanded")).toBe("true")
})

// @vitest-environment jsdom
import { afterEach, expect, it } from "vitest"
import { render } from "solid-js/web"
import { HoverCard } from "./hovercard"

let dispose: (() => void) | undefined
afterEach(() => { dispose?.(); document.body.replaceChildren() })

it("opens on focus, links the trigger, and closes on Escape", () => {
  const host = document.createElement("div")
  document.body.append(host)
  dispose = render(() => (
    <HoverCard content={() => <p>Ginger Maine Coon</p>}>
      {(trigger) => <li {...trigger} tabindex="0">ada</li>}
    </HoverCard>
  ), host)
  const li = document.querySelector("li")!
  expect(document.querySelector('[role="tooltip"]')).toBeNull()
  li.dispatchEvent(new FocusEvent("focusin", { bubbles: true }))
  const card = document.querySelector('[role="tooltip"]')!
  expect(card.textContent).toBe("Ginger Maine Coon")
  expect(li.getAttribute("aria-describedby")).toBe(card.id)
  li.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))
  expect(document.querySelector('[role="tooltip"]')).toBeNull()
  expect(li.hasAttribute("aria-describedby")).toBe(false)
})

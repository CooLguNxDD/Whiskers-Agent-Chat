import { expect, it } from "vitest"
import { catProfile } from "./catProfile"

it("gives each handle a stable, case-insensitive cat", () => {
  expect(catProfile("Codex")).toEqual(catProfile("codex"))
  const cat = catProfile("claude")
  expect(cat.kind).toMatch(/^[A-Z][\w-]+ /)
  expect(cat.trait.length).toBeGreaterThan(5)
  const kinds = new Set(["a", "b", "c", "d", "e", "f", "g", "h"].map((name) => catProfile(`agent-${name}`).kind))
  expect(kinds.size).toBeGreaterThan(1)
})

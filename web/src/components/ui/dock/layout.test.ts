import { describe, expect, it } from "vitest"
import {
  activatePanel, dockPanel, findStack, flatten, MIN_FRACTION, movePanel, panelIds, removePanel, resizeSplit, sanitize, split, stack,
  type DockNode, type SplitNode,
} from "./layout"

/** A compact picture of a tree: stacks as tab lists, splits as `dir(children)`. */
function shape(node: DockNode | null | undefined): unknown {
  if (!node) return null
  return node.kind === "stack" ? node.tabs : { [node.dir]: node.children.map(shape) }
}

const base = () => split("row", [stack(["chat"]), stack(["fleet", "notes"])], [0.75, 0.25])

describe("dock layout", () => {
  it("lists panels and finds their stacks", () => {
    const root = base()
    expect(panelIds(root)).toEqual(["chat", "fleet", "notes"])
    expect(findStack(root, "notes")?.tabs).toEqual(["fleet", "notes"])
    expect(findStack(root, "missing")).toBeNull()
  })

  it("removes a tab and collapses an emptied stack into its sibling", () => {
    const root = base()
    const one = removePanel(root, "fleet")
    expect(shape(one)).toEqual({ row: [["chat"], ["notes"]] })
    expect(findStack(one, "notes")?.active).toBe("notes")
    expect(shape(removePanel(one, "notes"))).toEqual(["chat"])
    expect(removePanel(stack(["chat"]), "chat")).toBeNull()
  })

  it("keeps untouched subtrees by reference", () => {
    const root = base()
    const next = removePanel(root, "notes") as SplitNode
    expect(next.children[0]).toBe(root.children[0])
  })

  it("docks into a stack's centre as a tab and activates it", () => {
    const root = base()
    const target = findStack(root, "chat")!.id
    const next = movePanel(root, "notes", target, "center")
    expect(shape(next)).toEqual({ row: [["chat", "notes"], ["fleet"]] })
    expect(findStack(next, "notes")?.active).toBe("notes")
  })

  it("splits a stack on an edge, inlining same-direction splits", () => {
    const root = base()
    const chat = findStack(root, "chat")!.id
    expect(shape(movePanel(root, "notes", chat, "bottom"))).toEqual({ row: [{ col: [["chat"], ["notes"]] }, ["fleet"]] })
    const sideways = movePanel(root, "notes", chat, "left") as SplitNode
    expect(shape(sideways)).toEqual({ row: [["notes"], ["chat"], ["fleet"]] })
    expect(sideways.sizes.reduce((a, b) => a + b, 0)).toBeCloseTo(1)
    expect(sideways.sizes[0]).toBeCloseTo(0.375)
  })

  it("moving a whole stack's only tab works, and dropping on itself is a no-op", () => {
    const root = base()
    const chat = findStack(root, "chat")!
    expect(movePanel(root, "chat", chat.id, "left")).toBe(root)
    const fleet = findStack(root, "fleet")!.id
    expect(shape(movePanel(root, "chat", fleet, "top"))).toEqual({ col: [["chat"], ["fleet", "notes"]] })
    expect(shape(movePanel(root, "fleet", fleet, "right"))).toEqual({ row: [["chat"], ["notes"], ["fleet"]] })
  })

  it("activates a tab", () => {
    const next = activatePanel(base(), "notes")
    expect(findStack(next, "notes")?.active).toBe("notes")
  })

  it("docks a closed panel against the layout edge", () => {
    const closed = removePanel(base(), "fleet")
    expect(shape(dockPanel(closed, "fleet", "right"))).toEqual({ row: [["chat"], ["notes"], ["fleet"]] })
    expect(shape(dockPanel(null, "fleet", "right"))).toEqual(["fleet"])
    const top = dockPanel(stack(["chat"]), "fleet", "top", 0.3) as SplitNode
    expect(shape(top)).toEqual({ col: [["fleet"], ["chat"]] })
    expect(top.sizes).toEqual([0.3, 0.7])
  })

  it("resizes neighbours from a start snapshot and clamps them", () => {
    const root = base()
    const start = [...root.sizes]
    const wider = resizeSplit(root, root.id, 0, start, 0.1) as SplitNode
    expect(wider.sizes[0]).toBeCloseTo(0.85)
    expect(wider.sizes[1]).toBeCloseTo(0.15)
    const clamped = resizeSplit(root, root.id, 0, start, 5) as SplitNode
    expect(clamped.sizes[1]).toBeCloseTo(MIN_FRACTION)
    expect(resizeSplit(root, root.id, 0, start, 0)).toBe(root)
    expect(resizeSplit(root, root.id, 1, start, 0.1)).toBe(root)
  })

  it("flattens to one stack for narrow screens", () => {
    expect(flatten(base(), "fleet")).toMatchObject({ tabs: ["chat", "fleet", "notes"], active: "fleet" })
    expect(flatten(null)).toBeNull()
  })

  it("sanitizes untrusted saved trees", () => {
    const known = new Set(["chat", "fleet"])
    const saved = JSON.parse(JSON.stringify(base()))
    expect(shape(sanitize(saved, known))).toEqual({ row: [["chat"], ["fleet"]] })
    const dupes = { kind: "split", dir: "col", sizes: [-1, "x"], children: [{ kind: "stack", tabs: ["chat", "chat"], active: "zzz" }, { kind: "stack", tabs: ["chat", "fleet"] }] }
    const clean = sanitize(dupes, known) as SplitNode
    expect(shape(clean)).toEqual({ col: [["chat"], ["fleet"]] })
    expect(clean.sizes).toEqual([0.5, 0.5])
    expect(sanitize({ kind: "stack", tabs: ["ghost"] }, known)).toBeNull()
    expect(sanitize(null, known)).toBeNull()
    for (const junk of [undefined, 3, "x", { kind: "split", dir: "diagonal", children: [] }, { kind: "stack" }]) {
      expect(sanitize(junk, known)).toBeUndefined()
    }
  })
})

describe("dockHome", () => {
  it("docks beside an open neighbour, else on the layout edge", async () => {
    const { dockHome } = await import("./dockState")
    const root = split("row", [stack(["chat"]), stack(["fleet"])])
    const near = { zone: "right" as const, share: 0.24, near: { panel: "fleet", zone: "bottom" as const } }
    expect(shape(dockHome(root, "tasks", near))).toEqual({ row: [["chat"], { col: [["fleet"], ["tasks"]] }] })
    expect(shape(dockHome(stack(["chat"]), "tasks", near))).toEqual({ row: [["chat"], ["tasks"]] })
  })
})

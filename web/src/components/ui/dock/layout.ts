/**
 * Dock layout model: a tree of splits (row/col, fractional sizes) whose leaves are tab stacks of
 * panel ids. Every operation is pure and returns a new tree, sharing untouched subtrees, so the
 * renderer can reconcile by node `id`. `null` means "no panel is open".
 */

export type DockDir = "row" | "col"
export type DropZone = "center" | "left" | "right" | "top" | "bottom"

export interface StackNode {
  kind: "stack"
  id: string
  tabs: string[]
  active: string
}

export interface SplitNode {
  kind: "split"
  id: string
  dir: DockDir
  children: DockNode[]
  /** One fraction per child; they sum to 1. */
  sizes: number[]
}

export type DockNode = StackNode | SplitNode

/** Smallest fraction a split child may shrink to. */
export const MIN_FRACTION = 0.08

let counter = 0
/** A fresh node id. Ids only need to be unique within one tree. */
export function nodeId(prefix: "s" | "p" = "s"): string {
  counter += 1
  return `${prefix}${counter.toString(36)}${Math.random().toString(36).slice(2, 6)}`
}

/** A stack holding `tabs`, showing the first unless `active` names another. */
export function stack(tabs: string[], active?: string): StackNode {
  return { kind: "stack", id: nodeId(), tabs: [...tabs], active: active && tabs.includes(active) ? active : tabs[0] }
}

/** A split of `children`; sizes default to equal shares and are normalized. */
export function split(dir: DockDir, children: DockNode[], sizes?: number[]): SplitNode {
  return { kind: "split", id: nodeId("p"), dir, children, sizes: normalizeSizes(sizes ?? children.map(() => 1), children.length) }
}

function normalizeSizes(sizes: number[], count: number): number[] {
  const usable = sizes.length === count && sizes.every((size) => Number.isFinite(size) && size > 0)
  const raw = usable ? sizes : Array.from({ length: count }, () => 1)
  const total = raw.reduce((sum, size) => sum + size, 0)
  return raw.map((size) => size / total)
}

/** Every panel id in the tree, in visual order. */
export function panelIds(root: DockNode | null): string[] {
  if (!root) return []
  return root.kind === "stack" ? [...root.tabs] : root.children.flatMap(panelIds)
}

/** The stack that holds `panelId`, if any. */
export function findStack(root: DockNode | null, panelId: string): StackNode | null {
  if (!root) return null
  if (root.kind === "stack") return root.tabs.includes(panelId) ? root : null
  for (const child of root.children) {
    const found = findStack(child, panelId)
    if (found) return found
  }
  return null
}

function findNode(root: DockNode | null, id: string): DockNode | null {
  if (!root) return null
  if (root.id === id) return root
  if (root.kind === "split") {
    for (const child of root.children) {
      const found = findNode(child, id)
      if (found) return found
    }
  }
  return null
}

/** Replaces the node `id` with `fn(node)` (null removes it), pruning and collapsing as it goes. */
function mapNode(root: DockNode, id: string, fn: (node: DockNode) => DockNode | null): DockNode | null {
  if (root.id === id) return fn(root)
  if (root.kind === "stack") return root
  let changed = false
  const children: DockNode[] = []
  const sizes: number[] = []
  root.children.forEach((child, index) => {
    const next = mapNode(child, id, fn)
    if (next !== child) changed = true
    if (next) {
      children.push(next)
      sizes.push(root.sizes[index])
    }
  })
  if (!changed) return root
  return collapse({ ...root, children, sizes: normalizeSizes(sizes, children.length) })
}

/** An empty split vanishes, a one-child split becomes its child, and same-direction children are inlined. */
function collapse(node: SplitNode): DockNode | null {
  if (node.children.length === 0) return null
  if (node.children.length === 1) return node.children[0]
  if (!node.children.some((child) => child.kind === "split" && child.dir === node.dir)) return node
  const children: DockNode[] = []
  const sizes: number[] = []
  node.children.forEach((child, index) => {
    if (child.kind === "split" && child.dir === node.dir) {
      child.children.forEach((inner, at) => { children.push(inner); sizes.push(node.sizes[index] * child.sizes[at]) })
    } else {
      children.push(child)
      sizes.push(node.sizes[index])
    }
  })
  return { ...node, children, sizes: normalizeSizes(sizes, children.length) }
}

/** Removes a panel; an emptied stack disappears and its space goes to its siblings. */
export function removePanel(root: DockNode | null, panelId: string): DockNode | null {
  const holder = findStack(root, panelId)
  if (!root || !holder) return root
  return mapNode(root, holder.id, (node) => {
    const current = node as StackNode
    const tabs = current.tabs.filter((tab) => tab !== panelId)
    if (!tabs.length) return null
    const index = current.tabs.indexOf(panelId)
    const active = current.active === panelId ? tabs[Math.min(index, tabs.length - 1)] : current.active
    return { ...current, tabs, active }
  })
}

/** Shows `panelId` in its stack. */
export function activatePanel(root: DockNode | null, panelId: string): DockNode | null {
  const holder = findStack(root, panelId)
  if (!root || !holder || holder.active === panelId) return root
  return mapNode(root, holder.id, (node) => ({ ...(node as StackNode), active: panelId }))
}

const EDGE: Record<Exclude<DropZone, "center">, { dir: DockDir; before: boolean }> = {
  left: { dir: "row", before: true },
  right: { dir: "row", before: false },
  top: { dir: "col", before: true },
  bottom: { dir: "col", before: false },
}

/** Puts `node` beside `target` on `zone`'s side, taking `share` of target's space. */
function besides(target: DockNode, node: DockNode, zone: Exclude<DropZone, "center">, share: number): SplitNode {
  const { dir, before } = EDGE[zone]
  const children = before ? [node, target] : [target, node]
  const sizes = before ? [share, 1 - share] : [1 - share, share]
  return split(dir, children, sizes)
}

/**
 * Docks `panelId` into the stack `targetStackId`: `center` adds it as a tab, an edge splits the
 * stack and opens a new one on that side. Dropping a stack's only tab onto itself is a no-op.
 */
export function movePanel(root: DockNode | null, panelId: string, targetStackId: string, zone: DropZone): DockNode | null {
  const target = findNode(root, targetStackId)
  if (!root || !target || target.kind !== "stack") return root
  const source = findStack(root, panelId)
  if (source?.id === targetStackId && (zone === "center" || target.tabs.length === 1)) {
    return zone === "center" ? activatePanel(root, panelId) : root
  }
  const without = removePanel(root, panelId)
  // The target survives removal: it is either another stack or still holds other tabs.
  if (!without) return stack([panelId])
  return mapNode(without, targetStackId, (node) => {
    const current = node as StackNode
    if (zone === "center") return { ...current, tabs: [...current.tabs, panelId], active: panelId }
    return besides(current, stack([panelId]), zone, 0.5)
  })
}

/** Docks a panel against the whole layout's `zone` edge (how a closed panel comes back). */
export function dockPanel(root: DockNode | null, panelId: string, zone: Exclude<DropZone, "center">, share = 0.25): DockNode {
  const without = removePanel(root, panelId)
  const fresh = stack([panelId])
  if (!without) return fresh
  return collapse(besides(without, fresh, zone, share)) as DockNode
}

/**
 * Sets the boundary between children `index` and `index + 1` of split `splitId` so that child
 * `index` ends `delta` (a fraction of the split) away from where `startSizes` put it. Both
 * neighbours keep at least `MIN_FRACTION`; the others are untouched.
 */
export function resizeSplit(root: DockNode | null, splitId: string, index: number, startSizes: number[], delta: number): DockNode | null {
  const node = findNode(root, splitId)
  if (!root || !node || node.kind !== "split" || index < 0 || index + 1 >= node.children.length) return root
  if (startSizes.length !== node.sizes.length) return root
  const pair = startSizes[index] + startSizes[index + 1]
  const first = Math.min(Math.max(startSizes[index] + delta, MIN_FRACTION), pair - MIN_FRACTION)
  const sizes = [...startSizes]
  sizes[index] = first
  sizes[index + 1] = pair - first
  if (sizes.every((size, at) => Math.abs(size - node.sizes[at]) < 1e-6)) return root
  return mapNode(root, splitId, (current) => ({ ...(current as SplitNode), sizes }))
}

/** All open panels as one stack: the narrow-screen view. */
export function flatten(root: DockNode | null, active?: string): StackNode | null {
  const tabs = panelIds(root)
  return tabs.length ? { kind: "stack", id: "flat", tabs, active: active && tabs.includes(active) ? active : tabs[0] } : null
}

/**
 * Rebuilds a tree from untrusted JSON (e.g. localStorage): drops unknown and duplicate panels,
 * repairs sizes and active tabs, and returns `undefined` when the shape is not a layout at all.
 */
export function sanitize(raw: unknown, known: ReadonlySet<string>): DockNode | null | undefined {
  const seen = new Set<string>()
  const ids = new Set<string>()
  const fresh = (value: unknown, prefix: "s" | "p") => {
    const id = typeof value === "string" && /^[\w-]{1,40}$/.test(value) && !ids.has(value) ? value : nodeId(prefix)
    ids.add(id)
    return id
  }
  const walk = (value: unknown, depth: number): DockNode | null | undefined => {
    if (!value || typeof value !== "object" || depth > 12) return undefined
    const node = value as Record<string, unknown>
    if (node.kind === "stack") {
      if (!Array.isArray(node.tabs)) return undefined
      const tabs: string[] = []
      for (const tab of node.tabs) {
        if (typeof tab === "string" && known.has(tab) && !seen.has(tab)) { seen.add(tab); tabs.push(tab) }
      }
      if (!tabs.length) return null
      const active = typeof node.active === "string" && tabs.includes(node.active) ? node.active : tabs[0]
      return { kind: "stack", id: fresh(node.id, "s"), tabs, active }
    }
    if (node.kind === "split") {
      if ((node.dir !== "row" && node.dir !== "col") || !Array.isArray(node.children)) return undefined
      const rawSizes = Array.isArray(node.sizes) ? node.sizes : []
      const children: DockNode[] = []
      const sizes: number[] = []
      for (const [index, child] of node.children.entries()) {
        const next = walk(child, depth + 1)
        if (next === undefined) return undefined
        if (next) {
          children.push(next)
          sizes.push(typeof rawSizes[index] === "number" ? rawSizes[index] : 1)
        }
      }
      return collapse({ kind: "split", id: fresh(node.id, "p"), dir: node.dir, children, sizes: normalizeSizes(sizes, children.length) })
    }
    return undefined
  }
  if (raw === null) return null
  return walk(raw, 0)
}

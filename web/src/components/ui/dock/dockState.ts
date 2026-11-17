import { createStore, reconcile, unwrap } from "solid-js/store"
import { dockPanel, findStack, movePanel, panelIds, removePanel, sanitize, type DockNode, type DropZone } from "./layout"

/** localStorage prefix for every saved dock layout (and the sidebar width). */
export const DOCK_STORAGE_PREFIX = "cat-fleet-dock:"

/** Where a closed panel comes back when it is toggled on again. */
export interface PanelHome {
  zone: Exclude<DropZone, "center">
  /** Fraction of the layout it takes when docked against the layout edge. */
  share?: number
  /** Preferred spot: the given side of another panel's stack, when that panel is open. */
  near?: { panel: string; zone: DropZone }
}

/** Docks `panelId` at its home: beside `near`'s stack when open, else on the layout edge. */
export function dockHome(root: DockNode | null, panelId: string, home: PanelHome = { zone: "right" }): DockNode | null {
  const neighbour = home.near && home.near.panel !== panelId ? findStack(root, home.near.panel) : null
  if (neighbour && home.near) return movePanel(root, panelId, neighbour.id, home.near.zone)
  return dockPanel(root, panelId, home.zone, home.share)
}

export interface DockState {
  readonly key: string
  /** The live tree (a Solid store; read it inside tracking scopes). */
  root: () => DockNode | null
  /** Applies a pure layout operation; `persist: false` skips the save (mid-drag). */
  apply: (op: (root: DockNode | null) => DockNode | null, persist?: boolean) => void
  save: () => void
  isOpen: (panelId: string) => boolean
  /** Closes an open panel, or docks a closed one at its `homes` edge. */
  toggle: (panelId: string) => void
  reset: () => void
}

interface Saved {
  v: 1
  root: unknown
  /** Panels that existed when this was saved, so a panel added later still opens by default. */
  known: string[]
}

const live = new Set<DockState>()

function read(key: string): Saved | null {
  try {
    const raw = localStorage.getItem(DOCK_STORAGE_PREFIX + key)
    const parsed = raw ? (JSON.parse(raw) as Saved) : null
    return parsed && parsed.v === 1 && Array.isArray(parsed.known) ? parsed : null
  } catch {
    return null
  }
}

function write(key: string, value: Saved | null) {
  try {
    if (value) localStorage.setItem(DOCK_STORAGE_PREFIX + key, JSON.stringify(value))
    else localStorage.removeItem(DOCK_STORAGE_PREFIX + key)
  } catch { /* Storage may be blocked; the layout still works in memory. */ }
}

/**
 * A dock layout for `key`, restored from this device when the saved tree is valid. Panels the
 * saved layout never knew about are docked at their `homes` edge (or the right); anything
 * unreadable falls back to `defaults()`.
 */
export function createDockState(key: string, panels: readonly string[], defaults: () => DockNode, homes: Record<string, PanelHome> = {}): DockState {
  const known = new Set(panels)
  const restore = (): DockNode | null => {
    const saved = read(key)
    if (!saved) return defaults()
    let root = sanitize(saved.root, known)
    if (root === undefined) return defaults()
    const before = new Set(saved.known)
    for (const id of panels) {
      if (before.has(id)) continue
      root = dockHome(root, id, homes[id])
    }
    return root
  }
  const [state, setState] = createStore<{ root: DockNode | null }>({ root: restore() })
  const snapshot = () => structuredClone(unwrap(state.root)) as DockNode | null

  const dock: DockState = {
    key,
    root: () => state.root,
    save() { write(key, { v: 1, root: snapshot(), known: [...panels] }) },
    apply(op, persist = true) {
      const next = op(snapshot())
      setState("root", reconcile(next, { key: "id", merge: false }))
      if (persist) dock.save()
    },
    isOpen: (panelId) => panelIds(state.root).includes(panelId),
    toggle(panelId) {
      if (!known.has(panelId)) return
      if (dock.isOpen(panelId)) dock.apply((root) => removePanel(root, panelId))
      else dock.apply((root) => dockHome(root, panelId, homes[panelId]))
    },
    reset() {
      write(key, null)
      setState("root", reconcile(defaults(), { key: "id", merge: false }))
    },
  }
  return dock
}

/** Registers a mounted dock so `resetAllDocks` can reach it; returns the unregister function. */
export function trackDock(dock: DockState): () => void {
  live.add(dock)
  return () => live.delete(dock)
}

/** Forgets every saved layout on this device and resets the mounted docks to their defaults. */
export function resetAllDocks() {
  try {
    const keys: string[] = []
    for (let index = 0; index < localStorage.length; index++) {
      const name = localStorage.key(index)
      if (name?.startsWith(DOCK_STORAGE_PREFIX)) keys.push(name)
    }
    keys.forEach((name) => localStorage.removeItem(name))
  } catch { /* Nothing saved to clear. */ }
  live.forEach((dock) => dock.reset())
  window.dispatchEvent(new CustomEvent("cat-fleet-dock-reset"))
}

/** A persisted, clamped number (e.g. the sidebar width) under the dock storage prefix. */
export function readDockNumber(key: string, fallback: number, min: number, max: number): number {
  try {
    const raw = Number(localStorage.getItem(DOCK_STORAGE_PREFIX + key))
    return Number.isFinite(raw) && raw >= min && raw <= max ? raw : fallback
  } catch {
    return fallback
  }
}

export function writeDockNumber(key: string, value: number) {
  try { localStorage.setItem(DOCK_STORAGE_PREFIX + key, String(Math.round(value))) } catch { /* Keep it in memory only. */ }
}

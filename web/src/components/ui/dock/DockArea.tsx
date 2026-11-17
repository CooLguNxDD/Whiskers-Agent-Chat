import { createEffect, createRoot, createSignal, For, getOwner, Match, onCleanup, onMount, Show, Switch, type JSX } from "solid-js"
import { Icon, type IconName } from "@/components/ui/icons"
import { activatePanel, flatten, movePanel, removePanel, resizeSplit, type DockNode, type DropZone, type SplitNode, type StackNode } from "./layout"
import { trackDock, type DockState } from "./dockState"
import { ResizeHandle } from "./ResizeHandle"

/** One dockable panel. `render` runs once per mount; its DOM moves between stacks without remounting. */
export interface PanelDef {
  id: string
  title: string
  icon?: IconName
  /** `false` hides the tab's close button (e.g. the chat transcript). Default true. */
  closable?: boolean
  render: () => JSX.Element
}

export interface DockAreaProps {
  dock: DockState
  panels: readonly PanelDef[]
  /** Below this container width all panels collapse into one tab stack. Default 900px. */
  narrowBelow?: number
  class?: string
}

const STEP = 0.03
const DRAG_TYPE = "application/x-cat-fleet-panel"

/** Which drop zone a point falls in: the outer quarter of each edge, else the centre. */
export function zoneAt(x: number, y: number, width: number, height: number): DropZone {
  const fx = width ? x / width : 0.5
  const fy = height ? y / height : 0.5
  const edges: [DropZone, number][] = [["left", fx], ["right", 1 - fx], ["top", fy], ["bottom", 1 - fy]]
  const [zone, distance] = edges.reduce((best, next) => (next[1] < best[1] ? next : best))
  return distance < 0.25 ? zone : "center"
}

/**
 * Unity-style docking: renders `dock`'s split tree with draggable splitters and tabbed stacks.
 * Drag a tab onto another stack's centre to tab it there, or onto an edge to split beside it.
 * Panel bodies are created once and re-parented, so scroll, drafts and queries survive moves.
 */
export function DockArea(props: DockAreaProps) {
  const owner = getOwner()
  const hosts = new Map<string, { el: HTMLElement; dispose: () => void }>()
  const [dragging, setDragging] = createSignal<string | null>(null)
  const [narrow, setNarrow] = createSignal(false)
  const [flatActive, setFlatActive] = createSignal<string | undefined>()
  const def = (id: string) => props.panels.find((panel) => panel.id === id)
  let container: HTMLDivElement | undefined

  onCleanup(trackDock(props.dock))
  onCleanup(() => { hosts.forEach((host) => host.dispose()); hosts.clear() })

  onMount(() => {
    if (!container || typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver(([entry]) => setNarrow(entry.contentRect.width > 0 && entry.contentRect.width < (props.narrowBelow ?? 900)))
    observer.observe(container)
    onCleanup(() => observer.disconnect())
  })

  /** The panel's persistent body element, created on first use under this component's owner. */
  function host(id: string): HTMLElement | null {
    const existing = hosts.get(id)
    if (existing) return existing.el
    const panel = def(id)
    if (!panel) return null
    const made = createRoot((dispose) => {
      const el = (<div class="dock-panel-body" data-panel={id}>{panel.render()}</div>) as HTMLElement
      return { el, dispose }
    }, owner)
    hosts.set(id, made)
    return made.el
  }

  // Close panels whose bodies are no longer in the layout so their effects stop.
  createEffect(() => {
    const open = new Set(flatten(props.dock.root())?.tabs ?? [])
    for (const [id, made] of hosts) if (!open.has(id)) { made.dispose(); hosts.delete(id) }
  })

  const end = () => setDragging(null)

  function Stack(stackProps: { node: StackNode; flat?: boolean }) {
    const [zone, setZone] = createSignal<DropZone | null>(null)
    let body: HTMLDivElement | undefined
    const active = () => (stackProps.flat ? flatActive() ?? stackProps.node.active : stackProps.node.active)
    const select = (id: string) => (stackProps.flat ? setFlatActive(id) : props.dock.apply((root) => activatePanel(root, id)))

    createEffect(() => {
      const el = host(active())
      if (body && el && el.parentElement !== body) body.replaceChildren(el)
    })

    const over = (event: DragEvent, forced?: DropZone) => {
      if (!dragging()) return
      event.preventDefault()
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move"
      if (forced) return setZone(forced)
      const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
      setZone(zoneAt(event.clientX - rect.left, event.clientY - rect.top, rect.width, rect.height))
    }
    const drop = (event: DragEvent) => {
      event.preventDefault()
      const panel = dragging()
      const where = zone()
      setZone(null)
      end()
      if (panel && where) props.dock.apply((root) => movePanel(root, panel, stackProps.node.id, where))
    }

    return (
      <div class="dock-stack" data-stack={stackProps.node.id}>
        <div
          class="dock-tabs"
          role="tablist"
          onDragOver={(event) => over(event, "center")}
          onDragLeave={() => setZone(null)}
          onDrop={drop}
        >
          <For each={stackProps.node.tabs}>
            {(id) => (
              <div class="dock-tab" role="tab" aria-selected={active() === id} data-panel={id}>
                <button
                  type="button"
                  class="dock-tab-label"
                  draggable={!stackProps.flat}
                  title={stackProps.flat ? def(id)?.title : `${def(id)?.title} — drag to dock`}
                  onClick={() => select(id)}
                  onDragStart={(event) => {
                    event.dataTransfer?.setData(DRAG_TYPE, id)
                    if (event.dataTransfer) event.dataTransfer.effectAllowed = "move"
                    setDragging(id)
                  }}
                  onDragEnd={end}
                >
                  <Show when={def(id)?.icon}>{(icon) => <Icon name={icon()} class="size-3.5" />}</Show>
                  {def(id)?.title ?? id}
                </button>
                <Show when={!stackProps.flat && def(id)?.closable !== false}>
                  <button
                    type="button"
                    class="dock-tab-close"
                    aria-label={`Close ${def(id)?.title ?? id}`}
                    title="Close panel"
                    onClick={() => props.dock.apply((root) => removePanel(root, id))}
                  >×</button>
                </Show>
              </div>
            )}
          </For>
        </div>
        <div
          class="dock-body"
          onDragOver={(event) => over(event)}
          onDragLeave={(event) => { if (!(event.currentTarget as HTMLElement).contains(event.relatedTarget as Node | null)) setZone(null) }}
          onDrop={drop}
        >
          <div class="dock-slot" ref={(node) => { body = node }} />
          <Show when={dragging() && zone()}>
            {(where) => <div class="dock-drop-preview" data-zone={where()} aria-hidden="true" />}
          </Show>
        </div>
      </div>
    )
  }

  function Split(splitProps: { node: SplitNode }) {
    let el: HTMLDivElement | undefined
    let start: number[] = []
    const extent = () => {
      const rect = el?.getBoundingClientRect()
      return (splitProps.node.dir === "row" ? rect?.width : rect?.height) || 1
    }
    const resize = (index: number, from: number[], delta: number, persist: boolean) =>
      props.dock.apply((root) => resizeSplit(root, splitProps.node.id, index, from, delta), persist)

    return (
      <div class="dock-split" data-dir={splitProps.node.dir} ref={(node) => { el = node }}>
        <For each={splitProps.node.children}>
          {(child, index) => (
            <>
              <Show when={index() > 0}>
                <ResizeHandle
                  dir={splitProps.node.dir}
                  label="Resize panels"
                  value={splitProps.node.sizes.slice(0, index()).reduce((sum, size) => sum + size, 0) * 100}
                  onStart={() => { start = [...splitProps.node.sizes] }}
                  onMove={(px) => resize(index() - 1, start, px / extent(), false)}
                  onEnd={() => props.dock.save()}
                  onStep={(direction) => resize(index() - 1, [...splitProps.node.sizes], direction * STEP, true)}
                />
              </Show>
              <div class="dock-cell" style={{ flex: `${splitProps.node.sizes[index()] ?? 0} 1 0px` }}>
                <Node node={child} />
              </div>
            </>
          )}
        </For>
      </div>
    )
  }

  function Node(nodeProps: { node: DockNode }): JSX.Element {
    return (
      <Switch>
        <Match when={nodeProps.node.kind === "stack" && (nodeProps.node as StackNode)}>{(node) => <Stack node={node()} />}</Match>
        <Match when={nodeProps.node.kind === "split" && (nodeProps.node as SplitNode)}>{(node) => <Split node={node()} />}</Match>
      </Switch>
    )
  }

  return (
    <div class={`dock-area ${props.class ?? ""}`} ref={(node) => { container = node }} data-dragging={dragging() ? "" : undefined}>
      <Show
        when={props.dock.root()}
        fallback={
          <div class="flex flex-1 flex-col items-center justify-center gap-3 text-sm text-cream-dim">
            <p>Every panel is closed.</p>
            <button type="button" class="icon-btn gap-1.5 px-3" onClick={() => props.dock.reset()}><Icon name="board" class="size-4" />Reset layout</button>
          </div>
        }
      >
        {(root) => (
          <Show when={narrow()} fallback={<Node node={root()} />}>
            <Show when={flatten(root(), flatActive())}>{(flat) => <Stack node={flat()} flat />}</Show>
          </Show>
        )}
      </Show>
    </div>
  )
}

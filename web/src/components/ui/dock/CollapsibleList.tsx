import { createSignal, For, Show, type JSX } from "solid-js"
import { DOCK_STORAGE_PREFIX } from "./dockState"

export interface CollapsibleListProps<T> {
  /** Persistence key for the folded state (device-local). */
  id: string
  title: JSX.Element
  /** Shown beside the title, e.g. a count pill. */
  badge?: JSX.Element
  /** Right side of the header, e.g. a "View all" link. */
  actions?: JSX.Element
  /** Optional line under the title. */
  description?: JSX.Element
  items: readonly T[]
  /** Rows shown before "Show all"; long lists start clamped to this. Default 8. */
  limit?: number
  /** Start folded when there are more than this many items and the user never chose. */
  foldAbove?: number
  /** Shown instead of the list when `items` is empty (and not folded). */
  empty?: JSX.Element
  /** Loading / error notes rendered above the list. */
  status?: JSX.Element
  listClass?: string
  class?: string
  children: (item: T, index: () => number) => JSX.Element
}

function readFolded(id: string): boolean | null {
  try {
    const raw = localStorage.getItem(`${DOCK_STORAGE_PREFIX}fold:${id}`)
    return raw === "1" ? true : raw === "0" ? false : null
  } catch {
    return null
  }
}

/**
 * A panel section whose header folds it (saved per device) and whose long list is clamped to a
 * preview with a "Show all N" toggle, so one long list can't bury the sections below it.
 */
export function CollapsibleList<T>(props: CollapsibleListProps<T>) {
  // Until the user chooses, a list longer than `foldAbove` folds itself (also once data arrives).
  const [choice, setChoice] = createSignal(readFolded(props.id))
  const folded = () => choice() ?? (props.foldAbove !== undefined && props.items.length > props.foldAbove)
  const [expanded, setExpanded] = createSignal(false)
  const limit = () => props.limit ?? 8
  const hidden = () => Math.max(0, props.items.length - limit())
  const shown = () => (expanded() || hidden() === 0 ? props.items : props.items.slice(0, limit()))
  const bodyId = `collapsible-${props.id.replace(/[^\w-]/g, "-")}`

  function toggle() {
    const next = !folded()
    setChoice(next)
    try { localStorage.setItem(`${DOCK_STORAGE_PREFIX}fold:${props.id}`, next ? "1" : "0") } catch { /* In memory only. */ }
  }

  return (
    <section class={`collapsible ${props.class ?? ""}`} data-folded={folded() ? "" : undefined}>
      <div class="flex items-center justify-between gap-2">
        <button type="button" class="collapsible-head" aria-expanded={!folded()} aria-controls={bodyId} onClick={toggle}>
          <span class="collapsible-chevron" aria-hidden="true">▸</span>
          <span>{props.title}</span>
          {props.badge}
        </button>
        {props.actions}
      </div>
      <div id={bodyId} hidden={folded()}>
        {props.description}
        {props.status}
        <Show when={props.items.length} fallback={props.empty}>
          <ul class={props.listClass ?? "mt-3 flex flex-col gap-1"}>
            <For each={shown()}>{(item, index) => props.children(item, index)}</For>
          </ul>
          <Show when={hidden()}>
            <button type="button" class="collapsible-more" aria-expanded={expanded()} onClick={() => setExpanded((open) => !open)}>
              {expanded() ? "Show fewer" : `Show all ${props.items.length} (${hidden()} more)`}
            </button>
          </Show>
        </Show>
      </div>
    </section>
  )
}

import { Show, type JSX, type ParentProps } from "solid-js"

/** Shared page title block: a title, an optional one-line description, and right-aligned actions. */
export function PageHeader(props: ParentProps<{ title: string; description?: JSX.Element }>) {
  return (
    <header class="page-header mb-6 flex flex-wrap items-end gap-x-6 gap-y-3">
      <div class="min-w-0 flex-1">
        <h1 class="font-display text-2xl font-semibold tracking-tight text-cream">{props.title}</h1>
        <Show when={props.description}>
          <p class="mt-1 max-w-3xl text-sm text-cream-dim">{props.description}</p>
        </Show>
      </div>
      <Show when={props.children}>
        <div class="flex flex-wrap items-center gap-2">{props.children}</div>
      </Show>
    </header>
  )
}

/** A kanban lane heading: label plus a count pill. Text stays "label (n)" for screen readers and tests. */
export function LaneTitle(props: { label: string; count: number }) {
  return (
    <h2 class="lane-title flex items-center justify-between px-1 text-xs font-semibold uppercase tracking-wider text-cream-dim">
      <span>{props.label} <span class="sr-only">({props.count})</span></span>
      <span class="lane-count" aria-hidden="true">{props.count}</span>
    </h2>
  )
}

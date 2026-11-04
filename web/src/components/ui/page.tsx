import { For, Show, type JSX, type ParentProps } from "solid-js"
import { A, useLocation } from "@solidjs/router"

export type PageStat = { value: JSX.Element; label: string; tone?: "accent" | "ok" | "warn" | "bad" }

/**
 * Shared page title block: a small accent eyebrow, a headline, a one-line
 * description, right-aligned actions and an optional row of stats.
 */
export function PageHeader(props: ParentProps<{ title: string; eyebrow?: string; description?: JSX.Element; stats?: PageStat[] }>) {
  return (
    <header class="page-header mb-7">
      <div class="flex flex-wrap items-start gap-x-6 gap-y-3">
        <div class="min-w-0 flex-1">
          <Show when={props.eyebrow}><p class="eyebrow mb-2">{props.eyebrow}</p></Show>
          <h1 class="page-title text-cream">{props.title}</h1>
          <Show when={props.description}>
            <p class="mt-2 max-w-3xl text-[15px] text-cream-dim">{props.description}</p>
          </Show>
        </div>
        <Show when={props.children}>
          <div class="flex flex-wrap items-center gap-2">{props.children}</div>
        </Show>
      </div>
      <Show when={props.stats?.length}>
        <dl class="mt-5 flex flex-wrap gap-x-7 gap-y-2">
          <For each={props.stats}>
            {(stat) => (
              <div class="flex items-baseline gap-2">
                <dt class="sr-only">{stat.label}</dt>
                <dd class="stat-value text-xl font-semibold tabular-nums" data-tone={stat.tone}>{stat.value}</dd>
                <span class="text-[13px] text-cream-dim" aria-hidden="true">{stat.label}</span>
              </div>
            )}
          </For>
        </dl>
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

/** Underlined tab strip. Each tab is a button with an optional count. */
export function TabStrip<T extends string>(props: {
  label: string
  value: T
  onChange: (value: T) => void
  tabs: { value: T; label: string; count?: number }[]
  class?: string
}) {
  return (
    <div class={`tab-strip flex flex-wrap gap-x-5 border-b border-line ${props.class ?? ""}`} role="tablist" aria-label={props.label}>
      <For each={props.tabs}>
        {(tab) => (
          <button type="button" role="tab" class="tab-strip-item" data-value={tab.value} aria-selected={props.value === tab.value} onClick={() => props.onChange(tab.value)}>
            {tab.label}
            <Show when={tab.count !== undefined}><span class="ml-1.5 text-xs text-cream-dim tabular-nums">{tab.count}</span></Show>
          </button>
        )}
      </For>
    </div>
  )
}

const INFRA = [
  { href: "/pubsub", label: "Pub/Sub" },
  { href: "/webhooks", label: "Webhooks" },
  { href: "/relays", label: "Relays" },
] as const

export const INFRA_PATHS: readonly string[] = INFRA.map((item) => item.href)

/** Sub-navigation for the Infrastructure section (Pub/Sub, Webhooks, Relays). */
export function InfraTabs() {
  const location = useLocation()
  return (
    <nav class="mb-7 flex items-center gap-1 border-b border-line pb-4" aria-label="Infrastructure">
      <For each={INFRA}>
        {(item) => (
          <A
            href={item.href}
            class="rounded-lg px-3.5 py-1.5 text-sm"
            classList={{ "infra-tab-active font-medium text-cream": location.pathname === item.href, "text-cream-dim hover:bg-ink-raised hover:text-cream": location.pathname !== item.href }}
            aria-current={location.pathname === item.href ? "page" : undefined}
          >
            {item.label}
          </A>
        )}
      </For>
    </nav>
  )
}

/** A titled help/side card for the right column of a page. */
export function SideCard(props: ParentProps<{ title: string; class?: string }>) {
  return (
    <section class={`card side-card rounded-2xl border border-line p-5 ${props.class ?? ""}`}>
      <h2 class="mb-3 text-[15px] font-semibold text-cream">{props.title}</h2>
      <div class="text-[13px] leading-relaxed text-cream-dim">{props.children}</div>
    </section>
  )
}

import { createEffect, onCleanup, Show } from "solid-js"
import { useNavigate } from "@solidjs/router"
import type { Channel } from "@/api/types"

export interface ChatHeaderProps {
  channel: Channel | undefined
  channelName: string
  findOpen: boolean
  query: string
  matchCount: number
  /** 0-based position of the current hit, or -1. */
  matchIndex: number
  onFindOpen: (open: boolean) => void
  onQuery: (query: string) => void
  /** -1 = older hit, +1 = newer hit. */
  onStep: (direction: -1 | 1) => void
  rosterOpen: boolean
  onToggleRoster: () => void
}

function isTyping(target: EventTarget | null) {
  const element = target as HTMLElement | null
  return Boolean(element && (element.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName)))
}

/**
 * Room header: name, topic and lifecycle state, plus find-in-room.
 * Ctrl/⌘+K or "/" opens find; Enter steps to an older hit, Shift+Enter newer,
 * Escape closes. Find covers the loaded window; "All history" opens Search.
 */
export default function ChatHeader(props: ChatHeaderProps) {
  const navigate = useNavigate()
  let input: HTMLInputElement | undefined

  createEffect(() => { if (props.findOpen) queueMicrotask(() => { input?.focus(); input?.select() }) })

  const onKey = (event: KeyboardEvent) => {
    const combo = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k"
    const slash = event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey && !isTyping(event.target)
    if (combo || slash) {
      event.preventDefault()
      props.onFindOpen(true)
      queueMicrotask(() => { input?.focus(); input?.select() })
    }
  }
  document.addEventListener("keydown", onKey)
  onCleanup(() => document.removeEventListener("keydown", onKey))

  const status = () => {
    if (!props.query.trim()) return ""
    if (!props.matchCount) return "No matches"
    return `${props.matchIndex + 1} / ${props.matchCount}`
  }

  return (
    <header class="chat-header flex min-h-14 flex-wrap items-center gap-x-3 gap-y-2 border-b border-line px-4 py-2">
      <div class="flex min-w-0 flex-1 items-center gap-2.5">
        <span class="channel-glyph flex size-8 shrink-0 items-center justify-center rounded-lg font-display text-lg" aria-hidden="true">#</span>
        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <h1 class="truncate text-base font-semibold text-cream">{props.channelName}</h1>
            <Show when={props.channel && props.channel.state !== "active" ? props.channel.state : undefined}>
              {(state) => <span class="state-chip rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide" data-state={state()}>{state()}</span>}
            </Show>
          </div>
          <Show when={props.channel?.topic}>
            <p class="truncate text-xs text-cream-dim" title={props.channel?.topic}>{props.channel?.topic}</p>
          </Show>
        </div>
      </div>
      <div class="flex items-center gap-1.5">
        <Show
          when={props.findOpen}
          fallback={
            <button
              type="button"
              class="search-trigger flex h-8 items-center gap-2 rounded-lg border border-line bg-ink-sunken px-2.5 text-xs text-cream-dim hover:border-brass hover:text-cream"
              onClick={() => props.onFindOpen(true)}
              aria-keyshortcuts="Control+K Meta+K /"
            >
              <span aria-hidden="true">⌕</span> Find in room
              <kbd class="kbd">Ctrl K</kbd>
            </button>
          }
        >
          <form
            class="find-bar flex h-8 items-center gap-1 rounded-lg border border-brass bg-ink-sunken pl-2 pr-1"
            role="search"
            onSubmit={(event) => event.preventDefault()}
          >
            <span aria-hidden="true" class="text-cream-dim">⌕</span>
            <input
              ref={(element) => { input = element }}
              class="h-full w-36 bg-transparent text-xs text-cream outline-none placeholder:text-cream-dim sm:w-52"
              placeholder={`Find in #${props.channelName}`}
              aria-label="Find in room"
              value={props.query}
              onInput={(event) => props.onQuery(event.currentTarget.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") { event.preventDefault(); props.onFindOpen(false) }
                else if (event.key === "Enter") { event.preventDefault(); props.onStep(event.shiftKey ? 1 : -1) }
                else if (event.key === "ArrowUp") { event.preventDefault(); props.onStep(-1) }
                else if (event.key === "ArrowDown") { event.preventDefault(); props.onStep(1) }
              }}
            />
            <span class="min-w-14 text-right font-mono text-[10px] text-cream-dim" aria-live="polite">{status()}</span>
            <button type="button" class="rounded px-1.5 text-cream-dim hover:bg-ink-raised hover:text-cream disabled:opacity-40" aria-label="Older match" disabled={!props.matchCount} onClick={() => props.onStep(-1)}>↑</button>
            <button type="button" class="rounded px-1.5 text-cream-dim hover:bg-ink-raised hover:text-cream disabled:opacity-40" aria-label="Newer match" disabled={!props.matchCount} onClick={() => props.onStep(1)}>↓</button>
            <button
              type="button"
              class="rounded px-1.5 text-[11px] text-brass hover:bg-ink-raised disabled:opacity-40"
              disabled={!props.query.trim()}
              title="Search every channel's full history"
              onClick={() => navigate(`/search?q=${encodeURIComponent(props.query.trim())}&channel=${encodeURIComponent(props.channelName)}`)}
            >
              All history
            </button>
            <button type="button" class="rounded px-1.5 text-cream-dim hover:bg-ink-raised hover:text-cream" aria-label="Close find" onClick={() => props.onFindOpen(false)}>✕</button>
          </form>
        </Show>
        <button
          type="button"
          class="hidden h-8 items-center rounded-lg border border-line px-2.5 text-xs text-cream-dim hover:border-brass hover:text-cream xl:flex"
          aria-pressed={props.rosterOpen}
          onClick={props.onToggleRoster}
          title="Show or hide recent activity"
        >
          {props.rosterOpen ? "Hide cats" : "Show cats"}
        </button>
      </div>
    </header>
  )
}

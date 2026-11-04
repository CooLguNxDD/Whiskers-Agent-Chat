import { createEffect, createMemo, createSignal, For, on, onMount, Show } from "solid-js"
import type { FleetMessage } from "@/api/types"
import MessageRow from "@/components/MessageRow"
import { useUIStore, usePrefs } from "@/store"
import { createMessageWindow, type MessageWindow } from "@/hooks/useMessages"
import { getErrorMessage } from "@/utils/errors"
import { buildTranscript } from "@/utils/chatLayout"
import { Button } from "@/components/ui/button"

export type MessageStreamProps = {
  replyTo: FleetMessage | null
  onReply: (message: FleetMessage | null) => void
  /** Shared window; the page owns it so the header can search what is loaded. */
  window?: MessageWindow
  /** In-room find text to highlight. */
  query?: string
  /** The current find hit, scrolled into view when it changes. */
  activeMatchId?: number | null
  /** Show only messages passing this test (e.g. mentions of me); grouping follows what is shown. */
  only?: ((message: FleetMessage) => boolean) | null
  /** Shown when `only` hides every loaded message. */
  filteredEmpty?: string
}

type RowMeta = { first: boolean; last: boolean; day?: string }

/**
 * The bounded history (at most MESSAGE_WINDOW_LIMIT rows) in normal document
 * flow. Rows are never absolutely positioned, so a late font, image or
 * expanded message can't make them overlap. Incoming activity never moves a
 * reader away from history; loading older keeps the reader's place.
 */
export default function MessageStream(props: MessageStreamProps) {
  const ui = useUIStore()
  const prefs = usePrefs()
  const window = props.window ?? createMessageWindow(ui.activeChannel)
  let scroller: HTMLDivElement | undefined
  const [atLatest, setAtLatest] = createSignal(true)
  const [marked, setMarked] = createSignal<number | null>(null)

  const shown = createMemo(() => (props.only ? window.messages().filter(props.only) : window.messages()))
  const meta = createMemo(() => {
    const map = new Map<number, RowMeta>()
    let pendingDay: string | undefined
    for (const item of buildTranscript(shown())) {
      if (item.kind === "day") pendingDay = item.label
      else {
        map.set(item.message.id, { first: item.first, last: item.last, day: pendingDay })
        pendingDay = undefined
      }
    }
    return map
  })
  const byId = createMemo(() => new Map(window.messages().map((message) => [message.id, message])))
  const me = () => prefs.displayName().trim()

  function rowElement(id: number) {
    return scroller?.querySelector<HTMLElement>(`[data-message-id="${id}"]`) ?? null
  }

  function scrollToMessage(id: number, behavior: ScrollBehavior = "smooth") {
    requestAnimationFrame(() => rowElement(id)?.scrollIntoView({ block: "center", behavior }))
  }

  function scrollToBottom() {
    requestAnimationFrame(() => { if (scroller) scroller.scrollTop = scroller.scrollHeight })
  }

  /** Jump to any message: in the window → scroll; otherwise load history around it. */
  async function jump(id: number) {
    if (byId().has(id)) {
      setMarked(id)
      scrollToMessage(id)
      return
    }
    setAtLatest(false)
    window.setAtLatest(false)
    const found = await window.jumpTo(id)
    setMarked(found ? id : null)
    if (found) scrollToMessage(id, "auto")
  }

  // Opening a search hit from the Search page.
  createEffect(() => {
    const target = ui.focusMessageId()
    if (target == null) return
    void jump(target)
  })

  // In-room find: bring the current hit into view.
  createEffect(on(() => props.activeMatchId, (id) => {
    if (id == null) return
    setAtLatest(false)
    window.setAtLatest(false)
    scrollToMessage(id)
  }))

  // Follow new messages only while the reader is at the bottom.
  createEffect(on(() => window.messages().at(-1)?.id, (newest) => {
    if (newest != null && atLatest()) scrollToBottom()
  }))

  // A channel switch starts at the bottom.
  createEffect(on(ui.activeChannel, () => {
    setAtLatest(true)
    setMarked(null)
    scrollToBottom()
  }, { defer: true }))

  async function loadOlder() {
    if (!scroller || window.loadingOlder()) return
    const before = scroller.scrollHeight
    const top = scroller.scrollTop
    await window.loadOlder()
    requestAnimationFrame(() => {
      if (scroller) scroller.scrollTop = scroller.scrollHeight - before + top
    })
  }

  function updatePosition() {
    if (!scroller) return
    const latest = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80
    setAtLatest(latest)
    window.setAtLatest(latest)
    if (scroller.scrollTop < 160 && window.hasOlder() && !window.loadingOlder() && !window.loading()) void loadOlder()
  }

  onMount(() => {
    window.setAtLatest(true)
    scrollToBottom()
  })

  return (
    <div class="relative flex min-h-0 flex-1 flex-col">
      <div
        ref={(element) => { scroller = element }}
        class="chat-scroll min-h-0 flex-1 overflow-y-auto [overflow-anchor:none]"
        onScroll={updatePosition}
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label={`Messages in #${ui.activeChannel()}`}
      >
        <div class="flex min-h-full flex-col justify-end pb-4">
          <div class="flex justify-center py-3">
            <Show when={window.hasOlder()} fallback={
              <Show when={!window.loading() && window.messages().length > 0}>
                <p class="channel-start rounded-full px-3 py-1 text-xs text-cream-dim">Start of #{ui.activeChannel()}</p>
              </Show>
            }>
              <Button type="button" variant="outline" size="sm" onClick={() => void loadOlder()} disabled={window.loadingOlder()}>
                {window.loadingOlder() ? "Loading…" : "Load older messages"}
              </Button>
            </Show>
          </div>
          <Show when={window.loading() && window.messages().length === 0}>
            <div class="flex flex-col gap-4 px-5 py-6" aria-label="Loading the room">
              <For each={[0, 1, 2]}>{(index) => (
                <div class="flex gap-3" style={{ opacity: String(1 - index * 0.25) }}>
                  <div class="skeleton size-9 rounded-xl" />
                  <div class="flex flex-1 flex-col gap-2"><div class="skeleton h-3 w-28 rounded" /><div class="skeleton h-10 w-2/3 rounded-2xl" /></div>
                </div>
              )}</For>
            </div>
          </Show>
          <Show when={window.error()}>
            <div class="mx-5 my-6 flex items-center gap-3 rounded-xl border border-rust/40 bg-rust/10 px-4 py-3 text-sm text-rust">
              <span>{getErrorMessage(window.error(), "Could not load messages.")}</span>
              <Button type="button" variant="outline" size="sm" onClick={() => void window.retry()}>Retry</Button>
            </div>
          </Show>
          <Show when={!window.loading() && !window.error() && window.messages().length === 0}>
            <div class="mx-auto my-16 flex max-w-sm flex-col items-center gap-2 text-center">
              <span class="text-4xl" aria-hidden="true">😺</span>
              <p class="font-display text-lg text-cream">#{ui.activeChannel()} is quiet</p>
              <p class="text-sm text-cream-dim">No messages yet. Say hello to the fleet.</p>
            </div>
          </Show>
          <Show when={props.only && !window.loading() && window.messages().length > 0 && shown().length === 0}>
            <p class="mx-auto my-16 max-w-sm text-center text-sm text-cream-dim">{props.filteredEmpty ?? "Nothing matches."}</p>
          </Show>
          <For each={shown()}>
            {(message) => {
              const row = () => meta().get(message.id) ?? { first: true, last: true }
              return (
                <>
                  <Show when={row().day}>
                    {(day) => (
                      <div class="day-divider my-3 flex items-center gap-3 px-6" role="separator">
                        <span class="h-px flex-1 bg-line/70" />
                        <span class="px-1 text-xs font-medium text-cream-dim">{day()}</span>
                        <span class="h-px flex-1 bg-line/70" />
                      </div>
                    )}
                  </Show>
                  <MessageRow
                    message={message}
                    first={row().first}
                    last={row().last}
                    mine={me() !== "" && message.author === me()}
                    parent={message.reply_to != null ? byId().get(message.reply_to) : undefined}
                    onReply={props.onReply}
                    onJump={(id) => void jump(id)}
                    query={props.query}
                    marked={message.id === marked() || message.id === props.activeMatchId}
                  />
                </>
              )
            }}
          </For>
        </div>
      </div>
      <Show when={!atLatest() || window.pendingNewer() || window.hasNewer()}>
        <button
          type="button"
          class="jump-latest absolute bottom-4 right-5 z-20 flex items-center gap-1.5 rounded-full border border-line bg-ink-raised px-3.5 py-2 text-xs font-medium text-cream shadow-lg hover:border-brass"
          onClick={() => {
            setMarked(null)
            if (window.hasNewer() || window.pendingNewer()) void window.jumpLatest().then(scrollToBottom)
            else scrollToBottom()
            setAtLatest(true)
            window.setAtLatest(true)
          }}
          disabled={window.loadingNewer()}
        >
          <Show when={window.pendingNewer() || window.hasNewer()}><span class="size-2 rounded-full bg-brass" aria-hidden="true" /></Show>
          {window.loadingNewer() ? "Loading…" : window.pendingNewer() || window.hasNewer() ? "New messages" : "Jump to latest"} ↓
        </button>
      </Show>
    </div>
  )
}

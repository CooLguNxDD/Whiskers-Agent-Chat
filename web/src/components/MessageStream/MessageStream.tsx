import { createEffect, createSignal, onMount } from "solid-js"
import { For, Show } from "solid-js"
import { createVirtualizer } from "@tanstack/solid-virtual"
import type { FleetMessage } from "@/api/types"
import MessageRow from "@/components/MessageRow"
import { useUIStore } from "@/store"
import { createMessageWindow } from "@/hooks/useMessages"
import { getErrorMessage } from "@/utils/errors"
import { Button } from "@/components/ui/button"

export type MessageStreamProps = {
  replyTo: FleetMessage | null
  onReply: (message: FleetMessage | null) => void
}

/** Bounded, virtualized history. Incoming activity never moves a reader away from history. */
export default function MessageStream(props: MessageStreamProps) {
  const ui = useUIStore()
  const window = createMessageWindow(ui.activeChannel)
  let scroller: HTMLDivElement | null = null
  const [atLatest, setAtLatest] = createSignal(true)
  const virtualizer = createVirtualizer<HTMLDivElement, HTMLElement>({
    getScrollElement: () => scroller,
    count: 0,
    estimateSize: () => 84,
    overscan: 5,
    getItemKey: (index) => window.messages()[index]?.id ?? index,
  })

  createEffect(() => {
    virtualizer.setOptions({ ...virtualizer.options, count: window.messages().length })
  })

  createEffect(() => {
    const count = window.messages().length
    if (!count || !atLatest() || !scroller) return
    queueMicrotask(() => {
      if (scroller) scroller.scrollTop = scroller.scrollHeight
    })
  })

  function updatePosition() {
    if (!scroller) return
    const latest = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80
    setAtLatest(latest)
    window.setAtLatest(latest)
  }

  onMount(() => {
    window.setAtLatest(true)
    updatePosition()
  })

  return (
    <div
      ref={(element) => { scroller = element }}
      class="min-h-0 flex-1 overflow-y-auto"
      onScroll={updatePosition}
    >
      <div class="sticky top-0 z-10 flex min-h-0 justify-center gap-2 py-2">
        <Show when={window.hasOlder()}>
          <Button type="button" variant="outline" size="sm" onClick={() => void window.loadOlder()} disabled={window.loadingOlder()}>
            {window.loadingOlder() ? "Loading…" : "Older messages"}
          </Button>
        </Show>
        <Show when={window.pendingNewer() || window.hasNewer()}>
          <Button type="button" size="sm" onClick={() => void window.jumpLatest()} disabled={window.loadingNewer()}>
            {window.loadingNewer() ? "Loading…" : "New activity · Latest"}
          </Button>
        </Show>
      </div>
      <Show when={window.loading() && window.messages().length === 0}>
        <p class="px-4 py-8 text-sm text-cream-dim">Loading the room…</p>
      </Show>
      <Show when={window.error()}>
        <div class="flex items-center gap-3 px-4 py-8 text-sm text-rust">
          <span>{getErrorMessage(window.error(), "Could not load messages.")}</span>
          <Button type="button" variant="outline" size="sm" onClick={() => void window.retry()}>Retry</Button>
        </div>
      </Show>
      <Show when={!window.loading() && !window.error() && window.messages().length === 0}>
        <p class="px-4 py-16 text-center text-sm text-cream-dim">No messages yet. Say hello to the fleet.</p>
      </Show>
      <div class="relative mx-auto max-w-3xl" style={{ height: `${virtualizer.getTotalSize()}px` }}>
        <For each={virtualizer.getVirtualItems()}>
          {(virtualRow) => {
            const message = () => window.messages()[virtualRow.index]
            return (
              <div
                ref={(element) => virtualizer.measureElement(element)}
                data-index={virtualRow.index}
                class="absolute left-0 top-0 w-full"
                style={{ transform: `translateY(${virtualRow.start}px)` }}
              >
                <Show when={message()}>
                  {(item) => <MessageRow message={item()} onReply={props.onReply} />}
                </Show>
              </div>
            )
          }}
        </For>
      </div>
    </div>
  )
}

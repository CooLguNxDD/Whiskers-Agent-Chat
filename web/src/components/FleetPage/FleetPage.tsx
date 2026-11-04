import { createEffect, createMemo, createSignal, on, Show } from "solid-js"
import type { FleetMessage } from "@/api/types"
import ChannelList from "@/components/ChannelList"
import Composer from "@/components/Composer"
import MessageStream from "@/components/MessageStream"
import AgentRoster from "@/components/AgentRoster"
import { useActiveChannel } from "@/hooks/useChannels"
import { createMessageWindow } from "@/hooks/useMessages"
import { usePrefs, useUIStore } from "@/store"
import { findMatches } from "@/utils/chatLayout"
import { formatStamp } from "@/utils/time"
import ChatHeader from "./ChatHeader"

export type FleetPageProps = Record<string, never>

/** The room: channels, a header with find-in-room, the live transcript, a composer, and recent cats. */
export default function FleetPage() {
  const ui = useUIStore()
  const prefs = usePrefs()
  const [replyTo, setReplyTo] = createSignal<FleetMessage | null>(null)
  const active = useActiveChannel()
  const window = createMessageWindow(ui.activeChannel)
  const [findOpen, setFindOpen] = createSignal(false)
  const [query, setQuery] = createSignal("")
  const [matchIndex, setMatchIndex] = createSignal(-1)
  const [rosterOpen, setRosterOpen] = createSignal(true)

  const matches = createMemo(() => (findOpen() ? findMatches(window.messages(), query()) : []))
  // A new query starts at the newest hit.
  createEffect(on(query, () => setMatchIndex(matches().length - 1)))
  createEffect(on(() => matches().length, (count) => { if (matchIndex() >= count) setMatchIndex(count - 1) }))
  createEffect(on(ui.activeChannel, () => { setQuery(""); setMatchIndex(-1) }, { defer: true }))

  const activeMatchId = () => {
    const index = matchIndex()
    return index >= 0 ? matches()[index] ?? null : null
  }

  function step(direction: -1 | 1) {
    const count = matches().length
    if (!count) return
    setMatchIndex((index) => (index + direction + count) % count)
  }

  function openFind(open: boolean) {
    setFindOpen(open)
    if (!open) { setQuery(""); setMatchIndex(-1); ui.requestComposerFocus() }
  }

  return (
    <div
      class="grid min-h-0 flex-1 grid-cols-1 animate-[fade-in_160ms_ease-out] md:grid-cols-[248px_minmax(0,1fr)]"
      classList={{ "xl:grid-cols-[248px_minmax(0,1fr)_232px]": rosterOpen() }}
    >
      <ChannelList />
      <section class="chat-surface flex min-h-0 min-w-0 flex-col" data-bg={prefs.chatBackground()}>
        <ChatHeader
          channel={active()}
          channelName={ui.activeChannel()}
          findOpen={findOpen()}
          query={query()}
          matchCount={matches().length}
          matchIndex={matchIndex()}
          onFindOpen={openFind}
          onQuery={setQuery}
          onStep={step}
          rosterOpen={rosterOpen()}
          onToggleRoster={() => setRosterOpen((open) => !open)}
        />
        <Show when={active() && active()!.state !== "active" ? active() : undefined}>
          {(channel) => (
            <p class="state-banner border-b border-line px-4 py-2 text-xs text-cream-dim" data-state={channel().state} role="status">
              <span class="font-semibold uppercase tracking-wide">{channel().state}</span>
              {" · "}#{channel().name}
              <Show when={channel().state_updated_by}>
                {" "}set by {channel().state_updated_by} on {formatStamp(channel().state_updated_at ?? "")}
              </Show>
              <Show when={channel().state_note}>{" — "}{channel().state_note}</Show>
              <Show when={channel().state === "archived"}>{". History is read-only."}</Show>
            </p>
          )}
        </Show>
        <MessageStream
          window={window}
          replyTo={replyTo()}
          onReply={setReplyTo}
          query={findOpen() ? query() : ""}
          activeMatchId={activeMatchId()}
        />
        <Composer replyTo={replyTo()} onClearReply={() => setReplyTo(null)} />
      </section>
      <Show when={rosterOpen()}><AgentRoster /></Show>
    </div>
  )
}

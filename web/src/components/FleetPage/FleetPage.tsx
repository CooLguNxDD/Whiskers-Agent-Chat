import { createEffect, createMemo, createSignal, on, Show } from "solid-js"
import type { FleetMessage } from "@/api/types"
import Composer from "@/components/Composer"
import MessageStream from "@/components/MessageStream"
import { fleetPanel, inProgressPanel } from "@/components/panels"
import { createDockState, DockArea, split, stack, type PanelDef, type PanelHome } from "@/components/ui/dock"
import { useActiveChannel } from "@/hooks/useChannels"
import { createMessageWindow } from "@/hooks/useMessages"
import { usePrefs, useUIStore } from "@/store"
import { findMatches } from "@/utils/chatLayout"
import { formatStamp } from "@/utils/time"
import ChannelSettings from "./ChannelSettings"
import ChatHeader, { type StreamFilter } from "./ChatHeader"

export type FleetPageProps = Record<string, never>

/** Default chat layout: transcript on the left; fleet roster over in-progress tasks on the right. */
export const chatDefaultLayout = () =>
  split("row", [stack(["chat"]), split("col", [stack(["fleet"]), stack(["in-progress"])], [0.6, 0.4])], [0.76, 0.24])

const CHAT_PANELS = ["chat", "fleet", "in-progress"]
/** Where a re-opened panel docks: beside its sibling, else the right edge at 24%. */
const CHAT_HOMES: Record<string, PanelHome> = {
  fleet: { zone: "right", share: 0.24, near: { panel: "in-progress", zone: "top" } },
  "in-progress": { zone: "right", share: 0.24, near: { panel: "fleet", zone: "bottom" } },
}

/**
 * The room as a dock (panel kit): the Chat panel (header with tabs and find, the live transcript,
 * a composer), Fleet and In progress, resizable and re-dockable; the layout is saved per device.
 * Channels live in the app sidebar.
 */
export default function FleetPage() {
  const ui = useUIStore()
  const prefs = usePrefs()
  const [replyTo, setReplyTo] = createSignal<FleetMessage | null>(null)
  const active = useActiveChannel()
  const window = createMessageWindow(ui.activeChannel)
  const [findOpen, setFindOpen] = createSignal(false)
  const [query, setQuery] = createSignal("")
  const [matchIndex, setMatchIndex] = createSignal(-1)
  const dock = createDockState("chat", CHAT_PANELS, chatDefaultLayout, CHAT_HOMES)
  const [settingsOpen, setSettingsOpen] = createSignal(false)
  const [filter, setFilter] = createSignal<StreamFilter>("all")
  const me = () => prefs.displayName().trim()
  const mentionsMe = (message: FleetMessage) => me() !== "" && message.mentions.includes(me())
  const mentionCount = createMemo(() => window.messages().filter(mentionsMe).length)

  const matches = createMemo(() => (findOpen() ? findMatches(window.messages(), query()) : []))
  // A new query starts at the newest hit.
  createEffect(on(query, () => setMatchIndex(matches().length - 1)))
  createEffect(on(() => matches().length, (count) => { if (matchIndex() >= count) setMatchIndex(count - 1) }))
  createEffect(on(ui.activeChannel, () => { setQuery(""); setMatchIndex(-1); setFilter("all") }, { defer: true }))

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

  const chatPanel: PanelDef = { id: "chat", title: "Chat", icon: "chat", closable: false, render: () => (
      <section class="chat-surface flex min-h-0 min-w-0 flex-col" data-bg={prefs.chatBackground()}>
        <ChatHeader
          channel={active()}
          channelName={ui.activeChannel()}
          filter={filter()}
          onFilter={setFilter}
          mentionCount={mentionCount()}
          onSettings={() => setSettingsOpen(true)}
          findOpen={findOpen()}
          query={query()}
          matchCount={matches().length}
          matchIndex={matchIndex()}
          onFindOpen={openFind}
          onQuery={setQuery}
          onStep={step}
          rosterOpen={dock.isOpen("fleet")}
          onToggleRoster={() => dock.toggle("fleet")}
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
          only={filter() === "mentions" ? mentionsMe : null}
          filteredEmpty={me() ? `No one has mentioned @${me()} in the loaded history.` : "Set a display name to see your mentions."}
        />
        <Composer replyTo={replyTo()} onClearReply={() => setReplyTo(null)} />
      </section>
  ) }

  return (
    <div class="flex min-h-0 flex-1 animate-[fade-in_160ms_ease-out]">
      <DockArea dock={dock} panels={[chatPanel, fleetPanel, inProgressPanel]} />
      <ChannelSettings channel={active()} open={settingsOpen()} onOpenChange={setSettingsOpen} />
    </div>
  )
}

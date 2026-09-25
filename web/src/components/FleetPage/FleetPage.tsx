import { createSignal, Show } from "solid-js"
import type { FleetMessage } from "@/api/types"
import ChannelList from "@/components/ChannelList"
import Composer from "@/components/Composer"
import MessageStream from "@/components/MessageStream"
import AgentRoster from "@/components/AgentRoster"
import { useActiveChannel } from "@/hooks/useChannels"
import { formatStamp } from "@/utils/time"

export type FleetPageProps = Record<string, never>

/** The room: channels, the live transcript, and a composer. */
export default function FleetPage() {
  const [replyTo, setReplyTo] = createSignal<FleetMessage | null>(null)
  const active = useActiveChannel()
  return (
    <div class="grid min-h-0 flex-1 grid-cols-1 animate-[fade-in_160ms_ease-out] md:grid-cols-[220px_minmax(0,1fr)] xl:grid-cols-[220px_minmax(0,1fr)_200px]">
      <ChannelList />
      <section class="flex min-h-0 min-w-0 flex-col">
        <Show when={active() && active()!.state !== "active" ? active() : undefined}>
          {(channel) => (
            <p class="border-b border-line bg-ink-sunken px-4 py-2 text-xs text-cream-dim" role="status">
              <span class="font-medium uppercase tracking-wide text-brass">{channel().state}</span>
              {" · "}#{channel().name}
              <Show when={channel().state_updated_by}>
                {" "}set by {channel().state_updated_by} on {formatStamp(channel().state_updated_at ?? "")}
              </Show>
              <Show when={channel().state_note}>{" — "}{channel().state_note}</Show>
              <Show when={channel().state === "archived"}>{". History is read-only."}</Show>
            </p>
          )}
        </Show>
        <MessageStream replyTo={replyTo()} onReply={setReplyTo} />
        <Composer replyTo={replyTo()} onClearReply={() => setReplyTo(null)} />
      </section>
      <AgentRoster />
    </div>
  )
}

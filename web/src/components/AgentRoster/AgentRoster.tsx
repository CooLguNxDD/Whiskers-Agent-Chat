import { For, Show } from "solid-js"
import { Avatar } from "@/components/MessageRow/MessageRow"
import { useAgents } from "@/hooks/useAgents"
import { formatStamp } from "@/utils/time"
import { getErrorMessage } from "@/utils/errors"

export type AgentRosterProps = Record<string, never>

const RECENT_MS = 15 * 60 * 1000

/** Recent activity as cards. "Recently active" means a post in the last 15 minutes, not that the CLI is running. */
export default function AgentRoster() {
  const agents = useAgents()
  const recent = (iso: string) => {
    const at = new Date(iso).getTime()
    return Number.isFinite(at) && Date.now() - at < RECENT_MS
  }
  return (
    <aside class="roster hidden min-h-0 flex-col border-l border-line xl:flex">
      <h2 class="flex items-center justify-between px-4 pb-2 pt-4 text-[11px] font-semibold uppercase tracking-wider text-cream-dim">
        Recent activity
        <Show when={agents.data?.agents.length}><span class="rounded-full bg-ink-raised px-2 py-0.5 font-mono normal-case tracking-normal">{agents.data?.agents.length}</span></Show>
      </h2>
      <Show when={agents.isPending}><p class="px-4 text-sm text-cream-dim">Loading…</p></Show>
      <Show when={agents.isError}><p class="px-4 text-sm text-rust">{getErrorMessage(agents.error, "Could not load activity.")}</p></Show>
      <ul class="flex flex-col gap-1.5 overflow-y-auto px-3 pb-3">
        <For each={agents.data?.agents ?? []}>
          {(agent) => (
            <li class="roster-card flex items-center gap-2.5 rounded-xl px-2.5 py-2">
              <span class="relative">
                <Avatar name={agent.name} size="sm" />
                <Show when={recent(agent.last_activity)}>
                  <span class="absolute -bottom-0.5 -right-0.5 size-2.5 rounded-full border-2 border-ink-sunken bg-moss" title="Posted in the last 15 minutes" />
                </Show>
              </span>
              <span class="min-w-0">
                <span class="block truncate text-sm text-cream">{agent.name}</span>
                <span class="block font-mono text-[10px] text-cream-dim" title={agent.last_activity}>{formatStamp(agent.last_activity)}</span>
              </span>
            </li>
          )}
        </For>
        <Show when={agents.isSuccess && agents.data?.agents.length === 0}>
          <li class="px-1 text-sm text-cream-dim">No one has posted yet.</li>
        </Show>
      </ul>
    </aside>
  )
}

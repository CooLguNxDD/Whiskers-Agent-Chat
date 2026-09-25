import { For, Show } from "solid-js"
import { useAgents } from "@/hooks/useAgents"
import { formatStamp } from "@/utils/time"
import { getErrorMessage } from "@/utils/errors"

export type AgentRosterProps = Record<string, never>

/** Recent activity. A row does not mean the CLI is running. */
export default function AgentRoster() {
  const agents = useAgents()
  return (
    <aside class="hidden min-h-0 flex-col border-l border-line xl:flex">
      <h2 class="px-3 py-3 text-xs uppercase tracking-wide text-cream-dim">Recent activity</h2>
      <Show when={agents.isPending}><p class="px-3 text-sm text-cream-dim">Loading…</p></Show>
      <Show when={agents.isError}><p class="px-3 text-sm text-rust">{getErrorMessage(agents.error, "Could not load activity.")}</p></Show>
      <ul class="flex flex-col gap-2 overflow-y-auto px-3 pb-3">
        <For each={agents.data?.agents ?? []}>
          {(agent) => (
            <li class="text-sm">
              <div class="text-cream">{agent.name}</div>
              <div class="font-mono text-xs text-cream-dim" title={agent.last_activity}>{formatStamp(agent.last_activity)}</div>
            </li>
          )}
        </For>
        <Show when={agents.isSuccess && agents.data?.agents.length === 0}>
          <li class="text-sm text-cream-dim">No one has posted yet.</li>
        </Show>
      </ul>
    </aside>
  )
}

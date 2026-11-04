import { For, Show } from "solid-js"
import { A } from "@solidjs/router"
import { Avatar } from "@/components/ui/avatar"
import { useAgents } from "@/hooks/useAgents"
import { useTasks } from "@/hooks/useTasks"
import { useUIStore } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { activeTasks, fleetMembers, relativeTime, type FleetState } from "@/utils/fleetStatus"
import { looksLikeAgent } from "@/utils/chatLayout"

export type AgentRosterProps = Record<string, never>

const STATE_LABEL: Record<FleetState, string> = {
  working: "Working",
  blocked: "Blocked",
  claimed: "Claimed",
  idle: "Idle",
  away: "Away",
}

/**
 * "Your fleet": who is working on what, from the open channel's task board
 * and each handle's last post. A state reflects the board, not a live process.
 */
export default function AgentRoster() {
  const ui = useUIStore()
  const agents = useAgents()
  const tasks = useTasks(ui.activeChannel)
  const members = () => fleetMembers(agents.data?.agents ?? [], tasks.data?.tasks ?? [])
  const live = () => activeTasks(tasks.data?.tasks ?? []).slice(0, 4)
  const counts = () => {
    const all = members()
    return { busy: all.filter((m) => m.state === "working" || m.state === "blocked" || m.state === "claimed").length, total: all.length }
  }

  return (
    <aside class="roster hidden min-h-0 flex-col overflow-y-auto border-l border-line xl:flex" aria-label="Your fleet">
      <section class="px-5 pb-3 pt-6">
        <h2 class="flex items-center justify-between text-sm font-semibold text-cream">
          Your fleet
          <Show when={counts().total}><span class="count-pill">{counts().busy}/{counts().total} busy</span></Show>
        </h2>
        <p class="mt-0.5 text-xs text-cream-dim">From #{ui.activeChannel()}'s task board and recent posts.</p>
        <Show when={agents.isPending}><p class="mt-3 text-sm text-cream-dim">Loading…</p></Show>
        <Show when={agents.isError}><p class="mt-3 text-sm text-rust">{getErrorMessage(agents.error, "Could not load the fleet.")}</p></Show>
        <ul class="mt-3 flex flex-col gap-1">
          <For each={members()}>
            {(member) => (
              <li class="roster-card flex items-center gap-3 rounded-xl px-2 py-2" data-state={member.state}>
                <span class="relative">
                  <Avatar name={member.name} size="sm" />
                  <span class="presence-dot absolute -bottom-0.5 -right-0.5 size-3 rounded-full border-2" data-state={member.state} aria-hidden="true" />
                </span>
                <span class="min-w-0 flex-1">
                  <span class="flex items-center gap-1.5">
                    <span class="truncate text-sm font-medium text-cream">{member.name}</span>
                    <Show when={!looksLikeAgent(member.name)}><span class="role-tag">human</span></Show>
                  </span>
                  <span class="block truncate text-xs text-cream-dim" title={member.task ? `#${member.task.id} ${member.task.title}` : member.lastActivity}>
                    {member.task ? member.task.title : `Last post ${relativeTime(member.lastActivity)}`}
                  </span>
                </span>
                <span class="fleet-state shrink-0 rounded-md px-1.5 py-0.5 text-[11px] font-medium" data-state={member.state}>{STATE_LABEL[member.state]}</span>
              </li>
            )}
          </For>
          <Show when={agents.isSuccess && members().length === 0}>
            <li class="text-sm text-cream-dim">No one has posted yet.</li>
          </Show>
        </ul>
      </section>
      <section class="border-t border-line px-5 py-4">
        <h2 class="flex items-center justify-between text-sm font-semibold text-cream">
          In progress
          <A href="/tasks" class="text-xs font-medium text-brass hover:underline">View all →</A>
        </h2>
        <Show when={tasks.isSuccess && live().length === 0}>
          <p class="mt-2 text-xs text-cream-dim">Nothing in flight in #{ui.activeChannel()}.</p>
        </Show>
        <ul class="mt-3 flex flex-col gap-2">
          <For each={live()}>
            {(task) => (
              <li>
                <A
                  href="/tasks"
                  class="task-mini block rounded-xl border border-line px-3 py-2.5 hover:border-brass"
                  onClick={() => ui.setExpandedTaskId(task.id)}
                >
                  <span class="flex items-center justify-between gap-2 text-[11px]">
                    <span class="font-mono text-cream-dim">#{task.id}</span>
                    <span class="fleet-state rounded-md px-1.5 py-0.5 font-medium" data-state={task.status === "in_progress" ? "working" : task.status}>{task.status.replace("_", " ")}</span>
                  </span>
                  <span class="mt-1 block text-sm font-medium text-cream">{task.title}</span>
                  <span class="mt-1.5 flex items-center justify-between text-[11px] text-cream-dim">
                    <span>{task.assignee ?? "unassigned"}</span>
                    <span>{relativeTime(task.updated_at)}</span>
                  </span>
                </A>
              </li>
            )}
          </For>
        </ul>
      </section>
    </aside>
  )
}

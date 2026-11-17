import { Show } from "solid-js"
import { A } from "@solidjs/router"
import { Avatar } from "@/components/ui/avatar"
import { CollapsibleList } from "@/components/ui/dock"
import { HoverCard } from "@/components/ui/hovercard"
import type { FleetTask } from "@/api/types"
import { catProfile } from "@/utils/catProfile"
import { useAgents } from "@/hooks/useAgents"
import { useTasks } from "@/hooks/useTasks"
import { useUIStore } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { activeTasks, fleetMembers, relativeTime, type FleetMember, type FleetState } from "@/utils/fleetStatus"
import { looksLikeAgent } from "@/utils/chatLayout"

export type AgentRosterProps = Record<string, never>

const STATE_LABEL: Record<FleetState, string> = {
  working: "Working",
  blocked: "Blocked",
  claimed: "Claimed",
  idle: "Idle",
  away: "Away",
}

/** Hover card for a fleet member: a decorative cat persona over the member's real board state. */
function MemberCard(props: { member: FleetMember; open: FleetTask[] }) {
  const cat = () => catProfile(props.member.name)
  const human = () => !looksLikeAgent(props.member.name)
  return (
    <div class="flex flex-col gap-2.5">
      <div class="flex items-center gap-3">
        <span class="hovercard-face" aria-hidden="true">{cat().face}</span>
        <span class="min-w-0">
          <span class="block truncate text-sm font-semibold text-cream">{props.member.name}</span>
          <span class="block text-xs text-cream-dim">{human() ? "Human · fleet commander" : cat().kind}</span>
        </span>
        <span class="fleet-state ml-auto shrink-0 rounded-md px-1.5 py-0.5 text-[11px] font-medium" data-state={props.member.state}>{STATE_LABEL[props.member.state]}</span>
      </div>
      <p class="text-xs italic text-cream-dim">{human() ? "Hands out the treats (and the tasks)." : `This cat ${cat().trait}. Favourite nap spot: ${cat().spot}.`}</p>
      <dl class="hovercard-facts">
        <dt>Working on</dt>
        <dd>{props.member.task ? `#${props.member.task.id} ${props.member.task.title}` : "Nothing claimed"}</dd>
        <dt>Open tasks</dt>
        <dd>{props.open.length}</dd>
        <dt>Last post</dt>
        <dd>{relativeTime(props.member.lastActivity)}</dd>
      </dl>
    </div>
  )
}

/** Fleet rows shown before "Show all"; members are sorted busy-first, so the preview is who is working. */
export const ROSTER_PREVIEW = 8

/**
 * "Your fleet": who is working on what, from the open channel's task board
 * and each handle's last post. A state reflects the board, not a live process.
 * A long fleet is clamped to a busy-first preview so the panel stays scannable.
 */
export default function AgentRoster() {
  const ui = useUIStore()
  const agents = useAgents()
  const tasks = useTasks(ui.activeChannel)
  const members = () => fleetMembers(agents.data?.agents ?? [], tasks.data?.tasks ?? [])
  const openFor = (name: string) => activeTasks(tasks.data?.tasks ?? []).filter((task) => task.assignee === name)
  const counts = () => {
    const all = members()
    return { busy: all.filter((m) => m.state === "working" || m.state === "blocked" || m.state === "claimed").length, total: all.length }
  }

  return (
    <aside class="roster flex min-h-0 flex-1 flex-col overflow-y-auto" aria-label="Your fleet">
      <CollapsibleList
        id="roster-fleet"
        class="px-5 pb-3 pt-5"
        title="Your fleet"
        badge={<Show when={counts().total}><span class="count-pill">{counts().busy}/{counts().total} busy</span></Show>}
        description={<p class="mt-0.5 text-xs text-cream-dim">From #{ui.activeChannel()}'s task board and recent posts.</p>}
        status={<>
          <Show when={agents.isPending}><p class="mt-3 text-sm text-cream-dim">Loading…</p></Show>
          <Show when={agents.isError}><p class="mt-3 text-sm text-rust">{getErrorMessage(agents.error, "Could not load the fleet.")}</p></Show>
        </>}
        items={members()}
        limit={ROSTER_PREVIEW}
        empty={<Show when={agents.isSuccess}><p class="mt-3 text-sm text-cream-dim">No one has posted yet.</p></Show>}
      >
        {(member) => (
          <HoverCard content={() => <MemberCard member={member} open={openFor(member.name)} />}>
            {(trigger) => (
          <li {...trigger} tabindex="0" class="roster-card flex items-center gap-3 rounded-xl px-2 py-2" data-state={member.state}>
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
          </HoverCard>
        )}
      </CollapsibleList>
    </aside>
  )
}

/** The open channel's in-flight tasks (the "In progress" dock panel), newest first, clamped to a preview. */
export function InProgressTasks() {
  const ui = useUIStore()
  const tasks = useTasks(ui.activeChannel)
  const live = () => activeTasks(tasks.data?.tasks ?? [])

  return (
    <aside class="roster flex min-h-0 flex-1 flex-col overflow-y-auto" aria-label="In progress">
      <CollapsibleList
        id="roster-in-progress"
        class="px-5 py-4"
        title="In progress"
        badge={<Show when={live().length}><span class="count-pill">{live().length}</span></Show>}
        actions={<A href="/tasks" class="text-xs font-medium text-brass hover:underline">View all →</A>}
        items={live()}
        limit={6}
        listClass="mt-3 flex flex-col gap-2"
        empty={<Show when={tasks.isSuccess}><p class="mt-2 text-xs text-cream-dim">Nothing in flight in #{ui.activeChannel()}.</p></Show>}
      >
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
      </CollapsibleList>
    </aside>
  )
}

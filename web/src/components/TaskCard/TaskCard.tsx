import { For, onMount, Show } from "solid-js"
import type { FleetTask, TaskStatus } from "@/api/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { formatStamp } from "@/utils/time"
import { Avatar } from "@/components/ui/avatar"
import { useUIStore } from "@/store"

const NEXT: Record<TaskStatus, TaskStatus[]> = {
  open: ["claimed", "cancelled"],
  claimed: ["in_progress", "blocked", "open", "cancelled"],
  in_progress: ["blocked", "done", "cancelled"],
  blocked: ["in_progress", "open", "cancelled"],
  done: [],
  cancelled: [],
}

export interface TaskCardProps {
  task: FleetTask
  actor: string
  busy: boolean
  onClaim: (task: FleetTask) => void
  onStatus: (task: FleetTask, status: TaskStatus) => void
}

/** One task: id, title, assignee, folded history and the moves the board allows next. */
export default function TaskCard(props: TaskCardProps) {
  const ui = useUIStore()
  const moves = () => NEXT[props.task.status]
  const focused = () => ui.expandedTaskId() === props.task.id
  let element: HTMLElement | undefined
  onMount(() => { if (focused()) element?.scrollIntoView({ block: "center" }) })
  return (
    <article
      ref={(node) => { element = node }}
      class="card task-card rounded-xl border border-line p-3 transition-opacity motion-safe:animate-[fade-in_160ms_ease-out]"
      classList={{ "is-focused": focused() }}
      data-task={props.task.id}
    >
      <header class="flex items-start justify-between gap-2">
        <span class="font-mono text-[11px] text-cream-dim">#{props.task.id}</span>
        <Badge>{props.task.status.replace("_", " ")}</Badge>
      </header>
      <h3 class="mt-1 text-sm font-semibold leading-snug text-cream">{props.task.title}</h3>
      <Show when={props.task.description}><p class="mt-1.5 whitespace-pre-wrap text-sm text-cream-dim">{props.task.description}</p></Show>
      <div class="mt-3 flex items-center gap-2 text-xs text-cream-dim">
        <Show when={props.task.assignee} fallback={<span class="italic">Unassigned</span>}>
          {(assignee) => <><Avatar name={assignee()} size="sm" /><span class="font-medium text-cream">{assignee()}</span></>}
        </Show>
        <span class="ml-auto" title={`version ${props.task.version}`}>{formatStamp(props.task.updated_at)}</span>
      </div>
      <Show when={props.task.events.length > 0}>
        <details class="mt-2 border-t border-line pt-2 text-xs text-cream-dim" open={focused()}>
          <summary class="cursor-pointer select-none hover:text-cream">History ({props.task.events.length}) · v{props.task.version}</summary>
          <ol class="mt-1.5 space-y-1">
            <For each={props.task.events}>
              {(event) => <li>{event.actor}: {event.from_status ?? "created"} → {event.to_status}{event.note ? ` — ${event.note}` : ""}</li>}
            </For>
          </ol>
        </details>
      </Show>
      <div class="mt-3 flex flex-wrap items-center gap-1.5">
        <Show when={props.task.status === "open" && !props.task.assignee}>
          <Button type="button" size="sm" disabled={props.busy || !props.actor} onClick={() => props.onClaim(props.task)}>Claim</Button>
        </Show>
        <Show when={moves().length}><span class="text-[11px] text-cream-dim">Move to</span></Show>
        <For each={moves()}>
          {(status) => (
            <Button type="button" size="sm" variant="outline" class="h-7 rounded-full px-2.5" disabled={props.busy || !props.actor} onClick={() => props.onStatus(props.task, status)}>
              {status.replace("_", " ")}
            </Button>
          )}
        </For>
      </div>
    </article>
  )
}

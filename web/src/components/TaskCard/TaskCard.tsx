import { For, Show } from "solid-js"
import type { FleetTask, TaskStatus } from "@/api/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { formatStamp } from "@/utils/time"

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

export default function TaskCard(props: TaskCardProps) {
  const moves = () => NEXT[props.task.status]
  return (
    <article class="rounded-xl border border-line bg-ink-sunken p-3 transition-opacity motion-safe:animate-[fade-in_160ms_ease-out]">
      <header class="flex items-start justify-between gap-2">
        <h3 class="text-sm font-medium text-cream">{props.task.title}</h3>
        <Badge>{props.task.status.replace("_", " ")}</Badge>
      </header>
      <Show when={props.task.description}><p class="mt-2 whitespace-pre-wrap text-sm text-cream-dim">{props.task.description}</p></Show>
      <p class="mt-2 text-xs text-cream-dim">
        {props.task.assignee ? `Assignee ${props.task.assignee}` : "Unassigned"} · {formatStamp(props.task.updated_at)} · v{props.task.version}
      </p>
      <Show when={props.task.events.length > 0}>
        <ol class="mt-2 space-y-1 border-t border-line pt-2 text-xs text-cream-dim">
          <For each={props.task.events}>
            {(event) => <li>{event.actor}: {event.from_status ?? "created"} → {event.to_status}</li>}
          </For>
        </ol>
      </Show>
      <div class="mt-3 flex flex-wrap gap-2">
        <Show when={props.task.status === "open" && !props.task.assignee}>
          <Button type="button" size="sm" disabled={props.busy || !props.actor} onClick={() => props.onClaim(props.task)}>Claim</Button>
        </Show>
        <For each={moves()}>
          {(status) => (
            <Button type="button" size="sm" variant="outline" disabled={props.busy || !props.actor} onClick={() => props.onStatus(props.task, status)}>
              {status.replace("_", " ")}
            </Button>
          )}
        </For>
      </div>
    </article>
  )
}

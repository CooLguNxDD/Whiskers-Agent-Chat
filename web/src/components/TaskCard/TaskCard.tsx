import { For, onMount, Show } from "solid-js"
import type { FleetTask, TaskStatus } from "@/api/types"
import { Button } from "@/components/ui/button"
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

const LABEL: Record<TaskStatus, string> = {
  open: "Open", claimed: "Claimed", in_progress: "In progress", blocked: "Blocked", done: "Done", cancelled: "Cancelled",
}

/** One task: id, title, description, assignee, folded history, and a status menu limited to the moves the board allows. */
export default function TaskCard(props: TaskCardProps) {
  const ui = useUIStore()
  const moves = () => NEXT[props.task.status]
  const focused = () => ui.expandedTaskId() === props.task.id
  const lastNote = () => [...props.task.events].reverse().find((event) => event.note)?.note
  let element: HTMLElement | undefined
  let picker: HTMLSelectElement | undefined
  onMount(() => { if (focused()) element?.scrollIntoView({ block: "center" }) })
  return (
    <article
      ref={(node) => { element = node }}
      class="card task-card rounded-xl border border-line p-4 transition-opacity motion-safe:animate-[fade-in_160ms_ease-out]"
      classList={{ "is-focused": focused() }}
      data-task={props.task.id}
    >
      <header class="flex items-center justify-between gap-2">
        <span class="font-mono text-[11px] tracking-wide text-cream-dim">#{props.task.id}</span>
        <Show when={props.task.status === "blocked"}><span class="soft-badge" data-tone="warn">Needs attention</span></Show>
      </header>
      <h3 class="mt-2 text-[15px] font-semibold leading-snug text-cream">{props.task.title}</h3>
      <Show when={props.task.description}><p class="mt-1.5 whitespace-pre-wrap text-[13px] leading-relaxed text-cream-dim">{props.task.description}</p></Show>
      <Show when={props.task.status === "blocked" && lastNote()}>
        <p class="blocked-note mt-3 rounded-lg px-3 py-2 text-xs">◷ {lastNote()}</p>
      </Show>
      <Show when={props.task.events.length > 0}>
        <details class="mt-2 text-xs text-cream-dim" open={focused()}>
          <summary class="cursor-pointer select-none hover:text-cream">History ({props.task.events.length}) · v{props.task.version}</summary>
          <ol class="mt-1.5 space-y-1">
            <For each={props.task.events}>
              {(event) => <li>{event.actor}: {event.from_status ?? "created"} → {event.to_status}{event.note ? ` — ${event.note}` : ""}</li>}
            </For>
          </ol>
        </details>
      </Show>
      <footer class="mt-3 flex items-center gap-2 border-t border-line pt-3 text-xs">
        <Show when={props.task.assignee} fallback={<span class="flex items-center gap-2 text-cream-dim"><span class="avatar-empty flex size-7 items-center justify-center rounded-lg">?</span>Unassigned</span>}>
          {(assignee) => <span class="flex min-w-0 items-center gap-2"><Avatar name={assignee()} size="sm" /><span class="truncate text-cream">{assignee()}</span></span>}
        </Show>
        <div class="ml-auto flex items-center gap-1.5">
          <Show when={props.task.status === "open" && !props.task.assignee}>
            <Button type="button" size="sm" class="h-7 px-2.5" disabled={props.busy || !props.actor} onClick={() => props.onClaim(props.task)}>Claim</Button>
          </Show>
          <select
            ref={(node) => { picker = node }}
            class="status-select h-7 rounded-lg border border-line bg-ink-sunken px-1.5 text-xs text-cream"
            aria-label={`Status of task #${props.task.id}`}
            value={props.task.status}
            disabled={props.busy || !props.actor || moves().length === 0}
            title={!props.actor ? "Set a display name to change tasks" : undefined}
            onChange={(event) => {
              const next = event.currentTarget.value as TaskStatus
              if (picker) picker.value = props.task.status
              if (next !== props.task.status) props.onStatus(props.task, next)
            }}
          >
            <option value={props.task.status}>{LABEL[props.task.status]}</option>
            <For each={moves()}>{(status) => <option value={status}>→ {LABEL[status]}</option>}</For>
          </select>
        </div>
      </footer>
    </article>
  )
}

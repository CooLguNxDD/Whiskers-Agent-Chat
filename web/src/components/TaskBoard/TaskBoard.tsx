import { createSignal, For, Show } from "solid-js"
import { isApiError } from "@/api/client"
import type { TaskStatus } from "@/api/types"
import TaskCard from "@/components/TaskCard"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { useClaimTask, useCreateTask, useTasks, useUpdateTaskStatus } from "@/hooks/useTasks"
import { usePrefs, useUIStore } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { isHandle } from "@/utils/handles"
import { PageHeader } from "@/components/ui/page"
import { Icon } from "@/components/ui/icons"
import { selectClass } from "@/components/RichText"
import { relativeTime } from "@/utils/fleetStatus"

export type TaskBoardProps = Record<string, never>
const MAIN_LANES: TaskStatus[] = ["open", "in_progress", "blocked", "done"]
const ALL_LANES: TaskStatus[] = ["open", "claimed", "in_progress", "blocked", "done", "cancelled"]
const LANE_LABEL: Record<TaskStatus, string> = {
  open: "Ready", claimed: "Claimed", in_progress: "In progress", blocked: "Blocked", done: "Done", cancelled: "Cancelled",
}

/** Task board with optimistic updates that roll back, and a refresh on conflict. */
export default function TaskBoard() {
  const ui = useUIStore()
  const prefs = usePrefs()
  const actor = () => isHandle(prefs.displayName().trim()) ? prefs.displayName().trim() : ""
  const tasks = useTasks(ui.activeChannel)
  const create = useCreateTask()
  const claim = useClaimTask(ui.activeChannel)
  const update = useUpdateTaskStatus(ui.activeChannel)
  const [title, setTitle] = createSignal("")
  const [description, setDescription] = createSignal("")
  const [assignee, setAssignee] = createSignal("")
  const [requestId, setRequestId] = createSignal(crypto.randomUUID())
  const [error, setError] = createSignal("")
  const [conflict, setConflict] = createSignal("")
  const rows = () => tasks.data?.tasks ?? []
  const [composing, setComposing] = createSignal(false)
  const [view, setView] = createSignal<"board" | "list">("board")
  const [search, setSearch] = createSignal("")
  const [who, setWho] = createSignal("")
  const [showAll, setShowAll] = createSignal(false)
  const assignees = () => [...new Set(rows().map((task) => task.assignee).filter((name): name is string => Boolean(name)))].sort()
  const visible = () => {
    const needle = search().trim().toLowerCase()
    return rows().filter((task) =>
      (!needle || `${task.title} ${task.description} #${task.id}`.toLowerCase().includes(needle)) &&
      (!who() || (who() === "-" ? !task.assignee : task.assignee === who())))
  }
  const lanes = () => (showAll() ? ALL_LANES : MAIN_LANES)
  const live = () => rows().filter((task) => ["open", "claimed", "in_progress", "blocked"].includes(task.status))
  const openComposer = () => { setComposing(true); queueMicrotask(() => document.querySelector<HTMLInputElement>('input[aria-label="Title"]')?.focus()) }
  const cardProps = () => ({
    actor: actor(),
    busy: claim.isPending || update.isPending,
    onClaim: (item: Parameters<typeof claim.mutate>[0]["task"]) => {
      setConflict("")
      claim.mutate({ task: item, actor: actor() }, {
        onError: (reason) => {
          if (isApiError(reason) && reason.status === 409) setConflict("Someone else updated this task. The board was refreshed.")
        },
      })
    },
    onStatus: (item: Parameters<typeof update.mutate>[0]["task"], next: TaskStatus) => {
      setConflict("")
      update.mutate({ task: item, status: next, actor: actor() }, {
        onError: (reason) => {
          if (isApiError(reason) && reason.status === 409) setConflict("That move conflicted with a newer version. The board was refreshed.")
        },
      })
    },
  })

  return (
    <div class="page min-h-0 flex-1 overflow-y-auto">
      <PageHeader
        eyebrow={`Fleet workspace · #${ui.activeChannel()}`}
        title="Small steps. Big missions."
        description="Know what's moving, who's on it, and what needs you."
        stats={[
          { value: live().length, label: "active tasks" },
          { value: rows().filter((task) => task.status === "in_progress").length, label: "in progress" },
          { value: rows().filter((task) => task.status === "blocked").length, label: "needs attention", tone: "accent" },
        ]}
      >
        <Button type="button" variant={composing() ? "outline" : "default"} class="h-10 px-4" onClick={() => (composing() ? setComposing(false) : openComposer())} aria-expanded={composing()}>
          {composing() ? "Close" : "＋ New task"}
        </Button>
      </PageHeader>
      <Show when={composing()}>
      <form
        class="card mb-6 grid max-w-3xl gap-2 rounded-2xl border border-line p-5"
        onSubmit={(event) => {
          event.preventDefault()
          setError("")
          if (!actor()) { setError("Set a display name before creating a task."); return }
          if (!title().trim()) { setError("A title is required."); return }
          create.mutate(
            {
              channel: ui.activeChannel(),
              title: title().trim(),
              description: description().trim(),
              assignee: assignee().trim() || undefined,
              actor: actor(),
              client_request_id: requestId(),
            },
            {
              onSuccess: () => {
                setTitle("")
                setDescription("")
                setAssignee("")
                setRequestId(crypto.randomUUID())
                setComposing(false)
              },
              onError: (reason) => setError(getErrorMessage(reason, "The task was not created. Your draft is still here.")),
            },
          )
        }}
      >
        <h2 class="text-sm font-semibold text-cream">New task in #{ui.activeChannel()}</h2>
        <Input aria-label="Title" placeholder="Title" value={title()} onInput={(event) => setTitle(event.currentTarget.value)} maxLength={200} />
        <Textarea aria-label="Description" placeholder="Description" value={description()} onInput={(event) => setDescription(event.currentTarget.value)} />
        <Input aria-label="Assignee" placeholder="Assignee (optional)" value={assignee()} onInput={(event) => setAssignee(event.currentTarget.value)} />
        <div class="flex justify-end"><Button type="submit" disabled={create.isPending}>Add task</Button></div>
        <Show when={error()}><p class="text-sm text-rust">{error()}</p></Show>
      </form>
      </Show>
      <div class="mb-5 flex flex-wrap items-center gap-3 border-t border-line pt-5">
        <div class="segmented flex rounded-xl border border-line p-1 text-sm" role="group" aria-label="View">
          <button type="button" class="flex items-center gap-1.5 rounded-lg px-3 py-1" aria-pressed={view() === "board"} onClick={() => setView("board")}><Icon name="board" class="size-4" />Board</button>
          <button type="button" class="flex items-center gap-1.5 rounded-lg px-3 py-1" aria-pressed={view() === "list"} onClick={() => setView("list")}><Icon name="list" class="size-4" />List</button>
        </div>
        <div class="ml-auto flex flex-wrap items-center gap-2">
          <Input class="h-9 w-48" aria-label="Search tasks" placeholder="Search tasks…" value={search()} onInput={(event) => setSearch(event.currentTarget.value)} />
          <select class={`${selectClass} h-9`} aria-label="Assignee filter" value={who()} onChange={(event) => setWho(event.currentTarget.value)}>
            <option value="">All assignees</option>
            <option value="-">Unassigned</option>
            <For each={assignees()}>{(name) => <option value={name}>{name}</option>}</For>
          </select>
        </div>
      </div>
      <Show when={conflict()}><p class="mb-4 text-sm text-brass">{conflict()}</p></Show>
      <Show when={tasks.isError}><p class="text-sm text-rust">{getErrorMessage(tasks.error, "Could not load tasks.")}</p></Show>
      <Show when={view() === "board"} fallback={
        <div class="card overflow-x-auto rounded-2xl border border-line">
          <table class="w-full text-sm">
            <thead class="text-left">
              <tr class="list-row"><th class="field-label px-4 py-3">Task</th><th class="field-label px-4 py-3">Status</th><th class="field-label px-4 py-3">Assignee</th><th class="field-label px-4 py-3">Updated</th></tr>
            </thead>
            <tbody>
              <For each={visible().filter((task) => lanes().includes(task.status))}>
                {(task) => (
                  <tr class="list-row last:border-0" data-task={task.id}>
                    <td class="px-4 py-3"><span class="mr-2 font-mono text-xs text-cream-dim">#{task.id}</span><span class="font-medium text-cream">{task.title}</span></td>
                    <td class="px-4 py-3"><span class="soft-badge" data-tone={task.status === "blocked" ? "warn" : task.status === "done" ? "ok" : task.status === "in_progress" ? "accent" : undefined}>{LANE_LABEL[task.status]}</span></td>
                    <td class="px-4 py-3 text-cream-dim">{task.assignee ?? "Unassigned"}</td>
                    <td class="px-4 py-3 text-cream-dim">{relativeTime(task.updated_at)}</td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
          <Show when={tasks.isSuccess && visible().length === 0}><p class="px-4 py-8 text-center text-sm text-cream-dim">No tasks match.</p></Show>
        </div>
      }>
        <div class="lanes grid gap-4">
          <For each={lanes()}>
            {(status) => {
              const items = () => visible().filter((task) => task.status === status)
              return (
                <section class="lane flex min-w-0 flex-col gap-3 rounded-2xl p-3.5" data-status={status} aria-label={`${LANE_LABEL[status]} tasks`}>
                  <h2 class="flex items-center gap-2 px-1 text-sm font-semibold text-cream">
                    <span class="lane-dot size-2 rounded-full" data-status={status} aria-hidden="true" />
                    {LANE_LABEL[status]}
                    <span class="font-normal text-cream-dim tabular-nums">{items().length}</span>
                    <Show when={status === "open"}>
                      <button type="button" class="icon-btn ml-auto size-7 min-w-7" title="New task" aria-label="New task" onClick={openComposer}><Icon name="plus" class="size-4" /></button>
                    </Show>
                  </h2>
                  <For each={items()}>{(task) => <TaskCard task={task} {...cardProps()} />}</For>
                  <Show when={status === "open"}>
                    <button type="button" class="flex items-center gap-1.5 px-1 text-[13px] text-cream-dim hover:text-cream" onClick={openComposer}><Icon name="plus" class="size-3.5" />Add task</button>
                  </Show>
                  <Show when={tasks.isSuccess && items().length === 0 && status !== "open"}>
                    <p class="px-1 pb-2 text-[13px] text-cream-dim">Nothing here</p>
                  </Show>
                </section>
              )
            }}
          </For>
        </div>
      </Show>
      <div class="mt-5 flex flex-wrap items-center justify-between gap-2 text-xs text-cream-dim">
        <label class="flex items-center gap-2"><input type="checkbox" checked={showAll()} onChange={(event) => setShowAll(event.currentTarget.checked)} />Show claimed &amp; cancelled</label>
        <span>Change a card's status from its menu · Claim picks it up as you</span>
      </div>
    </div>
  )
}

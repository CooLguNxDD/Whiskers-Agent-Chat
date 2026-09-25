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

export type TaskBoardProps = Record<string, never>
const COLUMNS: TaskStatus[] = ["open", "claimed", "in_progress", "blocked", "done", "cancelled"]

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

  return (
    <div class="min-h-0 flex-1 overflow-y-auto p-4">
      <form
        class="mx-auto mb-6 grid max-w-3xl gap-2"
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
              },
              onError: (reason) => setError(getErrorMessage(reason, "The task was not created. Your draft is still here.")),
            },
          )
        }}
      >
        <h2 class="font-display text-2xl">Tasks in #{ui.activeChannel()}</h2>
        <Input aria-label="Title" placeholder="Title" value={title()} onInput={(event) => setTitle(event.currentTarget.value)} maxLength={200} />
        <Textarea aria-label="Description" placeholder="Description" value={description()} onInput={(event) => setDescription(event.currentTarget.value)} />
        <Input aria-label="Assignee" placeholder="Assignee (optional)" value={assignee()} onInput={(event) => setAssignee(event.currentTarget.value)} />
        <div class="flex justify-end"><Button type="submit" disabled={create.isPending}>Add task</Button></div>
        <Show when={error()}><p class="text-sm text-rust">{error()}</p></Show>
      </form>
      <Show when={conflict()}><p class="mx-auto mb-4 max-w-6xl text-sm text-brass">{conflict()}</p></Show>
      <Show when={tasks.isError}><p class="text-sm text-rust">{getErrorMessage(tasks.error, "Could not load tasks.")}</p></Show>
      <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        <For each={COLUMNS}>
          {(status) => (
            <section class="flex flex-col gap-2">
              <h3 class="text-xs uppercase tracking-wide text-cream-dim">{status.replace("_", " ")}</h3>
              <For each={rows().filter((task) => task.status === status)}>
                {(task) => (
                  <TaskCard
                    task={task}
                    actor={actor()}
                    busy={claim.isPending || update.isPending}
                    onClaim={(item) => {
                      setConflict("")
                      claim.mutate({ task: item, actor: actor() }, {
                        onError: (reason) => {
                          if (isApiError(reason) && reason.status === 409) setConflict("Someone else updated this task. The board was refreshed.")
                        },
                      })
                    }}
                    onStatus={(item, next) => {
                      setConflict("")
                      update.mutate({ task: item, status: next, actor: actor() }, {
                        onError: (reason) => {
                          if (isApiError(reason) && reason.status === 409) setConflict("That move conflicted with a newer version. The board was refreshed.")
                        },
                      })
                    }}
                  />
                )}
              </For>
            </section>
          )}
        </For>
      </div>
    </div>
  )
}

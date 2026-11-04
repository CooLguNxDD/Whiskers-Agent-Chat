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
import { LaneTitle, PageHeader } from "@/components/ui/page"

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
  const [composing, setComposing] = createSignal(false)

  return (
    <div class="page min-h-0 flex-1 overflow-y-auto">
      <PageHeader title={`Tasks in #${ui.activeChannel()}`} description="Claim work, move it across the board, and see who is on what.">
        <Button type="button" variant={composing() ? "outline" : "default"} onClick={() => setComposing((open) => !open)} aria-expanded={composing()}>
          {composing() ? "Close" : "＋ New task"}
        </Button>
      </PageHeader>
      <Show when={composing()}>
      <form
        class="card mb-6 grid max-w-3xl gap-2 rounded-xl border border-line p-4"
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
        <h2 class="text-sm font-semibold text-cream">New task</h2>
        <Input aria-label="Title" placeholder="Title" value={title()} onInput={(event) => setTitle(event.currentTarget.value)} maxLength={200} />
        <Textarea aria-label="Description" placeholder="Description" value={description()} onInput={(event) => setDescription(event.currentTarget.value)} />
        <Input aria-label="Assignee" placeholder="Assignee (optional)" value={assignee()} onInput={(event) => setAssignee(event.currentTarget.value)} />
        <div class="flex justify-end"><Button type="submit" disabled={create.isPending}>Add task</Button></div>
        <Show when={error()}><p class="text-sm text-rust">{error()}</p></Show>
      </form>
      </Show>
      <Show when={conflict()}><p class="mx-auto mb-4 max-w-6xl text-sm text-brass">{conflict()}</p></Show>
      <Show when={tasks.isError}><p class="text-sm text-rust">{getErrorMessage(tasks.error, "Could not load tasks.")}</p></Show>
      <div class="lanes grid gap-3">
        <For each={COLUMNS}>
          {(status) => (
            <section class="lane flex min-w-0 flex-col gap-2 rounded-2xl p-2" data-status={status} aria-label={`${status.replace("_", " ")} tasks`}>
              <LaneTitle label={status.replace("_", " ")} count={rows().filter((task) => task.status === status).length} />
              <Show when={tasks.isSuccess && rows().every((task) => task.status !== status)}>
                <p class="px-1 py-3 text-center text-xs text-cream-dim">Nothing here</p>
              </Show>
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

import { createMutation, createQuery, useQueryClient } from "@tanstack/solid-query"
import type { Accessor } from "solid-js"
import { claimTask, createTask, listTasks, updateTaskStatus } from "@/api/fleet"
import { isApiError } from "@/api/client"
import type { FleetTask, TaskStatus } from "@/api/types"

export function useTasks(channel: Accessor<string>) {
  return createQuery(() => ({
    queryKey: ["tasks", channel()],
    queryFn: () => listTasks(channel()),
    enabled: Boolean(channel()),
  }))
}

export function useCreateTask() {
  const queryClient = useQueryClient()
  return createMutation(() => ({
    mutationFn: createTask,
    onSuccess: (_result, submitted) => {
      void queryClient.invalidateQueries({ queryKey: ["tasks", submitted.channel] })
    },
  }))
}

function snapshotKey(channel: string) {
  return ["tasks", channel] as const
}

/** Optimistic claim. A 409 refreshes the board instead of leaving the guessed row. */
export function useClaimTask(channel: Accessor<string>) {
  const queryClient = useQueryClient()
  return createMutation(() => ({
    mutationFn: ({ task, actor }: { task: FleetTask; actor: string }) =>
      claimTask(task.id, actor, task.version),
    onMutate: async ({ task, actor }) => {
      await queryClient.cancelQueries({ queryKey: snapshotKey(channel()) })
      const previous = queryClient.getQueryData<{ tasks: FleetTask[] }>(snapshotKey(channel()))
      queryClient.setQueryData<{ tasks: FleetTask[] }>(snapshotKey(channel()), (current) => {
        if (!current) return current
        return {
          tasks: current.tasks.map((item) =>
            item.id === task.id
              ? { ...item, status: "claimed" as const, assignee: actor, version: item.version + 1 }
              : item,
          ),
        }
      })
      return { previous }
    },
    onError: (err, _vars, context) => {
      if (context?.previous) queryClient.setQueryData(snapshotKey(channel()), context.previous)
      if (isApiError(err) && err.status === 409) {
        void queryClient.invalidateQueries({ queryKey: snapshotKey(channel()) })
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: snapshotKey(channel()) })
    },
  }))
}

export function useUpdateTaskStatus(channel: Accessor<string>) {
  const queryClient = useQueryClient()
  return createMutation(() => ({
    mutationFn: ({
      task,
      status,
      actor,
    }: {
      task: FleetTask
      status: TaskStatus
      actor: string
    }) => updateTaskStatus(task.id, status, actor, task.version),
    onMutate: async ({ task, status }) => {
      await queryClient.cancelQueries({ queryKey: snapshotKey(channel()) })
      const previous = queryClient.getQueryData<{ tasks: FleetTask[] }>(snapshotKey(channel()))
      queryClient.setQueryData<{ tasks: FleetTask[] }>(snapshotKey(channel()), (current) => {
        if (!current) return current
        return {
          tasks: current.tasks.map((item) =>
            item.id === task.id
              ? {
                  ...item,
                  status,
                  assignee: status === "open" ? null : item.assignee,
                  version: item.version + 1,
                }
              : item,
          ),
        }
      })
      return { previous }
    },
    onError: (err, _vars, context) => {
      if (context?.previous) queryClient.setQueryData(snapshotKey(channel()), context.previous)
      if (isApiError(err) && err.status === 409) {
        void queryClient.invalidateQueries({ queryKey: snapshotKey(channel()) })
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: snapshotKey(channel()) })
    },
  }))
}

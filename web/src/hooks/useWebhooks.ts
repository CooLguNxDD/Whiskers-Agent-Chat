import { createMutation, createQuery, useQueryClient } from "@tanstack/solid-query"
import {
  createWebhook,
  deleteWebhook,
  listWebhooks,
  rotateWebhookSecret,
  testWebhook,
  updateWebhook,
} from "@/api/fleet"

/** Webhooks, polled while `active` so a hook's last status updates in front of you. */
export function useWebhooks(active: () => boolean) {
  return createQuery(() => ({
    queryKey: ["webhooks"],
    queryFn: listWebhooks,
    enabled: active(),
    refetchInterval: active() ? 5_000 : false,
  }))
}

export function useWebhookMutations() {
  const queryClient = useQueryClient()
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["webhooks"] })
  return {
    create: createMutation(() => ({ mutationFn: createWebhook, onSuccess: refresh })),
    update: createMutation(() => ({
      mutationFn: ({ id, changes }: { id: number; changes: Record<string, unknown> }) =>
        updateWebhook(id, changes),
      onSuccess: refresh,
    })),
    remove: createMutation(() => ({ mutationFn: deleteWebhook, onSuccess: refresh })),
    rotate: createMutation(() => ({ mutationFn: rotateWebhookSecret, onSuccess: refresh })),
    test: createMutation(() => ({ mutationFn: testWebhook, onSuccess: refresh })),
  }
}

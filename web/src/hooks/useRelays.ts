import { createMutation, createQuery, useQueryClient } from "@tanstack/solid-query"
import {
  addBinding,
  addRoute,
  createRelay,
  deleteBinding,
  deleteRelay,
  deleteRoute,
  getRelay,
  listAudit,
  listRelays,
  rotateRelayToken,
  seekRoute,
  updateRelay,
  updateRoute,
} from "@/api/relays"

/**
 * Relays, polled every 5 s so the online/stale/offline state follows the
 * heartbeats (which are quiet writes and never reach the event stream).
 * `relay.*` and `consumer.*` events also refresh the "relay" query family.
 */
export function useRelays() {
  return createQuery(() => ({
    queryKey: ["relay", "list"],
    queryFn: ({ signal }) => listRelays(signal),
    refetchInterval: 5_000,
  }))
}

/** One relay with its bindings and routes, polled like the list. */
export function useRelay(name: () => string | null) {
  return createQuery(() => ({
    queryKey: ["relay", "detail", name()],
    queryFn: ({ signal }) => getRelay(name() as string, signal),
    enabled: Boolean(name()),
    refetchInterval: 5_000,
  }))
}

/** Control-plane audit rows (newest first); the page filters them to one relay. */
export function useRelayAudit(enabled: () => boolean) {
  return createQuery(() => ({
    queryKey: ["relay", "audit"],
    queryFn: ({ signal }) => listAudit({ limit: 200 }, signal),
    enabled: enabled(),
  }))
}

export function useRelayMutations() {
  const queryClient = useQueryClient()
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["relay"] })
  return {
    create: createMutation(() => ({
      mutationFn: (input: Parameters<typeof createRelay>[0]) => createRelay(input),
      onSuccess: refresh,
    })),
    update: createMutation(() => ({
      mutationFn: (input: { name: string; version: number; changes: Parameters<typeof updateRelay>[2] }) =>
        updateRelay(input.name, input.version, input.changes),
      onSuccess: refresh,
    })),
    remove: createMutation(() => ({
      mutationFn: (input: { name: string; version: number; actor: string }) => deleteRelay(input.name, input.version, input.actor),
      onSuccess: refresh,
    })),
    rotate: createMutation(() => ({
      mutationFn: (input: { name: string; actor: string }) => rotateRelayToken(input.name, input.actor),
      onSuccess: refresh,
    })),
    bind: createMutation(() => ({
      mutationFn: (input: { name: string; body: Parameters<typeof addBinding>[1] }) => addBinding(input.name, input.body),
      onSuccess: refresh,
    })),
    unbind: createMutation(() => ({
      mutationFn: (input: { name: string; id: number; actor: string }) => deleteBinding(input.name, input.id, input.actor),
      onSuccess: refresh,
    })),
    addRoute: createMutation(() => ({
      mutationFn: (input: { name: string; body: Parameters<typeof addRoute>[1] }) => addRoute(input.name, input.body),
      onSuccess: refresh,
    })),
    updateRoute: createMutation(() => ({
      mutationFn: (input: { name: string; id: number; changes: Parameters<typeof updateRoute>[2] }) =>
        updateRoute(input.name, input.id, input.changes),
      onSuccess: refresh,
    })),
    removeRoute: createMutation(() => ({
      mutationFn: (input: { name: string; id: number; actor: string }) => deleteRoute(input.name, input.id, input.actor),
      onSuccess: refresh,
    })),
    seek: createMutation(() => ({
      mutationFn: (input: { name: string; id: number; afterEventId: number; actor: string }) =>
        seekRoute(input.name, input.id, input.afterEventId, input.actor),
      onSuccess: refresh,
    })),
  }
}

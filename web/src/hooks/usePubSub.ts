import { createInfiniteQuery, createMutation, createQuery, useQueryClient } from "@tanstack/solid-query"
import { listPublications, listSubscriptions, publish, subscribe, unsubscribe } from "@/api/pubsub"

/** Publication history, newest first. Refreshed by `topic.published` events (query family "pubsub"). */
export function usePublications(pattern: () => string) {
  return createInfiniteQuery(() => ({
    queryKey: ["pubsub", "publications", pattern()],
    queryFn: ({ pageParam, signal }) =>
      listPublications({ pattern: pattern() || undefined, beforeId: pageParam, limit: 50 }, signal),
    initialPageParam: null as number | null,
    getNextPageParam: (page) => (page.has_more ? page.cursor : undefined),
  }))
}

/** All subscriptions. Refreshed by `topic.subscribed` / `topic.unsubscribed` events. */
export function useSubscriptions() {
  return createQuery(() => ({
    queryKey: ["pubsub", "subscriptions"],
    queryFn: ({ signal }) => listSubscriptions(undefined, signal),
  }))
}

export function usePubSubMutations() {
  const queryClient = useQueryClient()
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["pubsub"] })
  return {
    publish: createMutation(() => ({ mutationFn: publish, onSuccess: refresh })),
    subscribe: createMutation(() => ({
      mutationFn: ({ agent, pattern }: { agent: string; pattern: string }) => subscribe(agent, pattern),
      onSuccess: refresh,
    })),
    unsubscribe: createMutation(() => ({
      mutationFn: ({ agent, pattern }: { agent: string; pattern: string }) => unsubscribe(agent, pattern),
      onSuccess: refresh,
    })),
  }
}

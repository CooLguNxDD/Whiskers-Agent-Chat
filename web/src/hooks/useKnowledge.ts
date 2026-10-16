import { createInfiniteQuery, createMutation, createQuery, useQueryClient } from "@tanstack/solid-query"
import { addFact, listFacts, listProjects, listTopics, searchFacts, setFactStatus, type FactFilters } from "@/api/knowledge"
import type { FactStatus } from "@/api/types"

/** Every project with a fact, gathered from all fact pages. */
export function useKnowledgeProjects() {
  return createQuery(() => ({
    queryKey: ["knowledge", "projects"],
    queryFn: ({ signal }) => listProjects(signal),
  }))
}

export function useKnowledgeTopics(project: () => string) {
  return createQuery(() => ({
    queryKey: ["knowledge", "topics", project()],
    queryFn: () => listTopics(project()),
    enabled: project() !== "",
  }))
}

export function useFacts(filters: () => FactFilters | null) {
  return createInfiniteQuery(() => ({
    queryKey: ["knowledge", "facts", filters()],
    queryFn: ({ pageParam, signal }) => listFacts(filters()!, { cursor: pageParam, limit: 50 }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => (page.has_more ? page.next_cursor : undefined),
    enabled: filters() !== null,
  }))
}

export function useFactSearch(q: () => string) {
  return createQuery(() => ({
    queryKey: ["knowledge", "search", q()],
    queryFn: ({ signal }) => searchFacts(q(), { limit: 50 }, signal),
    enabled: q().trim() !== "",
    retry: false,
  }))
}

export function useKnowledgeMutations() {
  const queryClient = useQueryClient()
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["knowledge"] })
  return {
    add: createMutation(() => ({ mutationFn: addFact, onSuccess: refresh })),
    status: createMutation(() => ({
      mutationFn: ({ id, status, actor }: { id: number; status: FactStatus; actor: string }) => setFactStatus(id, status, actor),
      onSuccess: refresh,
    })),
  }
}

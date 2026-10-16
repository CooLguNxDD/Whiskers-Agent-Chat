import { createQuery } from "@tanstack/solid-query"
import { search, searchStatus } from "@/api/search"
import type { SearchQuery } from "@/api/types"

/** Embedder state for the mode toggle. Polled slowly: it changes only on hub restarts and queue errors. */
export function useSearchStatus() {
  return createQuery(() => ({ queryKey: ["search-status"], queryFn: searchStatus, refetchInterval: 60_000, retry: false }))
}

/** One results page. Disabled until there is a query. Not refreshed by events: results are a snapshot. */
export function useSearch(query: () => SearchQuery | null) {
  return createQuery(() => {
    const current = query()
    return {
      queryKey: ["search", current],
      queryFn: ({ signal }) => search(current!, signal),
      enabled: current !== null && current.q.trim() !== "",
      retry: false,
    }
  })
}

import { createInfiniteQuery, createMutation, createQuery, useQueryClient } from "@tanstack/solid-query"
import { createPost, follow, getThread, getTimeline, listFollowing, repost, trendingTags, unfollow, type TimelineScope } from "@/api/feed"

/** Timeline pages. Refreshed by feed events (query family "feed"). */
export function useTimeline(scope: () => TimelineScope | null) {
  return createInfiniteQuery(() => ({
    queryKey: ["feed", "timeline", scope()],
    queryFn: ({ pageParam, signal }) => getTimeline(scope()!, { cursor: pageParam, limit: 30 }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => (page.has_more ? page.next_cursor : undefined),
    enabled: scope() !== null,
  }))
}

/** One thread window around `postId`, paged by the hub's cursor. */
export function useThread(postId: () => number | null) {
  return createInfiniteQuery(() => ({
    queryKey: ["feed", "thread", postId()],
    queryFn: ({ pageParam, signal }) => getThread(postId()!, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => (page.has_more ? page.next_cursor : undefined),
    enabled: postId() !== null,
  }))
}

export function useFollowing(handle: () => string) {
  return createQuery(() => ({
    queryKey: ["feed", "following", handle()],
    queryFn: () => listFollowing(handle()),
    enabled: handle() !== "",
  }))
}

export function useTrendingTags() {
  return createQuery(() => ({ queryKey: ["feed", "trending"], queryFn: () => trendingTags(10) }))
}

export function useFeedMutations() {
  const queryClient = useQueryClient()
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["feed"] })
  return {
    post: createMutation(() => ({ mutationFn: createPost, onSuccess: refresh })),
    repost: createMutation(() => ({
      mutationFn: ({ id, author }: { id: number; author: string }) => repost(id, author),
      onSuccess: refresh,
    })),
    follow: createMutation(() => ({
      mutationFn: ({ follower, followee, on }: { follower: string; followee: string; on: boolean }) =>
        on ? follow(follower, followee) : unfollow(follower, followee),
      onSuccess: refresh,
    })),
  }
}

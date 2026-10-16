import { api, queryString } from "./client"
import type { FeedPost, FollowResult, ThreadPage, TimelinePage } from "./types"

export type TimelineScope =
  | { scope: "global" }
  | { scope: "following"; handle: string }
  | { scope: "hashtag"; tag: string }
  | { scope: "author"; author: string }

/** Newest-first posts. Pass the previous page's `next_cursor` to load more. */
export function getTimeline(
  scope: TimelineScope,
  opts: { cursor?: string | null; limit?: number } = {},
  signal?: AbortSignal,
) {
  const path = `/api/v1/feed/timeline${queryString({
    scope: scope.scope,
    handle: scope.scope === "following" ? scope.handle : undefined,
    tag: scope.scope === "hashtag" ? scope.tag : undefined,
    author: scope.scope === "author" ? scope.author : undefined,
    cursor: opts.cursor,
    limit: opts.limit,
  })}`
  return api<TimelinePage>(path, { signal })
}

/** The thread window around any post in it. A wide window pages by `next_cursor`. */
export function getThread(postId: number, cursor?: string | null, signal?: AbortSignal) {
  return api<ThreadPage>(`/api/v1/feed/posts/${postId}${queryString({ thread: 1, cursor })}`, { signal })
}

/** A post, a reply (`parent_id`) or a quote (`quote_of`). */
export function createPost(input: {
  author: string
  body: string
  parent_id?: number | null
  quote_of?: number | null
  client_request_id: string
}) {
  const body: Record<string, unknown> = {
    author: input.author,
    body: input.body,
    client_request_id: input.client_request_id,
  }
  if (input.parent_id != null) body.parent_id = input.parent_id
  if (input.quote_of != null) body.quote_of = input.quote_of
  return api<{ post: FeedPost }>("/api/v1/feed/posts", { method: "POST", body: JSON.stringify(body) })
}

/** Repost. Repeating it returns the same repost with `idempotent: true`. */
export function repost(postId: number, author: string) {
  return api<{ post: FeedPost; idempotent: boolean }>(`/api/v1/feed/posts/${postId}/repost`, {
    method: "POST",
    body: JSON.stringify({ author }),
  })
}

export function deletePost(postId: number, actor: string) {
  return api<{ post: FeedPost; idempotent: boolean }>(`/api/v1/feed/posts/${postId}`, {
    method: "DELETE",
    body: JSON.stringify({ actor }),
  })
}

export function follow(follower: string, followee: string) {
  return api<FollowResult>(`/api/v1/feed/follows/${encodeURIComponent(followee)}`, {
    method: "PUT",
    body: JSON.stringify({ follower }),
  })
}

export function unfollow(follower: string, followee: string) {
  return api<FollowResult>(`/api/v1/feed/follows/${encodeURIComponent(followee)}`, {
    method: "DELETE",
    body: JSON.stringify({ follower }),
  })
}

export function listFollowing(handle: string) {
  return api<{ handle: string; following: string[] }>(`/api/v1/feed/follows/${encodeURIComponent(handle)}`)
}

export function trendingTags(limit?: number) {
  return api<{ since: string; tags: { tag: string; count: number }[] }>(
    `/api/v1/feed/tags/trending${queryString({ limit })}`,
  )
}

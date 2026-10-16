import { createMemo, createSignal, For, Show } from "solid-js"
import type { TimelineScope } from "@/api/feed"
import type { FeedPost, FeedTombstone, ThreadNode } from "@/api/types"
import { FeedBody } from "@/components/RichText"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { useFeedMutations, useFollowing, useThread, useTimeline, useTrendingTags } from "@/hooks/useFeed"
import { usePrefs } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { isHandle } from "@/utils/handles"
import { mergeThread } from "@/utils/thread"
import { formatStamp } from "@/utils/time"

type Tab = "global" | "following"

interface FeedContext {
  me: () => string
  following: () => Set<string>
  onTag: (tag: string) => void
  openThread: (id: number) => void
}

/** The agent feed: global and following timelines, hashtags, threads, replies, reposts and quotes. */
export default function FeedPage() {
  const prefs = usePrefs()
  const me = () => (isHandle(prefs.displayName().trim()) ? prefs.displayName().trim() : "")
  const [tab, setTab] = createSignal<Tab>("global")
  const [tag, setTag] = createSignal("")
  const [thread, setThread] = createSignal<number | null>(null)
  const scope = (): TimelineScope | null => {
    if (tag()) return { scope: "hashtag", tag: tag() }
    if (tab() === "following") return me() ? { scope: "following", handle: me() } : null
    return { scope: "global" }
  }
  const timeline = useTimeline(scope)
  const followingQuery = useFollowing(me)
  const trending = useTrendingTags()
  const following = createMemo(() => new Set(followingQuery.data?.following ?? []))
  const posts = () => timeline.data?.pages.flatMap((page) => page.posts) ?? []
  const context: FeedContext = {
    me,
    following,
    onTag: (value) => { setTag(value); setThread(null) },
    openThread: setThread,
  }

  return (
    <div class="min-h-0 flex-1 overflow-y-auto p-4">
      <div class="mx-auto grid max-w-5xl gap-6 lg:grid-cols-[1fr_14rem]">
        <div class="min-w-0">
          <h1 class="font-display text-xl text-cream">Feed</h1>
          <div role="tablist" aria-label="Timeline" class="mt-3 flex flex-wrap items-center gap-1">
            <For each={["global", "following"] as Tab[]}>
              {(item) => (
                <Button type="button" role="tab" size="sm" variant={tab() === item && !tag() ? "default" : "outline"} aria-selected={tab() === item && !tag()} onClick={() => { setTab(item); setTag(""); setThread(null) }}>
                  {item === "global" ? "Global" : "Following"}
                </Button>
              )}
            </For>
            <Show when={tag()}>
              <span class="ml-2 text-sm text-brass">#{tag()}</span>
              <Button type="button" size="sm" variant="ghost" aria-label="Clear hashtag" onClick={() => setTag("")}>×</Button>
            </Show>
          </div>
          <Show when={!me()}><p class="mt-2 text-xs text-rust">Set a valid display name to post, follow or see your following timeline.</p></Show>
          <Show when={me()}>
            <PostComposer author={me()} label="New post" placeholder="What's happening? Use #tags." class="mt-3" />
          </Show>

          <Show when={thread()} keyed>
            {(id) => <ThreadView postId={id} context={context} onClose={() => setThread(null)} />}
          </Show>

          <Show when={!thread()}>
            <section class="mt-4" aria-label="Posts">
              <Show when={timeline.isError}><p class="text-sm text-rust">{getErrorMessage(timeline.error, "Could not load the feed.")}</p></Show>
              <Show when={timeline.isPending && scope() !== null && !timeline.isError}><p class="text-sm text-cream-dim">Loading…</p></Show>
              <Show when={timeline.isSuccess && posts().length === 0}>
                <p class="text-sm text-cream-dim">{tab() === "following" && !tag() ? "Nothing from the handles you follow yet." : "No posts yet."}</p>
              </Show>
              <ul class="flex flex-col gap-3">
                <For each={posts()}>{(post) => <li><PostCard post={post} context={context} /></li>}</For>
              </ul>
              <Show when={timeline.hasNextPage}>
                <Button type="button" class="mt-3" variant="outline" size="sm" disabled={timeline.isFetchingNextPage} onClick={() => void timeline.fetchNextPage()}>
                  Load more
                </Button>
              </Show>
            </section>
          </Show>
        </div>
        <aside class="text-sm">
          <h2 class="mb-2 text-xs uppercase tracking-wide text-cream-dim">Trending</h2>
          <ul class="flex flex-col gap-1">
            <For each={trending.data?.tags ?? []}>
              {(item) => (
                <li>
                  <button type="button" class="text-brass hover:underline" onClick={() => context.onTag(item.tag)}>#{item.tag}</button>
                  <span class="ml-1 text-xs text-cream-dim">{item.count}</span>
                </li>
              )}
            </For>
            <Show when={trending.isSuccess && (trending.data?.tags.length ?? 0) === 0}><li class="text-cream-dim">No tags today.</li></Show>
          </ul>
        </aside>
      </div>
    </div>
  )
}

function isTombstone(post: FeedPost | FeedTombstone): post is FeedTombstone {
  return !("author" in post)
}

/** One post. A repost shows its original; actions on a repost act on the original. */
function PostCard(props: { post: FeedPost; context: FeedContext; compact?: boolean }) {
  const mutations = useFeedMutations()
  const [composer, setComposer] = createSignal<"reply" | "quote" | null>(null)
  const [error, setError] = createSignal("")
  const original = () => props.post.original
  const target = (): FeedPost => {
    const inner = original()
    return props.post.kind === "repost" && inner && !isTombstone(inner) ? inner : props.post
  }
  const followed = () => props.context.following().has(target().author)
  const fail = (reason: unknown) => setError(getErrorMessage(reason, "The feed did not accept that."))

  return (
    <article class="rounded-lg border border-line p-3" classList={{ "border-brass": (props.post as ThreadNode).focus === true }}>
      <Show when={props.post.kind === "repost"}>
        <p class="mb-1 text-xs text-cream-dim">{props.post.author} reposted</p>
      </Show>
      <Show when={!props.post.deleted} fallback={<p class="text-sm italic text-cream-dim">This post was deleted.</p>}>
        <header class="flex flex-wrap items-center gap-2 text-xs text-cream-dim">
          <span class="text-sm text-cream">{target().author}</span>
          <span title={target().created_at}>{formatStamp(target().created_at)}</span>
          <Show when={props.context.me() && target().author !== props.context.me()}>
            <button
              type="button"
              class="text-brass hover:underline"
              disabled={mutations.follow.isPending}
              onClick={() => mutations.follow.mutate({ follower: props.context.me(), followee: target().author, on: !followed() }, { onError: fail })}
            >
              {followed() ? "Unfollow" : "Follow"}
            </button>
          </Show>
        </header>
        <Show when={target().deleted} fallback={<p class="mt-1 text-sm text-cream"><FeedBody body={target().body} onTag={props.context.onTag} /></p>}>
          <p class="mt-1 text-sm italic text-cream-dim">The original post was deleted.</p>
        </Show>
        <Show when={props.post.kind === "quote" && original()}>
          {(inner) => (
            <div class="mt-2 rounded border border-line bg-ink-sunken p-2 text-sm">
              <Show when={!isTombstone(inner()) && !inner().deleted} fallback={<p class="italic text-cream-dim">The quoted post was deleted.</p>}>
                <p class="text-xs text-cream-dim">{(inner() as FeedPost).author}</p>
                <p class="text-cream"><FeedBody body={(inner() as FeedPost).body} onTag={props.context.onTag} /></p>
              </Show>
            </div>
          )}
        </Show>
        <footer class="mt-2 flex flex-wrap items-center gap-3 text-xs text-cream-dim">
          <Show when={props.context.me()}>
            <button type="button" class="hover:text-cream" onClick={() => setComposer(composer() === "reply" ? null : "reply")}>Reply</button>
            <button type="button" class="hover:text-cream" disabled={mutations.repost.isPending} onClick={() => mutations.repost.mutate({ id: target().id, author: props.context.me() }, { onError: fail })}>
              Repost{target().repost_count ? ` ${target().repost_count}` : ""}
            </button>
            <button type="button" class="hover:text-cream" onClick={() => setComposer(composer() === "quote" ? null : "quote")}>
              Quote{target().quote_count ? ` ${target().quote_count}` : ""}
            </button>
          </Show>
          <Show when={!props.compact}>
            <button type="button" class="hover:text-cream" onClick={() => props.context.openThread(target().id)}>
              Thread{target().reply_count ? ` (${target().reply_count})` : ""}
            </button>
          </Show>
        </footer>
        <Show when={error()}><p class="mt-1 text-xs text-rust">{error()}</p></Show>
        <Show when={composer()}>
          {(mode) => (
            <PostComposer
              author={props.context.me()}
              label={mode() === "reply" ? `Reply to ${target().author}` : `Quote ${target().author}`}
              placeholder={mode() === "reply" ? "Write a reply" : "Add your comment"}
              parentId={mode() === "reply" ? target().id : undefined}
              quoteOf={mode() === "quote" ? target().id : undefined}
              class="mt-2"
              onDone={() => setComposer(null)}
            />
          )}
        </Show>
      </Show>
    </article>
  )
}

function PostComposer(props: {
  author: string
  label: string
  placeholder: string
  parentId?: number
  quoteOf?: number
  class?: string
  onDone?: () => void
}) {
  const mutations = useFeedMutations()
  const [body, setBody] = createSignal("")
  const [requestId, setRequestId] = createSignal(crypto.randomUUID())
  const [error, setError] = createSignal("")
  return (
    <form
      class={`flex flex-col gap-2 ${props.class ?? ""}`}
      aria-label={props.label}
      onSubmit={(event) => {
        event.preventDefault()
        setError("")
        if (!body().trim()) { setError("Write something first."); return }
        mutations.post.mutate(
          { author: props.author, body: body(), parent_id: props.parentId, quote_of: props.quoteOf, client_request_id: requestId() },
          {
            onSuccess: () => { setBody(""); setRequestId(crypto.randomUUID()); props.onDone?.() },
            onError: (reason) => setError(getErrorMessage(reason, "Not posted. Your draft is still here.")),
          },
        )
      }}
    >
      <Textarea aria-label={props.label} class="min-h-16" placeholder={props.placeholder} maxLength={4000} value={body()} onInput={(event) => setBody(event.currentTarget.value)} />
      <div class="flex items-center justify-end gap-2">
        <Show when={error()}><span class="mr-auto text-xs text-rust">{error()}</span></Show>
        <Show when={props.onDone}><Button type="button" size="sm" variant="ghost" onClick={() => props.onDone?.()}>Cancel</Button></Show>
        <Button type="submit" size="sm" disabled={mutations.post.isPending}>{props.parentId ? "Reply" : "Post"}</Button>
      </div>
    </form>
  )
}

/** A thread window: flat ancestors above, the nested tree below, deeper windows on demand. */
function ThreadView(props: { postId: number; context: FeedContext; onClose: () => void }) {
  const [focus, setFocus] = createSignal(props.postId)
  const thread = useThread(focus)
  const pages = () => thread.data?.pages ?? []
  const tree = createMemo(() => mergeThread(pages()))
  const ancestors = () => pages()[0]?.ancestors ?? []
  const context: FeedContext = { ...props.context, openThread: setFocus }

  return (
    <section class="mt-4" aria-label="Thread">
      <div class="mb-2 flex items-center gap-2">
        <Button type="button" size="sm" variant="outline" onClick={props.onClose}>Back to timeline</Button>
        <Show when={pages()[0] && pages()[0].top_id !== pages()[0].root_id}>
          <Button type="button" size="sm" variant="ghost" onClick={() => setFocus(pages()[0].root_id)}>Go to root</Button>
        </Show>
      </div>
      <Show when={thread.isError}><p class="text-sm text-rust">{getErrorMessage(thread.error, "Could not load the thread.")}</p></Show>
      <Show when={thread.isPending && !thread.isError}><p class="text-sm text-cream-dim">Loading…</p></Show>
      <Show when={ancestors().length > 0}>
        <ol class="mb-2 flex flex-col gap-1 border-l border-line pl-3 text-xs text-cream-dim" aria-label="Earlier in the thread">
          <For each={ancestors()}>
            {(post) => (
              <li>
                <button type="button" class="hover:text-cream" onClick={() => setFocus(post.id)}>
                  {post.deleted ? "(deleted)" : `${post.author}: ${post.body.slice(0, 80)}`}
                </button>
              </li>
            )}
          </For>
        </ol>
      </Show>
      <ul class="flex flex-col gap-3">
        <For each={tree()}>{(node) => <li><ThreadBranch node={node} context={context} /></li>}</For>
      </ul>
      <Show when={thread.hasNextPage}>
        <Button type="button" class="mt-3" variant="outline" size="sm" disabled={thread.isFetchingNextPage} onClick={() => void thread.fetchNextPage()}>
          Load more replies
        </Button>
      </Show>
    </section>
  )
}

/** A node and its replies, collapsible. Depth is bounded by the hub's thread window. */
function ThreadBranch(props: { node: ThreadNode; context: FeedContext }) {
  const [collapsed, setCollapsed] = createSignal(false)
  return (
    <div>
      <PostCard post={props.node} context={props.context} compact />
      <div class="mt-1 flex gap-3 text-xs text-cream-dim">
        <Show when={props.node.replies.length > 0}>
          <button type="button" class="hover:text-cream" aria-expanded={!collapsed()} onClick={() => setCollapsed(!collapsed())}>
            {collapsed() ? `Show ${props.node.replies.length} replies` : "Hide replies"}
          </button>
        </Show>
        <Show when={props.node.more_replies}>
          <button type="button" class="text-brass hover:underline" onClick={() => props.context.openThread(props.node.id)}>Continue thread</button>
        </Show>
      </div>
      <Show when={!collapsed() && props.node.replies.length > 0}>
        <ul class="ml-3 mt-2 flex flex-col gap-2 border-l border-line pl-3">
          <For each={props.node.replies}>{(reply) => <li><ThreadBranch node={reply} context={props.context} /></li>}</For>
        </ul>
      </Show>
    </div>
  )
}

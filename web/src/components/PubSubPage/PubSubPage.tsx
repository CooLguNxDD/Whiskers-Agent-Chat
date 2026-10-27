import { createMemo, createSignal, For, Show } from "solid-js"
import { useSearchParams } from "@solidjs/router"
import { PATTERN_RE, TOPIC_RE, topicMatches } from "@/api/pubsub"
import type { Publication, TopicSubscription } from "@/api/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { usePublications, usePubSubMutations, useSubscriptions } from "@/hooks/usePubSub"
import { usePrefs } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { isHandle } from "@/utils/handles"
import { formatStamp } from "@/utils/time"

/** Topic pub/sub: live publication history, subscriptions, and a publish form. Updates from `topic.*` events. */
export default function PubSubPage() {
  const [params, setParams] = useSearchParams<{ pattern?: string }>()
  const pattern = () => params.pattern ?? ""
  const [patternDraft, setPatternDraft] = createSignal(pattern())
  const [patternError, setPatternError] = createSignal("")
  const publications = usePublications(pattern)
  const subscriptions = useSubscriptions()
  const rows = () => publications.data?.pages.flatMap((page) => page.publications) ?? []

  return (
    <div class="min-h-0 flex-1 overflow-y-auto p-4">
      <div class="mx-auto max-w-6xl">
        <h1 class="font-display text-xl text-cream">Pub/Sub</h1>
        <p class="mt-1 text-sm text-cream-dim">
          Topics are dotted lowercase names. In a pattern <code>*</code> is one segment and a trailing <code>&gt;</code> is the rest.
        </p>
        <form
          class="mt-3 flex gap-2"
          role="search"
          onSubmit={(event) => {
            event.preventDefault()
            const value = patternDraft().trim()
            if (value && !PATTERN_RE.test(value)) { setPatternError("Not a valid pattern."); return }
            setPatternError("")
            setParams({ pattern: value || undefined })
          }}
        >
          <Input aria-label="Filter by pattern" placeholder="Filter, e.g. ci.*.done or ci.>" value={patternDraft()} onInput={(event) => setPatternDraft(event.currentTarget.value)} />
          <Button type="submit">Filter</Button>
          <Show when={pattern()}><Button type="button" variant="ghost" onClick={() => { setPatternDraft(""); setParams({ pattern: undefined }) }}>Clear</Button></Show>
        </form>
        <Show when={patternError()}><p class="mt-1 text-sm text-rust">{patternError()}</p></Show>

        <div class="mt-5 grid gap-6 lg:grid-cols-[1fr_20rem]">
          <section aria-label="Publications" class="min-w-0">
            <h2 class="mb-2 font-display text-lg text-cream">{pattern() ? `Publications on ${pattern()}` : "All publications"}</h2>
            <Show when={publications.isError}><p class="text-sm text-rust">{getErrorMessage(publications.error, "Could not load publications.")}</p></Show>
            <Show when={publications.isSuccess && rows().length === 0}><p class="text-sm text-cream-dim">Nothing published yet.</p></Show>
            <ul class="flex flex-col gap-2">
              <For each={rows()}>
                {(item) => <PublicationRow item={item} subscriptions={subscriptions.data?.subscriptions ?? []} onTopic={(topic) => { setPatternDraft(topic); setParams({ pattern: topic }) }} />}
              </For>
            </ul>
            <Show when={publications.hasNextPage}>
              <Button type="button" class="mt-3" variant="outline" size="sm" disabled={publications.isFetchingNextPage} onClick={() => void publications.fetchNextPage()}>
                Load older
              </Button>
            </Show>
          </section>

          <div class="flex flex-col gap-4">
            <PublishForm />
            <Subscriptions rows={subscriptions.data?.subscriptions ?? []} error={subscriptions.isError ? getErrorMessage(subscriptions.error, "Could not load subscriptions.") : ""} />
          </div>
        </div>
      </div>
    </div>
  )
}

function PublicationRow(props: { item: Publication; subscriptions: TopicSubscription[]; onTopic: (topic: string) => void }) {
  const receivers = createMemo(() => [
    ...new Set(
      props.subscriptions
        .filter((sub) => sub.subscriber !== props.item.publisher && topicMatches(sub.pattern, props.item.topic))
        .map((sub) => sub.subscriber),
    ),
  ])
  const body = () => JSON.stringify(props.item.data, null, 2)
  return (
    <li class="rounded-lg border border-line p-3 text-sm">
      <div class="flex flex-wrap items-center gap-2 text-xs text-cream-dim">
        <button type="button" class="font-mono text-brass hover:underline" onClick={() => props.onTopic(props.item.topic)}>{props.item.topic}</button>
        <span>by {props.item.publisher}</span>
        <span title={props.item.created_at}>{formatStamp(props.item.created_at)}</span>
        <span class="ml-auto">#{props.item.id}</span>
      </div>
      <pre class="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-black/20 p-2 font-mono text-xs text-cream">{body()}</pre>
      <Show when={receivers().length > 0}>
        <p class="mt-1 text-xs text-cream-dim">Subscribers now: {receivers().join(", ")}</p>
      </Show>
    </li>
  )
}

function PublishForm() {
  const prefs = usePrefs()
  const mutations = usePubSubMutations()
  const [topic, setTopic] = createSignal("")
  const [data, setData] = createSignal("")
  const [error, setError] = createSignal("")
  const [note, setNote] = createSignal("")
  const publisher = () => (isHandle(prefs.displayName().trim()) ? prefs.displayName().trim() : "")

  return (
    <form
      class="flex flex-col gap-2 rounded-lg border border-line p-3"
      aria-label="Publish"
      onSubmit={(event) => {
        event.preventDefault()
        setError("")
        setNote("")
        if (!publisher()) { setError("Set a valid display name first."); return }
        if (!TOPIC_RE.test(topic().trim())) { setError("Enter a topic like ci.whiskers.done."); return }
        let payload: unknown = null
        if (data().trim()) {
          try { payload = JSON.parse(data()) } catch { setError("Data must be JSON (or empty)."); return }
        }
        mutations.publish.mutate(
          { publisher: publisher(), topic: topic().trim(), data: payload },
          {
            onSuccess: (result) => {
              setData("")
              const count = result.subscribers.length
              setNote(count ? `Published to ${count} subscriber${count === 1 ? "" : "s"}: ${result.subscribers.join(", ")}.` : "Published. No subscribers yet.")
            },
            onError: (reason) => setError(getErrorMessage(reason, "Not published. Your draft is still here.")),
          },
        )
      }}
    >
      <h2 class="font-display text-lg text-cream">Publish</h2>
      <Input aria-label="Topic" placeholder="ci.whiskers.done" value={topic()} onInput={(event) => setTopic(event.currentTarget.value)} />
      <Textarea aria-label="Data" placeholder='{"ok": true}' class="font-mono" value={data()} onInput={(event) => setData(event.currentTarget.value)} />
      <div class="flex justify-end"><Button type="submit" disabled={mutations.publish.isPending}>Publish</Button></div>
      <Show when={error()}><p class="text-sm text-rust">{error()}</p></Show>
      <Show when={note()}><p class="text-xs text-cream-dim" role="status">{note()}</p></Show>
    </form>
  )
}

function Subscriptions(props: { rows: TopicSubscription[]; error: string }) {
  const prefs = usePrefs()
  const mutations = usePubSubMutations()
  const [pattern, setPattern] = createSignal("")
  const [error, setError] = createSignal("")
  const me = () => (isHandle(prefs.displayName().trim()) ? prefs.displayName().trim() : "")
  const fail = (reason: unknown) => setError(getErrorMessage(reason))

  return (
    <section class="flex flex-col gap-2 rounded-lg border border-line p-3" aria-label="Subscriptions">
      <h2 class="font-display text-lg text-cream">Subscriptions</h2>
      <form
        class="flex gap-1"
        onSubmit={(event) => {
          event.preventDefault()
          setError("")
          if (!me()) { setError("Set a valid display name first."); return }
          if (!PATTERN_RE.test(pattern().trim())) { setError("Enter a pattern like ci.*.done or ci.>."); return }
          mutations.subscribe.mutate({ agent: me(), pattern: pattern().trim() }, { onSuccess: () => setPattern(""), onError: fail })
        }}
      >
        <Input class="h-8" aria-label="Subscribe pattern" placeholder={me() ? `subscribe ${me()} to…` : "set a name first"} value={pattern()} onInput={(event) => setPattern(event.currentTarget.value)} />
        <Button type="submit" size="sm" variant="outline" disabled={mutations.subscribe.isPending}>Subscribe</Button>
      </form>
      <Show when={error() || props.error}><p class="text-xs text-rust">{error() || props.error}</p></Show>
      <Show when={props.rows.length === 0 && !props.error}><p class="text-sm text-cream-dim">No subscriptions.</p></Show>
      <ul class="flex flex-col gap-1 text-sm">
        <For each={props.rows}>
          {(row) => (
            <li class="flex items-center gap-2">
              <Badge classList={{ "text-brass": row.subscriber === me() }}>{row.subscriber}</Badge>
              <span class="min-w-0 flex-1 truncate font-mono text-xs text-cream" title={row.pattern}>{row.pattern}</span>
              <Show when={row.subscriber === me()}>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  disabled={mutations.unsubscribe.isPending}
                  onClick={() => { setError(""); mutations.unsubscribe.mutate({ agent: row.subscriber, pattern: row.pattern }, { onError: fail }) }}
                >
                  Unsubscribe
                </Button>
              </Show>
            </li>
          )}
        </For>
      </ul>
    </section>
  )
}

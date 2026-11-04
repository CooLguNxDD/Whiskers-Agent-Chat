import { createMemo, createSignal, For, Show } from "solid-js"
import { useSearchParams } from "@solidjs/router"
import { PATTERN_RE, TOPIC_RE, topicMatches } from "@/api/pubsub"
import type { Publication, TopicSubscription } from "@/api/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { usePublications, usePubSubMutations, useSubscriptions } from "@/hooks/usePubSub"
import { usePrefs } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { isHandle } from "@/utils/handles"
import { formatStamp } from "@/utils/time"
import { InfraTabs, PageHeader, SideCard } from "@/components/ui/page"
import { Icon } from "@/components/ui/icons"
import { relativeTime } from "@/utils/fleetStatus"

/** Topic pub/sub: live publication history, subscriptions, and a publish form. Updates from `topic.*` events. */
export default function PubSubPage() {
  const [params, setParams] = useSearchParams<{ pattern?: string }>()
  const pattern = () => params.pattern ?? ""
  const [patternDraft, setPatternDraft] = createSignal(pattern())
  const [patternError, setPatternError] = createSignal("")
  const publications = usePublications(pattern)
  const subscriptions = useSubscriptions()
  const rows = () => publications.data?.pages.flatMap((page) => page.publications) ?? []

  const [publishing, setPublishing] = createSignal(false)
  const topicCount = () => new Set(rows().map((item) => item.topic)).size

  return (
    <div class="page min-h-0 flex-1 overflow-y-auto">
      <div class="mx-auto max-w-6xl">
        <InfraTabs />
        <PageHeader
          eyebrow="Events & coordination"
          title="The fleet's shared signal."
          description="Inspect events and route the right updates to the right cats."
          stats={[
            { value: `${rows().length}${publications.hasNextPage ? "+" : ""}`, label: "publications" },
            { value: subscriptions.data?.subscriptions.length ?? 0, label: "subscriptions" },
            { value: topicCount(), label: "topics in loaded events" },
          ]}
        >
          <Button type="button" class="h-10 px-4" variant={publishing() ? "outline" : "default"} aria-expanded={publishing()} onClick={() => setPublishing((open) => !open)}>
            {publishing() ? "Close" : "＋ Publish event"}
          </Button>
        </PageHeader>
        <Show when={publishing()}><div class="mb-6 max-w-2xl"><PublishForm /></div></Show>

        <div class="grid gap-8 lg:grid-cols-[minmax(0,1fr)_18.5rem]">
          <section aria-label="Publications" class="min-w-0">
            <form
              class="flex gap-2"
              role="search"
              onSubmit={(event) => {
                event.preventDefault()
                const value = patternDraft().trim()
                if (value && !PATTERN_RE.test(value)) { setPatternError("Not a valid pattern."); return }
                setPatternError("")
                setParams({ pattern: value || undefined })
              }}
            >
              <Input class="h-11" aria-label="Filter by pattern" placeholder="Filter a topic, e.g. ci.*.done or fleet.>" value={patternDraft()} onInput={(event) => setPatternDraft(event.currentTarget.value)} />
              <Button type="submit" variant="outline" class="h-11">Filter</Button>
              <Show when={pattern()}><Button type="button" variant="ghost" class="h-11" onClick={() => { setPatternDraft(""); setParams({ pattern: undefined }) }}>Clear</Button></Show>
            </form>
            <Show when={patternError()}><p class="mt-1 text-sm text-rust">{patternError()}</p></Show>
            <h2 class="mb-1 mt-6 flex items-center justify-between text-[15px] font-semibold text-cream">
              {pattern() ? `Events on ${pattern()}` : "Event stream"}
              <span class="text-xs font-normal text-cream-dim">{rows().length} event{rows().length === 1 ? "" : "s"}</span>
            </h2>
            <Show when={publications.isError}><p class="text-sm text-rust">{getErrorMessage(publications.error, "Could not load publications.")}</p></Show>
            <Show when={publications.isSuccess && rows().length === 0}><p class="py-8 text-sm text-cream-dim">Nothing published yet.</p></Show>
            <ul class="flex flex-col">
              <For each={rows()}>
                {(item) => <PublicationRow item={item} subscriptions={subscriptions.data?.subscriptions ?? []} onTopic={(topic) => { setPatternDraft(topic); setParams({ pattern: topic }) }} />}
              </For>
            </ul>
            <Show when={publications.hasNextPage}>
              <Button type="button" class="mt-4" variant="outline" size="sm" disabled={publications.isFetchingNextPage} onClick={() => void publications.fetchNextPage()}>
                Load older
              </Button>
            </Show>
          </section>

          <div class="flex flex-col gap-5">
            <Subscriptions rows={subscriptions.data?.subscriptions ?? []} error={subscriptions.isError ? getErrorMessage(subscriptions.error, "Could not load subscriptions.") : ""} />
            <SideCard title="A little pattern guide">
              <dl class="flex flex-col gap-3">
                <div><dt class="font-mono text-cream">ci.build.done</dt><dd>One exact topic.</dd></div>
                <div><dt class="font-mono text-cream">ci.*.done</dt><dd><code>*</code> matches one segment.</dd></div>
                <div><dt class="font-mono text-cream">fleet.&gt;</dt><dd>A trailing <code>&gt;</code> matches the remaining segments.</dd></div>
              </dl>
              <p class="mt-3">Use lowercase names, separated by dots. Subscribers get matching events through their event wait; a publisher never hears its own.</p>
            </SideCard>
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
    <li class="list-row flex gap-3.5 py-4 text-sm">
      <span class="icon-tile size-9 shrink-0" aria-hidden="true"><Icon name="arrow" class="size-4" /></span>
      <div class="min-w-0 flex-1">
        <div class="flex items-start gap-2">
          <button type="button" class="min-w-0 truncate text-left font-medium text-cream hover:text-brass" onClick={() => props.onTopic(props.item.topic)} title={`Filter on ${props.item.topic}`}>{props.item.topic}</button>
          <span class="ml-auto shrink-0 text-xs text-cream-dim" title={props.item.created_at}>{relativeTime(props.item.created_at) || formatStamp(props.item.created_at)}</span>
        </div>
        <p class="mt-0.5 text-xs text-cream-dim">
          Published by {props.item.publisher} · #{props.item.id}
          <Show when={receivers().length > 0}> · currently matched by {receivers().join(", ")}</Show>
        </p>
        <details class="provenance mt-2 text-xs">
          <summary class="cursor-pointer select-none text-cream-dim hover:text-cream">View payload</summary>
          <pre class="code-block mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg p-3 font-mono text-xs">{body()}</pre>
        </details>
      </div>
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
      class="card flex flex-col gap-2 rounded-2xl border border-line p-5"
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
      <h2 class="text-[15px] font-semibold text-cream">Publish an event</h2>
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
    <section class="card side-card flex flex-col gap-3 rounded-2xl border border-line p-5" aria-label="Subscriptions">
      <div>
        <h2 class="text-[15px] font-semibold text-cream">Subscriptions</h2>
        <p class="mt-1 text-[13px] text-cream-dim">Patterns that keep cats in the loop.</p>
      </div>
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
        <Input class="h-9" aria-label="Subscribe pattern" placeholder={me() ? "fleet.>" : "set a name first"} value={pattern()} onInput={(event) => setPattern(event.currentTarget.value)} />
        <Button type="submit" variant="outline" class="h-9" disabled={mutations.subscribe.isPending}>Add</Button>
      </form>
      <Show when={error() || props.error}><p class="text-xs text-rust">{error() || props.error}</p></Show>
      <Show when={props.rows.length === 0 && !props.error}><p class="text-sm text-cream-dim">No subscriptions.</p></Show>
      <ul class="flex flex-col text-sm">
        <For each={[...props.rows].sort((a, b) => Number(b.subscriber === me()) - Number(a.subscriber === me()))}>
          {(row) => (
            <li class="list-row flex items-center gap-2 py-2.5">
              <span class="min-w-0 flex-1">
                <span class="block truncate font-mono text-[13px] text-cream" title={row.pattern}>{row.pattern}</span>
                <span class="text-xs text-cream-dim" classList={{ "text-brass": row.subscriber === me() }}>{row.subscriber === me() ? "you" : row.subscriber}</span>
              </span>
              <Show when={row.subscriber === me()}>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  class="h-7 px-2"
                  aria-label={`Unsubscribe from ${row.pattern}`}
                  title="Unsubscribe"
                  disabled={mutations.unsubscribe.isPending}
                  onClick={() => { setError(""); mutations.unsubscribe.mutate({ agent: row.subscriber, pattern: row.pattern }, { onError: fail }) }}
                >
                  ×
                </Button>
              </Show>
            </li>
          )}
        </For>
      </ul>
    </section>
  )
}

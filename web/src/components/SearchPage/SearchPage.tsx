import { createEffect, createMemo, createSignal, For, Show } from "solid-js"
import { useNavigate } from "@solidjs/router"
import { SEARCH_KINDS, type SearchHit, type SearchKind, type SearchMode, type SearchQuery } from "@/api/types"
import { selectClass, Snippet } from "@/components/RichText"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useChannels } from "@/hooks/useChannels"
import { useSearch, useSearchStatus } from "@/hooks/useSearch"
import { useUIStore } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { effectiveMode, isEmbedderError, searchModeOptions } from "@/utils/searchModes"
import { formatStamp } from "@/utils/time"

const PAGE = 20

/** Global search over messages, tasks, attachments and knowledge, with a BM25/vector/hybrid toggle. */
export default function SearchPage() {
  const status = useSearchStatus()
  const channels = useChannels()
  const [q, setQ] = createSignal("")
  const [kind, setKind] = createSignal<SearchKind | "">("")
  const [channel, setChannel] = createSignal("")
  const [author, setAuthor] = createSignal("")
  const [since, setSince] = createSignal("")
  const [mode, setMode] = createSignal<SearchMode>("bm25")
  const [submitted, setSubmitted] = createSignal<SearchQuery | null>(null)
  const results = useSearch(submitted)
  // Sticky: once the hub says the embedder cannot serve a query, stay on BM25 until the page reloads.
  const [embedderError, setEmbedderError] = createSignal<unknown>(null)
  createEffect(() => {
    if (isEmbedderError(results.error)) {
      setEmbedderError(results.error)
      setMode("bm25")
    }
  })
  const options = createMemo(() =>
    searchModeOptions({ status: status.data, statusError: status.error, searchError: embedderError() }),
  )
  const activeMode = () => effectiveMode(mode(), options())
  const hint = () => options().find((option) => option.mode !== "bm25")?.hint

  const run = (offset = 0, nextMode: SearchMode = activeMode()) => {
    if (!q().trim()) { setSubmitted(null); return }
    setSubmitted({
      q: q().trim(),
      kind: kind() || undefined,
      channel: channel() || undefined,
      author: author().trim() || undefined,
      since: since().trim() || undefined,
      mode: nextMode,
      limit: PAGE,
      offset,
    })
  }

  return (
    <div class="min-h-0 flex-1 overflow-y-auto p-4">
      <div class="mx-auto max-w-4xl">
        <h1 class="font-display text-xl text-cream">Search</h1>
        <form class="mt-3 flex flex-col gap-3" role="search" onSubmit={(event) => { event.preventDefault(); run() }}>
          <div class="flex gap-2">
            <Input aria-label="Search query" placeholder="Search messages, tasks, files and knowledge" value={q()} onInput={(event) => setQ(event.currentTarget.value)} />
            <Button type="submit">Search</Button>
          </div>
          <div class="flex flex-wrap items-center gap-2 text-sm">
            <select class={selectClass} aria-label="Kind" value={kind()} onChange={(event) => setKind(event.currentTarget.value as SearchKind | "")}>
              <option value="">All kinds</option>
              <For each={SEARCH_KINDS}>{(item) => <option value={item}>{item}</option>}</For>
            </select>
            <select class={selectClass} aria-label="Channel" value={channel()} onChange={(event) => setChannel(event.currentTarget.value)}>
              <option value="">All channels</option>
              <For each={channels.data?.channels ?? []}>{(item) => <option value={item.name}>#{item.name}</option>}</For>
            </select>
            <Input class="w-36" aria-label="Author" placeholder="Author" value={author()} onInput={(event) => setAuthor(event.currentTarget.value)} />
            <Input class="w-28" aria-label="Since" placeholder="Since (24h)" value={since()} onInput={(event) => setSince(event.currentTarget.value)} />
            <div role="radiogroup" aria-label="Search mode" class="ml-auto flex overflow-hidden rounded-lg border border-line">
              <For each={options()}>
                {(option) => (
                  <button
                    type="button"
                    role="radio"
                    aria-checked={activeMode() === option.mode}
                    disabled={!option.enabled}
                    title={option.hint ?? undefined}
                    class="px-3 py-1.5 text-xs disabled:cursor-not-allowed disabled:opacity-40"
                    classList={{ "bg-brass text-on-accent": activeMode() === option.mode, "text-cream-dim hover:text-cream": activeMode() !== option.mode }}
                    onClick={() => { setMode(option.mode); if (submitted()) run(0, option.mode) }}
                  >
                    {option.mode}
                  </button>
                )}
              </For>
            </div>
          </div>
          <Show when={hint()}><p class="text-xs text-cream-dim" role="note">{hint()}</p></Show>
        </form>

        <section class="mt-5" aria-live="polite">
          <Show when={results.isFetching}><p class="text-sm text-cream-dim">Searching…</p></Show>
          <Show when={results.isError}><p class="text-sm text-rust">{getErrorMessage(results.error, "Search failed.")}</p></Show>
          <Show when={results.data}>
            {(page) => (
              <>
                <p class="mb-2 text-xs text-cream-dim">
                  About {page().total_estimate} results · {page().mode}
                  <Show when={page().degraded}> · embedder failed, showing BM25 results</Show>
                </p>
                <Show when={page().results.length > 0} fallback={<p class="text-sm text-cream-dim">No results.</p>}>
                  <ol class="flex flex-col gap-2">
                    <For each={page().results}>{(hit) => <HitRow hit={hit} />}</For>
                  </ol>
                </Show>
                <div class="mt-3 flex gap-2">
                  <Show when={page().offset > 0}>
                    <Button type="button" variant="outline" size="sm" onClick={() => run(Math.max(0, page().offset - PAGE))}>Previous</Button>
                  </Show>
                  <Show when={page().next_offset != null}>
                    <Button type="button" variant="outline" size="sm" onClick={() => run(page().next_offset ?? 0)}>Next</Button>
                  </Show>
                </div>
              </>
            )}
          </Show>
        </section>
      </div>
    </div>
  )
}

function HitRow(props: { hit: SearchHit }) {
  const ui = useUIStore()
  const navigate = useNavigate()
  const open = () => {
    const hit = props.hit
    if (hit.kind === "knowledge") {
      const params = new URLSearchParams()
      if (hit.project) params.set("project", hit.project)
      if (hit.topic) params.set("topic", hit.topic)
      navigate(`/knowledge?${params}`)
      return
    }
    if (hit.channel) ui.setActiveChannel(hit.channel)
    if (hit.kind === "task") {
      ui.setExpandedTaskId(hit.id)
      navigate("/tasks")
    } else {
      navigate("/")
    }
  }
  const where = () => {
    const hit = props.hit
    if (hit.kind === "knowledge") return [hit.project, hit.topic].filter(Boolean).join(" / ")
    return hit.channel ? `#${hit.channel}` : ""
  }
  return (
    <li class="rounded-lg border border-line p-3">
      <div class="flex flex-wrap items-center gap-2 text-xs text-cream-dim">
        <Badge>{props.hit.kind}</Badge>
        <span>{where()}</span>
        <Show when={props.hit.author}><span>{props.hit.author}</span></Show>
        <span title={props.hit.created_at}>{formatStamp(props.hit.created_at)}</span>
        <span class="ml-auto font-mono" title="score">{props.hit.score.toPrecision(3)}</span>
      </div>
      <p class="mt-1 text-sm text-cream"><Snippet text={props.hit.snippet} /></p>
      <button type="button" class="mt-1 text-xs text-brass hover:underline" onClick={open}>
        Open {props.hit.kind} #{props.hit.id}
      </button>
    </li>
  )
}

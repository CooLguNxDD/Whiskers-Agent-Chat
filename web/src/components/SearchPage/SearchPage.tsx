import { createEffect, createMemo, createSignal, For, Show } from "solid-js"
import { useNavigate, useSearchParams } from "@solidjs/router"
import { getAttachment } from "@/api/fleet"
import { SEARCH_KINDS, type SearchHit, type SearchKind, type SearchMode, type SearchQuery } from "@/api/types"
import { selectClass, Snippet } from "@/components/RichText"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useChannels } from "@/hooks/useChannels"
import { useSearch, useSearchStatus } from "@/hooks/useSearch"
import { useUIStore } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { effectiveMode, isEmbedderError, searchModeOptions } from "@/utils/searchModes"
import { formatStamp } from "@/utils/time"
import { PageHeader } from "@/components/ui/page"
import { Icon } from "@/components/ui/icons"
import { relativeTime } from "@/utils/fleetStatus"

const PAGE = 20

/** Global search over messages, tasks, attachments and knowledge, with a BM25/vector/hybrid toggle. */
export default function SearchPage() {
  const status = useSearchStatus()
  const channels = useChannels()
  // "All history" from the chat find bar arrives as ?q=…&channel=…
  const [params] = useSearchParams()
  const param = (value: string | string[] | undefined) => (Array.isArray(value) ? value[0] : value) ?? ""
  const [q, setQ] = createSignal(param(params.q))
  const [kind, setKind] = createSignal<SearchKind | "">("")
  const [channel, setChannel] = createSignal(param(params.channel))
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

  if (q().trim()) queueMicrotask(() => run())

  return (
    <div class="page min-h-0 flex-1 overflow-y-auto">
      <div class="mx-auto max-w-5xl">
        <PageHeader eyebrow="One search, the whole fleet" title="Find the useful bit." description="Search conversations, tasks, files and knowledge in one place." />
        <form class="flex flex-col gap-3" role="search" onSubmit={(event) => { event.preventDefault(); run() }}>
          <div class="flex gap-3">
            <div class="search-field flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-line px-4">
              <Icon name="search" class="size-4 text-cream-dim" />
              <input class="h-12 min-w-0 flex-1 bg-transparent text-[15px] text-cream outline-none placeholder:text-cream-dim" aria-label="Search query" placeholder="Search messages, tasks, files and knowledge" value={q()} onInput={(event) => setQ(event.currentTarget.value)} />
            </div>
            <Button type="submit" class="h-12 px-5">Search ↗</Button>
          </div>
          <div class="flex flex-wrap items-center gap-2 text-sm">
            <select class={selectClass} aria-label="Kind" value={kind()} onChange={(event) => setKind(event.currentTarget.value as SearchKind | "")}>
              <option value="">All types</option>
              <For each={SEARCH_KINDS}>{(item) => <option value={item}>{item}</option>}</For>
            </select>
            <select class={selectClass} aria-label="Channel" value={channel()} onChange={(event) => setChannel(event.currentTarget.value)}>
              <option value="">All channels</option>
              <For each={channels.data?.channels ?? []}>{(item) => <option value={item.name}>#{item.name}</option>}</For>
            </select>
            <Input class="w-36" aria-label="Author" placeholder="Any author" value={author()} onInput={(event) => setAuthor(event.currentTarget.value)} />
            <select class={selectClass} aria-label="Since" value={since()} onChange={(event) => setSince(event.currentTarget.value)}>
              <option value="">Any time</option>
              <option value="24h">Last 24 hours</option>
              <option value="7d">Last 7 days</option>
              <option value="30d">Last 30 days</option>
            </select>
          </div>
          <Show when={hint()}><p class="text-xs text-cream-dim" role="note">{hint()}</p></Show>
        </form>

        <section class="mt-7" aria-live="polite">
          <div class="mb-1 flex flex-wrap items-center gap-3 text-[13px] text-cream-dim">
            <span>
              <Show when={results.isFetching} fallback={
                <Show when={results.data} fallback={<>Type a query and press Enter.</>}>
                  {(page) => <>About {page().total_estimate} result{page().total_estimate === 1 ? "" : "s"} for “{submitted()?.q}”<Show when={page().degraded}> · embedder failed, showing keyword results</Show></>}
                </Show>
              }>Searching…</Show>
            </span>
            <div role="radiogroup" aria-label="Search mode" class="segmented ml-auto flex rounded-lg border border-line p-0.5">
              <For each={options()}>
                {(option) => (
                  <button
                    type="button"
                    role="radio"
                    aria-checked={activeMode() === option.mode}
                    aria-pressed={activeMode() === option.mode}
                    disabled={!option.enabled}
                    title={option.hint ?? undefined}
                    class="rounded-md px-2.5 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-40"
                    onClick={() => { setMode(option.mode); if (submitted()) run(0, option.mode) }}
                  >
                    {MODE_LABEL[option.mode]}
                  </button>
                )}
              </For>
            </div>
          </div>
          <Show when={results.isError}><p class="mt-3 text-sm text-rust">{getErrorMessage(results.error, "Search failed.")}</p></Show>
          <Show when={results.data}>
            {(page) => (
              <>
                <Show when={page().results.length > 0} fallback={<p class="mt-6 text-sm text-cream-dim">No results. Try fewer words or another type.</p>}>
                  <ol class="flex flex-col">
                    <For each={page().results}>{(hit) => <HitRow hit={hit} />}</For>
                  </ol>
                </Show>
                <div class="mt-4 flex gap-2">
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

const MODE_LABEL: Record<SearchMode, string> = { bm25: "Keyword", vector: "Semantic", hybrid: "Hybrid" }

function HitRow(props: { hit: SearchHit }) {
  const ui = useUIStore()
  const navigate = useNavigate()
  const [openError, setOpenError] = createSignal("")
  const open = async () => {
    const hit = props.hit
    setOpenError("")
    if (hit.kind === "knowledge") {
      const params = new URLSearchParams()
      if (hit.project) params.set("project", hit.project)
      if (hit.topic) params.set("topic", hit.topic)
      navigate(`/knowledge?${params}`)
      return
    }
    if (!hit.channel) { setOpenError("This result has no channel to open."); return }
    if (hit.kind === "task") {
      ui.setActiveChannel(hit.channel)
      ui.setExpandedTaskId(hit.id)
      navigate("/tasks")
      return
    }
    // Messages open in the chat scrolled to the hit; an attachment opens the message carrying it.
    let messageId = hit.id
    if (hit.kind === "attachment") {
      try {
        messageId = (await getAttachment(hit.id)).attachment.message_id
      } catch (err) {
        setOpenError(getErrorMessage(err, "Could not find the attachment's message."))
        return
      }
    }
    ui.focusMessage(hit.channel, messageId)
    navigate("/")
  }
  const where = () => {
    const hit = props.hit
    if (hit.kind === "knowledge") return [hit.project, hit.topic].filter(Boolean).join(" / ")
    return hit.channel ? `#${hit.channel}` : ""
  }
  return (
    <li class="list-row py-5">
      <p class="field-label">{props.hit.kind}</p>
      <button type="button" class="hit-title mt-1.5 text-left text-[15.5px] font-semibold leading-snug text-cream hover:text-brass" onClick={() => void open()} title={`Open ${props.hit.kind} #${props.hit.id}`}>
        <Snippet text={props.hit.snippet} /> <span class="text-cream-dim" aria-hidden="true">↗</span>
      </button>
      <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-cream-dim">
        <Show when={where()}><span>{where()}</span></Show>
        <Show when={props.hit.author}><span>{props.hit.author}</span></Show>
        <span title={props.hit.created_at}>{relativeTime(props.hit.created_at) || formatStamp(props.hit.created_at)}</span>
        <button type="button" class="text-brass hover:underline" onClick={() => void open()}>
          Open {props.hit.kind} #{props.hit.id}
        </button>
        <span class="ml-auto font-mono opacity-70" title="Relevance score">{props.hit.score.toPrecision(3)}</span>
      </div>
      <Show when={openError()}><p class="mt-1 text-xs text-rust">{openError()}</p></Show>
    </li>
  )
}

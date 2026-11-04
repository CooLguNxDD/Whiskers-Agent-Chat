import { createEffect, createSignal, For, Show } from "solid-js"
import { useSearchParams } from "@solidjs/router"
import { exportFacts } from "@/api/knowledge"
import { FACT_STATUSES, type FactStatus, type KnowledgeFact } from "@/api/types"
import { selectClass, Snippet } from "@/components/RichText"
import { MessageBody } from "@/components/MessageRow/MessageRow"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { useFacts, useFactSearch, useKnowledgeMutations, useKnowledgeProjects, useKnowledgeTopics } from "@/hooks/useKnowledge"
import { usePrefs } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { isHandle } from "@/utils/handles"
import { formatStamp } from "@/utils/time"
import { relativeTime } from "@/utils/fleetStatus"
import { PageHeader } from "@/components/ui/page"

const PROJECT_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

/** Knowledge facts: browse by project then topic, search active facts, add new ones. */
export default function KnowledgePage() {
  const [params, setParams] = useSearchParams<{ project?: string; topic?: string }>()
  const project = () => params.project ?? ""
  const topic = () => params.topic ?? ""
  const [status, setStatus] = createSignal<FactStatus | "">("active")
  const [projectDraft, setProjectDraft] = createSignal("")
  const [query, setQuery] = createSignal("")
  const [submittedQuery, setSubmittedQuery] = createSignal("")
  const [exportError, setExportError] = createSignal("")
  const projects = useKnowledgeProjects()
  const topics = useKnowledgeTopics(project)
  const facts = useFacts(() =>
    project() ? { project: project(), topic: topic() || undefined, status: status() || undefined } : null,
  )
  const found = useFactSearch(submittedQuery)
  const rows = () => facts.data?.pages.flatMap((page) => page.facts) ?? []
  const pickProject = (value: string) => setParams({ project: value || undefined, topic: undefined })

  const download = async (scope: string | undefined) => {
    setExportError("")
    try {
      const text = await exportFacts({ project: scope, status: status() || "all" })
      const url = URL.createObjectURL(new Blob([text], { type: "application/x-ndjson" }))
      const link = document.createElement("a")
      link.href = url
      link.download = `knowledge-${scope || "all"}.jsonl`
      link.click()
      URL.revokeObjectURL(url)
    } catch (reason) {
      setExportError(getErrorMessage(reason, "Export failed."))
    }
  }

  // Open the first project when none is chosen, so the page never starts empty.
  createEffect(() => {
    const first = projects.data?.[0]
    if (!project() && first && !submittedQuery()) setParams({ project: first }, { replace: true })
  })
  const [adding, setAdding] = createSignal(false)
  const topicTotal = () => (topics.data?.topics ?? []).reduce((sum, item) => sum + item.count, 0)

  return (
    <div class="page min-h-0 flex-1 overflow-y-auto">
      <div class="mx-auto max-w-6xl">
        <PageHeader eyebrow="Fleet knowledge" title="Remember what matters." description="Useful lessons, with the context and sources to trust them. Agents read these before they start work.">
          <Button type="button" variant="outline" class="h-10 px-4" title="Every project, with the status filter applied" onClick={() => void download(undefined)}>Export all ↓</Button>
          <Button type="button" class="h-10 px-4" variant={adding() ? "outline" : "default"} aria-expanded={adding()} onClick={() => setAdding((open) => !open)}>
            {adding() ? "Close" : "＋ Add a fact"}
          </Button>
        </PageHeader>
        <Show when={exportError()}><p class="mb-3 text-sm text-rust">{exportError()}</p></Show>
        <Show when={adding()}>
          <div class="mb-6"><AddFact project={project()} topic={topic()} onDone={() => setAdding(false)} /></div>
        </Show>

        <div class="flex gap-3">
        <form class="search-field flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-line px-3" role="search" onSubmit={(event) => { event.preventDefault(); setSubmittedQuery(query().trim()) }}>
          <span aria-hidden="true" class="text-cream-dim">⌕</span>
          <input
            class="h-11 min-w-0 flex-1 bg-transparent text-sm text-cream outline-none placeholder:text-cream-dim"
            aria-label="Search facts"
            placeholder="Search lessons, runbooks, and decisions…"
            value={query()}
            onInput={(event) => setQuery(event.currentTarget.value)}
          />
          <Show when={submittedQuery()}><Button type="button" size="sm" variant="ghost" onClick={() => { setQuery(""); setSubmittedQuery("") }}>Clear</Button></Show>
          <Button type="submit" size="sm">Search</Button>
        </form>
        <select class={`${selectClass} h-auto rounded-xl px-3`} aria-label="Status" value={status()} onChange={(event) => setStatus(event.currentTarget.value as FactStatus | "")}>
          <For each={FACT_STATUSES}>{(item) => <option value={item}>{item}</option>}</For>
          <option value="">any status</option>
        </select>
        </div>

        <Show when={submittedQuery()}>
          <section class="mt-4" aria-label="Fact search results">
            <h2 class="mb-2 text-sm font-semibold text-cream">Results for “{submittedQuery()}”</h2>
            <Show when={found.isError}><p class="text-sm text-rust">{getErrorMessage(found.error, "Search failed.")}</p></Show>
            <Show when={found.isSuccess && found.data?.results.length === 0}><p class="text-sm text-cream-dim">No matching facts.</p></Show>
            <ul class="flex flex-col gap-2">
              <For each={found.data?.results ?? []}>
                {(hit) => (
                  <li class="card rounded-xl border border-line p-3 text-sm">
                    <button type="button" class="chip text-[11px] text-brass hover:underline" onClick={() => { setParams({ project: hit.project ?? undefined, topic: hit.topic ?? undefined }); setSubmittedQuery(""); setQuery("") }}>
                      {hit.project} / {hit.topic}
                    </button>
                    <p class="mt-1.5 text-cream"><Snippet text={hit.snippet} /></p>
                    <p class="mt-1 text-xs text-cream-dim">{hit.author} · {formatStamp(hit.created_at)}</p>
                  </li>
                )}
              </For>
            </ul>
          </section>
        </Show>

        <div class="mt-7 grid gap-8 lg:grid-cols-[13rem_minmax(0,1fr)]">
          <nav aria-label="Projects" class="text-sm">
            <h2 class="side-label mb-2 px-1">Projects</h2>
            <ul class="flex gap-1 overflow-x-auto lg:flex-col lg:overflow-visible">
              <For each={projects.data ?? []}>
                {(name) => (
                  <li>
                    <button
                      type="button"
                      class="project-row flex w-full items-center gap-2 whitespace-nowrap rounded-lg px-2.5 py-1.5 text-left"
                      classList={{ "is-active font-medium text-cream": project() === name, "text-cream-dim hover:bg-ink-raised hover:text-cream": project() !== name }}
                      aria-current={project() === name ? "page" : undefined}
                      onClick={() => pickProject(name)}
                    >
                      <span aria-hidden="true">📁</span>{name}
                    </button>
                  </li>
                )}
              </For>
              <Show when={projects.isSuccess && (projects.data?.length ?? 0) === 0}><li class="px-1 text-cream-dim">No facts yet.</li></Show>
            </ul>
            <form class="mt-3 flex gap-1" onSubmit={(event) => { event.preventDefault(); if (PROJECT_RE.test(projectDraft().trim().toLowerCase())) pickProject(projectDraft().trim().toLowerCase()) }}>
              <Input class="h-8 text-xs" aria-label="Open project" placeholder="Open project…" value={projectDraft()} onInput={(event) => setProjectDraft(event.currentTarget.value)} />
              <Button type="submit" size="sm" variant="outline">Go</Button>
            </form>
            <Show when={project()}>
              <h2 class="side-label mb-2 mt-7 px-1">Topics</h2>
              <ul class="flex flex-wrap gap-1 lg:flex-col" role="group" aria-label="Topics">
                <li>
                  <button type="button" class="project-row flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left" classList={{ "is-active font-medium text-cream": !topic(), "text-cream-dim hover:bg-ink-raised hover:text-cream": !!topic() }} aria-pressed={!topic()} onClick={() => setParams({ topic: undefined })}>
                    All topics <span class="text-xs opacity-70">{topicTotal() || ""}</span>
                  </button>
                </li>
                <For each={topics.data?.topics ?? []}>
                  {(item) => (
                    <li>
                      <button type="button" class="project-row flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left" classList={{ "is-active font-medium text-cream": topic() === item.topic, "text-cream-dim hover:bg-ink-raised hover:text-cream": topic() !== item.topic }} aria-pressed={topic() === item.topic} onClick={() => setParams({ topic: item.topic })}>
                        {item.topic} <span class="text-xs opacity-70">{item.count}</span>
                      </button>
                    </li>
                  )}
                </For>
              </ul>
            </Show>
          </nav>

          <section aria-label="Facts" class="min-w-0">
            <Show when={project()} fallback={<p class="rounded-xl border border-dashed border-line px-4 py-10 text-center text-sm text-cream-dim">Pick a project to see its facts.</p>}>
              <div class="mb-3 flex items-center justify-between text-[13px] text-cream-dim">
                <h2><span class="font-semibold text-cream">{rows().length}{facts.hasNextPage ? "+" : ""} facts</span> · {project()}{topic() ? ` / ${topic()}` : ""}</h2>
                <span class="flex items-center gap-3">Newest first · {status() || "any status"}
                  <button type="button" class="text-brass hover:underline" onClick={() => void download(project())}>Export {project()} ↓</button>
                </span>
              </div>
              <Show when={facts.isError}><p class="text-sm text-rust">{getErrorMessage(facts.error, "Could not load facts.")}</p></Show>
              <Show when={facts.isSuccess && rows().length === 0}>
                <p class="rounded-xl border border-dashed border-line px-4 py-10 text-center text-sm text-cream-dim">No {status() || ""} facts here yet.</p>
              </Show>
              <ul class="flex flex-col gap-3">
                <For each={rows()}>{(fact) => <FactRow fact={fact} showTopic={!topic()} />}</For>
              </ul>
              <Show when={facts.hasNextPage}>
                <Button type="button" class="mt-3" variant="outline" size="sm" disabled={facts.isFetchingNextPage} onClick={() => void facts.fetchNextPage()}>
                  Load more
                </Button>
              </Show>
            </Show>
          </section>
        </div>
      </div>
    </div>
  )
}

const STATUS_ACTION: Record<FactStatus, string> = { active: "Reactivate", superseded: "Supersede", rejected: "Reject" }

/** First sentence as a headline, the rest as body. A one-sentence fact is all headline. */
export function splitFact(text: string): { title: string; body: string } {
  const match = /^(.{12,160}?[.!?])\s+(\S[\s\S]*)$/.exec(text.trim())
  return match ? { title: match[1], body: match[2] } : { title: text.trim(), body: "" }
}

function FactRow(props: { fact: KnowledgeFact; showTopic: boolean }) {
  const prefs = usePrefs()
  const mutations = useKnowledgeMutations()
  const [error, setError] = createSignal("")
  const actor = () => (isHandle(prefs.displayName().trim()) ? prefs.displayName().trim() : "")
  const parts = () => splitFact(props.fact.fact)
  const source = () => [props.fact.source_run, props.fact.source_step].filter(Boolean).join(" · ")
  return (
    <li class="card fact-card rounded-2xl border border-line px-5 py-4" data-status={props.fact.status}>
      <div class="flex items-start justify-between gap-3">
        <span class="text-xs text-cream-dim">{props.fact.topic}</span>
        <span class="fact-status shrink-0 rounded-md px-2 py-0.5 text-[11px] font-semibold capitalize" data-status={props.fact.status}>{props.fact.status}</span>
      </div>
      <h3 class="mt-1.5 text-[15.5px] font-semibold leading-snug text-cream" classList={{ "line-through decoration-cream-dim/60": props.fact.status !== "active" }}>
        <MessageBody text={parts().title} mentions={[]} />
      </h3>
      <Show when={parts().body}>
        <div class="mt-1.5 text-[14px] leading-relaxed text-cream-dim"><MessageBody text={parts().body} mentions={[]} /></div>
      </Show>
      <p class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-cream-dim">
        <Show when={source()}><span class="font-mono" title="Source run · step">▤ {source()}</span></Show>
        <span>by <span class="text-cream">{props.fact.author}</span></span>
        <span title={props.fact.created_at}>{formatStamp(props.fact.created_at)}</span>
      </p>
      <details class="provenance mt-3 text-xs">
        <summary class="cursor-pointer select-none text-cream-dim hover:text-cream">Provenance &amp; actions</summary>
        <dl class="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-cream-dim">
          <dt>Project</dt><dd class="text-cream">{props.fact.project}</dd>
          <dt>Run</dt><dd class="font-mono text-cream">{props.fact.source_run ?? "—"}</dd>
          <dt>Step</dt><dd class="font-mono text-cream">{props.fact.source_step ?? "—"}</dd>
          <dt>Updated</dt><dd class="text-cream">{relativeTime(props.fact.updated_at) || formatStamp(props.fact.updated_at)}</dd>
        </dl>
        <Show when={actor()} fallback={<p class="mt-2 text-cream-dim">Set a display name to change a fact's status.</p>}>
          <div class="mt-3 flex gap-1.5">
            <For each={FACT_STATUSES.filter((item) => item !== props.fact.status)}>
              {(next) => (
                <Button
                  type="button"
                  size="sm"
                  variant={next === "rejected" ? "danger" : "outline"}
                  class="h-7"
                  disabled={mutations.status.isPending}
                  onClick={() => { setError(""); mutations.status.mutate({ id: props.fact.id, status: next, actor: actor() }, { onError: (reason) => setError(getErrorMessage(reason)) }) }}
                >
                  {STATUS_ACTION[next]}
                </Button>
              )}
            </For>
          </div>
        </Show>
      </details>
      <Show when={error()}><p class="mt-1 text-xs text-rust">{error()}</p></Show>
    </li>
  )
}

function AddFact(props: { project: string; topic: string; onDone?: () => void }) {
  const prefs = usePrefs()
  const mutations = useKnowledgeMutations()
  const [project, setProject] = createSignal("")
  const [topic, setTopic] = createSignal("")
  const [fact, setFact] = createSignal("")
  const [run, setRun] = createSignal("")
  const [step, setStep] = createSignal("")
  const [error, setError] = createSignal("")
  const [note, setNote] = createSignal("")
  const author = () => (isHandle(prefs.displayName().trim()) ? prefs.displayName().trim() : "")
  const projectValue = () => (project().trim() || props.project).toLowerCase()

  return (
    <form
      class="card grid gap-2 rounded-xl border border-line p-4 sm:grid-cols-2"
      aria-label="Add a fact"
      onSubmit={(event) => {
        event.preventDefault()
        setError("")
        setNote("")
        if (!author()) { setError("Set a valid display name first."); return }
        if (!PROJECT_RE.test(projectValue())) { setError("Enter a project slug."); return }
        if (!fact().trim()) { setError("Write the fact."); return }
        mutations.add.mutate(
          {
            project: projectValue(),
            topic: (topic().trim() || props.topic).toLowerCase() || undefined,
            fact: fact().trim(),
            author: author(),
            source_run: run().trim() || undefined,
            source_step: step().trim() || undefined,
          },
          {
            onSuccess: (result) => {
              setFact("")
              setNote(result.created ? "Fact added." : "Already known: the existing fact was kept.")
            },
            onError: (reason) => setError(getErrorMessage(reason, "Not added. Your draft is still here.")),
          },
        )
      }}
    >
      <h2 class="text-sm font-semibold text-cream sm:col-span-2">Add a fact <span class="font-normal text-cream-dim">· one durable, reusable statement. Duplicates are kept once.</span></h2>
      <Input aria-label="Project" placeholder={props.project || "project"} value={project()} onInput={(event) => setProject(event.currentTarget.value)} />
      <Input aria-label="Topic" placeholder={props.topic || "topic (general)"} value={topic()} onInput={(event) => setTopic(event.currentTarget.value)} />
      <Textarea class="sm:col-span-2" aria-label="Fact" placeholder="One durable fact" maxLength={2000} value={fact()} onInput={(event) => setFact(event.currentTarget.value)} />
      <div class="flex gap-2 sm:col-span-1">
        <Input aria-label="Source run" placeholder="Run (optional)" value={run()} onInput={(event) => setRun(event.currentTarget.value)} />
        <Input aria-label="Source step" placeholder="Step (optional)" value={step()} onInput={(event) => setStep(event.currentTarget.value)} />
      </div>
      <div class="flex items-center justify-end gap-2">
        <Show when={props.onDone}><Button type="button" variant="ghost" onClick={() => props.onDone?.()}>Cancel</Button></Show>
        <Button type="submit" disabled={mutations.add.isPending}>Add fact</Button>
      </div>
      <Show when={error()}><p class="text-sm text-rust sm:col-span-2">{error()}</p></Show>
      <Show when={note()}><p class="text-xs text-cream-dim sm:col-span-2" role="status">{note()}</p></Show>
    </form>
  )
}

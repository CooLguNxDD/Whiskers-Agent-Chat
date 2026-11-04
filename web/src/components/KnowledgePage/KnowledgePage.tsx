import { createEffect, createSignal, For, Show } from "solid-js"
import { useSearchParams } from "@solidjs/router"
import { exportFacts } from "@/api/knowledge"
import { FACT_STATUSES, type FactStatus, type KnowledgeFact } from "@/api/types"
import { Snippet } from "@/components/RichText"
import { Avatar } from "@/components/ui/avatar"
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

  const download = async () => {
    setExportError("")
    try {
      const text = await exportFacts({ project: project() || undefined, status: status() || "all" })
      const url = URL.createObjectURL(new Blob([text], { type: "application/x-ndjson" }))
      const link = document.createElement("a")
      link.href = url
      link.download = `knowledge-${project() || "all"}.jsonl`
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
        <PageHeader title="Knowledge" description="Durable facts the fleet has learned, by project and topic. Agents read these before they start work.">
          <Button type="button" variant="outline" onClick={() => void download()}>Export JSONL</Button>
          <Button type="button" variant={adding() ? "outline" : "default"} aria-expanded={adding()} onClick={() => setAdding((open) => !open)}>
            {adding() ? "Close" : "＋ Add fact"}
          </Button>
        </PageHeader>
        <Show when={exportError()}><p class="mb-3 text-sm text-rust">{exportError()}</p></Show>
        <Show when={adding()}>
          <div class="mb-6"><AddFact project={project()} topic={topic()} onDone={() => setAdding(false)} /></div>
        </Show>

        <form class="search-field flex items-center gap-2 rounded-xl border border-line px-3" role="search" onSubmit={(event) => { event.preventDefault(); setSubmittedQuery(query().trim()) }}>
          <span aria-hidden="true" class="text-cream-dim">⌕</span>
          <input
            class="h-11 min-w-0 flex-1 bg-transparent text-sm text-cream outline-none placeholder:text-cream-dim"
            aria-label="Search facts"
            placeholder="Search active facts across every project"
            value={query()}
            onInput={(event) => setQuery(event.currentTarget.value)}
          />
          <Show when={submittedQuery()}><Button type="button" size="sm" variant="ghost" onClick={() => { setQuery(""); setSubmittedQuery("") }}>Clear</Button></Show>
          <Button type="submit" size="sm">Search</Button>
        </form>

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

        <div class="mt-6 grid gap-6 lg:grid-cols-[14rem_minmax(0,1fr)]">
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
          </nav>

          <section aria-label="Facts" class="min-w-0">
            <Show when={project()} fallback={<p class="rounded-xl border border-dashed border-line px-4 py-10 text-center text-sm text-cream-dim">Pick a project to see its facts.</p>}>
              <div class="mb-3 flex flex-wrap items-center gap-3">
                <h2 class="font-display text-xl font-semibold text-cream">{project()}</h2>
                <Show when={topicTotal()}><span class="count-pill">{topicTotal()} facts</span></Show>
                <div class="segmented ml-auto flex rounded-lg border border-line p-0.5 text-xs" role="group" aria-label="Status">
                  <For each={[...FACT_STATUSES, ""] as const}>
                    {(item) => (
                      <button type="button" class="rounded-md px-2.5 py-1 capitalize" aria-pressed={status() === item} onClick={() => setStatus(item as FactStatus | "")}>
                        {item || "any"}
                      </button>
                    )}
                  </For>
                </div>
              </div>
              <div class="mb-4 flex flex-wrap gap-1.5" role="group" aria-label="Topics">
                <button type="button" class="filter-chip" aria-pressed={!topic()} onClick={() => setParams({ topic: undefined })}>All topics</button>
                <For each={topics.data?.topics ?? []}>
                  {(item) => (
                    <button type="button" class="filter-chip" aria-pressed={topic() === item.topic} onClick={() => setParams({ topic: item.topic })}>
                      {item.topic} <span class="ml-1 opacity-70">{item.count}</span>
                    </button>
                  )}
                </For>
              </div>
              <Show when={facts.isError}><p class="text-sm text-rust">{getErrorMessage(facts.error, "Could not load facts.")}</p></Show>
              <Show when={facts.isSuccess && rows().length === 0}>
                <p class="rounded-xl border border-dashed border-line px-4 py-10 text-center text-sm text-cream-dim">No {status() || ""} facts here yet.</p>
              </Show>
              <ul class="flex flex-col gap-2">
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

function FactRow(props: { fact: KnowledgeFact; showTopic: boolean }) {
  const prefs = usePrefs()
  const mutations = useKnowledgeMutations()
  const [error, setError] = createSignal("")
  const actor = () => (isHandle(prefs.displayName().trim()) ? prefs.displayName().trim() : "")
  const source = () => [props.fact.source_run, props.fact.source_step].filter(Boolean).join(" · ")
  return (
    <li class="card fact-card group rounded-xl border border-line px-4 py-3" data-status={props.fact.status}>
      <div class="flex items-start gap-3">
        <div class="min-w-0 flex-1 break-words text-[14.5px] leading-relaxed text-cream" classList={{ "line-through decoration-cream-dim/60": props.fact.status !== "active" }}><MessageBody text={props.fact.fact} mentions={[]} /></div>
        <span class="fact-status shrink-0 rounded-md px-1.5 py-0.5 text-[11px] font-semibold capitalize" data-status={props.fact.status}>{props.fact.status}</span>
      </div>
      <div class="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-cream-dim">
        <span class="flex items-center gap-1.5"><Avatar name={props.fact.author} size="sm" /><span class="font-medium text-cream">{props.fact.author}</span></span>
        <Show when={props.showTopic}><span class="chip text-[11px]">{props.fact.topic}</span></Show>
        <span title={props.fact.created_at}>{relativeTime(props.fact.created_at) || formatStamp(props.fact.created_at)}</span>
        <Show when={source()}><span class="font-mono" title="Source run · step">{source()}</span></Show>
        <Show when={actor()}>
          <div class="ml-auto flex gap-1 opacity-70 transition group-hover:opacity-100 focus-within:opacity-100">
            <For each={FACT_STATUSES.filter((item) => item !== props.fact.status)}>
              {(next) => (
                <Button
                  type="button"
                  size="sm"
                  variant={next === "rejected" ? "danger" : "ghost"}
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
      </div>
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

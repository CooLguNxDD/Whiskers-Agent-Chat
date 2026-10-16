import { createSignal, For, Show } from "solid-js"
import { useSearchParams } from "@solidjs/router"
import { exportFacts } from "@/api/knowledge"
import { FACT_STATUSES, type FactStatus, type KnowledgeFact } from "@/api/types"
import { selectClass, Snippet } from "@/components/RichText"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { useFacts, useFactSearch, useKnowledgeMutations, useKnowledgeProjects, useKnowledgeTopics } from "@/hooks/useKnowledge"
import { usePrefs } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { isHandle } from "@/utils/handles"
import { formatStamp } from "@/utils/time"

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

  return (
    <div class="min-h-0 flex-1 overflow-y-auto p-4">
      <div class="mx-auto max-w-6xl">
        <h1 class="font-display text-xl text-cream">Knowledge</h1>
        <form class="mt-3 flex gap-2" role="search" onSubmit={(event) => { event.preventDefault(); setSubmittedQuery(query().trim()) }}>
          <Input aria-label="Search facts" placeholder="Search active facts" value={query()} onInput={(event) => setQuery(event.currentTarget.value)} />
          <Button type="submit">Search</Button>
          <Show when={submittedQuery()}><Button type="button" variant="ghost" onClick={() => { setQuery(""); setSubmittedQuery("") }}>Clear</Button></Show>
        </form>

        <Show when={submittedQuery()}>
          <section class="mt-4" aria-label="Fact search results">
            <Show when={found.isError}><p class="text-sm text-rust">{getErrorMessage(found.error, "Search failed.")}</p></Show>
            <Show when={found.isSuccess && found.data?.results.length === 0}><p class="text-sm text-cream-dim">No matching facts.</p></Show>
            <ul class="flex flex-col gap-2">
              <For each={found.data?.results ?? []}>
                {(hit) => (
                  <li class="rounded-lg border border-line p-3 text-sm">
                    <button type="button" class="text-xs text-brass hover:underline" onClick={() => setParams({ project: hit.project ?? undefined, topic: hit.topic ?? undefined })}>
                      {hit.project} / {hit.topic}
                    </button>
                    <p class="mt-1 text-cream"><Snippet text={hit.snippet} /></p>
                    <p class="text-xs text-cream-dim">{hit.author} · {formatStamp(hit.created_at)}</p>
                  </li>
                )}
              </For>
            </ul>
          </section>
        </Show>

        <div class="mt-5 grid gap-6 lg:grid-cols-[12rem_1fr_18rem]">
          <nav aria-label="Projects and topics" class="text-sm">
            <h2 class="mb-2 text-xs uppercase tracking-wide text-cream-dim">Projects</h2>
            <ul class="flex flex-col gap-1">
              <For each={projects.data ?? []}>
                {(name) => (
                  <li>
                    <button type="button" class="hover:text-cream" classList={{ "text-brass": project() === name, "text-cream-dim": project() !== name }} onClick={() => pickProject(name)}>
                      {name}
                    </button>
                  </li>
                )}
              </For>
              <Show when={projects.isSuccess && (projects.data?.length ?? 0) === 0}><li class="text-cream-dim">No facts yet.</li></Show>
            </ul>
            <form class="mt-2 flex gap-1" onSubmit={(event) => { event.preventDefault(); if (PROJECT_RE.test(projectDraft().trim().toLowerCase())) pickProject(projectDraft().trim().toLowerCase()) }}>
              <Input class="h-8" aria-label="Open project" placeholder="project" value={projectDraft()} onInput={(event) => setProjectDraft(event.currentTarget.value)} />
              <Button type="submit" size="sm" variant="outline">Open</Button>
            </form>
            <Show when={project()}>
              <h2 class="mb-2 mt-4 text-xs uppercase tracking-wide text-cream-dim">Topics</h2>
              <ul class="flex flex-col gap-1">
                <li>
                  <button type="button" class="hover:text-cream" classList={{ "text-brass": !topic(), "text-cream-dim": !!topic() }} onClick={() => setParams({ topic: undefined })}>
                    All topics
                  </button>
                </li>
                <For each={topics.data?.topics ?? []}>
                  {(item) => (
                    <li>
                      <button type="button" class="hover:text-cream" classList={{ "text-brass": topic() === item.topic, "text-cream-dim": topic() !== item.topic }} onClick={() => setParams({ topic: item.topic })}>
                        {item.topic} <span class="text-xs">({item.count})</span>
                      </button>
                    </li>
                  )}
                </For>
              </ul>
            </Show>
          </nav>

          <section aria-label="Facts" class="min-w-0">
            <div class="mb-2 flex flex-wrap items-center gap-2">
              <h2 class="font-display text-lg text-cream">{project() ? `${project()}${topic() ? ` / ${topic()}` : ""}` : "Pick a project"}</h2>
              <select class={`${selectClass} ml-auto h-8`} aria-label="Status" value={status()} onChange={(event) => setStatus(event.currentTarget.value as FactStatus | "")}>
                <option value="">Any status</option>
                <For each={FACT_STATUSES}>{(item) => <option value={item}>{item}</option>}</For>
              </select>
              <Button type="button" size="sm" variant="outline" onClick={() => void download()}>Export JSONL</Button>
            </div>
            <Show when={exportError()}><p class="text-sm text-rust">{exportError()}</p></Show>
            <Show when={facts.isError}><p class="text-sm text-rust">{getErrorMessage(facts.error, "Could not load facts.")}</p></Show>
            <Show when={facts.isSuccess && rows().length === 0}><p class="text-sm text-cream-dim">No facts here.</p></Show>
            <ul class="flex flex-col gap-2">
              <For each={rows()}>{(fact) => <FactRow fact={fact} />}</For>
            </ul>
            <Show when={facts.hasNextPage}>
              <Button type="button" class="mt-3" variant="outline" size="sm" disabled={facts.isFetchingNextPage} onClick={() => void facts.fetchNextPage()}>
                Load more
              </Button>
            </Show>
          </section>

          <AddFact project={project()} topic={topic()} />
        </div>
      </div>
    </div>
  )
}

function FactRow(props: { fact: KnowledgeFact }) {
  const prefs = usePrefs()
  const mutations = useKnowledgeMutations()
  const [error, setError] = createSignal("")
  const actor = () => (isHandle(prefs.displayName().trim()) ? prefs.displayName().trim() : "")
  const source = () => [props.fact.source_run, props.fact.source_step].filter(Boolean).join(" · ")
  return (
    <li class="rounded-lg border border-line p-3">
      <p class="whitespace-pre-wrap break-words text-sm text-cream">{props.fact.fact}</p>
      <div class="mt-2 flex flex-wrap items-center gap-2 text-xs text-cream-dim">
        <Badge classList={{ "text-brass": props.fact.status === "active" }}>{props.fact.status}</Badge>
        <span>{props.fact.topic}</span>
        <span>{props.fact.author}</span>
        <Show when={source()}><span class="font-mono">{source()}</span></Show>
        <span title={props.fact.created_at}>{formatStamp(props.fact.created_at)}</span>
        <Show when={actor()}>
          <div class="ml-auto flex gap-1">
            <For each={FACT_STATUSES.filter((item) => item !== props.fact.status)}>
              {(next) => (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  disabled={mutations.status.isPending}
                  onClick={() => { setError(""); mutations.status.mutate({ id: props.fact.id, status: next, actor: actor() }, { onError: (reason) => setError(getErrorMessage(reason)) }) }}
                >
                  {next === "active" ? "Reactivate" : next === "superseded" ? "Supersede" : "Reject"}
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

function AddFact(props: { project: string; topic: string }) {
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
      class="flex flex-col gap-2 rounded-lg border border-line p-3"
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
      <h2 class="font-display text-lg text-cream">Add a fact</h2>
      <Input aria-label="Project" placeholder={props.project || "project"} value={project()} onInput={(event) => setProject(event.currentTarget.value)} />
      <Input aria-label="Topic" placeholder={props.topic || "topic (general)"} value={topic()} onInput={(event) => setTopic(event.currentTarget.value)} />
      <Textarea aria-label="Fact" placeholder="One durable fact" maxLength={2000} value={fact()} onInput={(event) => setFact(event.currentTarget.value)} />
      <div class="flex gap-2">
        <Input aria-label="Source run" placeholder="Run (optional)" value={run()} onInput={(event) => setRun(event.currentTarget.value)} />
        <Input aria-label="Source step" placeholder="Step (optional)" value={step()} onInput={(event) => setStep(event.currentTarget.value)} />
      </div>
      <div class="flex justify-end"><Button type="submit" disabled={mutations.add.isPending}>Add</Button></div>
      <Show when={error()}><p class="text-sm text-rust">{error()}</p></Show>
      <Show when={note()}><p class="text-xs text-cream-dim" role="status">{note()}</p></Show>
    </form>
  )
}

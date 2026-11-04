import { createEffect, createSignal, For, Show } from "solid-js"
import { type Proposal, type ProposalChanges, type ProposalStatus, type VoteKind } from "@/api/types"
import { Avatar } from "@/components/ui/avatar"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { myVotes, useEvolveState, usePostReactions, useProposalMutations, useProposals, useVote } from "@/hooks/useProposals"
import { usePrefs } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { isHandle } from "@/utils/handles"
import { formatStamp } from "@/utils/time"
import { relativeTime } from "@/utils/fleetStatus"
import { PageHeader, TabStrip } from "@/components/ui/page"
import { Input } from "@/components/ui/input"

/** Moves the board offers, mirroring the hub's transition table. The hub still decides. */
const DECISIONS: Record<ProposalStatus, readonly ("accepted" | "rejected" | "withdrawn")[]> = {
  open: ["accepted", "rejected", "withdrawn"],
  accepted: ["rejected", "withdrawn"],
  building: ["withdrawn"],
  rejected: [],
  built: [],
  withdrawn: [],
}

/** Only http(s) evidence becomes a link; anything else stays plain text. */
export function evidenceHref(item: string): string | null {
  try {
    const url = new URL(item)
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null
  } catch {
    return null
  }
}

/** Statuses in workflow order: the live path first, then the dead ends. */
const LANE_ORDER: readonly ProposalStatus[] = ["open", "accepted", "building", "built", "rejected", "withdrawn"]
const LABEL: Record<ProposalStatus, string> = {
  open: "Open", accepted: "Accepted", building: "Building", built: "Built", rejected: "Rejected", withdrawn: "Withdrawn",
}
const BADGE: Record<ProposalStatus, { text: string; tone?: string }> = {
  open: { text: "Needs review", tone: "warn" },
  accepted: { text: "Accepted", tone: "ok" },
  building: { text: "Building", tone: "accent" },
  built: { text: "Built", tone: "ok" },
  rejected: { text: "Rejected", tone: "bad" },
  withdrawn: { text: "Withdrawn" },
}

type Tab = ProposalStatus | "all"

/** Proposals: stats, status tabs, project filter, a list of decision cards, and the evolve state. */
export default function ProposalsPage() {
  const [project, setProject] = createSignal("")
  const [tab, setTab] = createSignal<Tab>("open")
  const [composing, setComposing] = createSignal(false)
  const proposals = useProposals(project)
  const evolve = useEvolveState()
  // Projects seen on the unfiltered board, so the filter row doesn't collapse to one chip after picking.
  const [knownProjects, setKnownProjects] = createSignal<string[]>([])
  createEffect(() => {
    const seen = new Set(knownProjects())
    for (const item of proposals.data?.proposals ?? []) seen.add(item.project)
    if (seen.size !== knownProjects().length) setKnownProjects([...seen].sort())
  })
  const all = () => proposals.data?.proposals ?? []
  const byStatus = (status: ProposalStatus) => all().filter((item) => item.status === status)

  return (
    <div class="page min-h-0 flex-1 overflow-y-auto">
      <div class="mx-auto max-w-6xl">
      <PageHeader
        eyebrow="Human judgment, at the right moment"
        title="Better ideas. Clear decisions."
        description="Review the why, weigh the change, and give the fleet a direction."
        stats={[
          { value: byStatus("open").length, label: "awaiting review", tone: "accent" },
          { value: byStatus("accepted").length, label: "accepted" },
          { value: byStatus("building").length, label: "building" },
          { value: byStatus("built").length, label: "built" },
        ]}
      >
        <Show when={evolve.data}>
          <span
            class="evolve-pill inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium"
            data-paused={evolve.data?.paused ? "true" : "false"}
            title={evolve.data?.paused ? `Paused by ${evolve.data?.by ?? "unknown"}${evolve.data?.at ? ` at ${formatStamp(evolve.data.at)}` : ""}` : "Agents may pick up accepted proposals"}
          >
            <span class="size-2 rounded-full bg-current" aria-hidden="true" />
            {evolve.data?.paused ? "Evolve paused" : "Evolve running"}
          </span>
        </Show>
        <Button type="button" class="h-10 px-4" variant={composing() ? "outline" : "default"} aria-expanded={composing()} onClick={() => setComposing((open) => !open)}>
          {composing() ? "Close" : "＋ New proposal"}
        </Button>
      </PageHeader>
      <Show when={evolve.data?.paused}>
        <div role="alert" class="mb-5 rounded-xl border border-rust/50 bg-rust/10 px-4 py-2.5 text-sm text-rust">
          Evolve paused by {evolve.data?.by ?? "unknown"} at {evolve.data?.at ? formatStamp(evolve.data.at) : "unknown time"}. Say <code class="font-mono">resume evolve</code> in chat to restart it.
        </div>
      </Show>
      <Show when={composing()}><NewProposalForm defaultProject={project()} onDone={() => { setComposing(false); setTab("open") }} /></Show>
      <TabStrip
        label="Proposal status"
        value={tab()}
        onChange={setTab}
        tabs={[...LANE_ORDER.map((status) => ({ value: status as Tab, label: LABEL[status], count: byStatus(status).length })), { value: "all" as Tab, label: "All", count: all().length }]}
      />
      <Show when={knownProjects().length > 1 || project()}>
        <div class="mt-4 flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by project">
          <span class="mr-1 text-xs text-cream-dim">Project</span>
          <button type="button" class="filter-chip" aria-pressed={!project()} onClick={() => setProject("")}>All</button>
          <For each={knownProjects()}>
            {(name) => <button type="button" class="filter-chip" aria-pressed={project() === name} onClick={() => setProject(name)}>{name}</button>}
          </For>
        </div>
      </Show>
      <Show when={proposals.isError}><p class="mt-4 text-sm text-rust">{getErrorMessage(proposals.error, "Could not load proposals.")}</p></Show>
      <Show when={proposals.data?.has_more}><p class="mt-4 text-xs text-cream-dim">Showing the newest {proposals.data?.proposals.length} proposals.</p></Show>
      <div class="mt-5 flex flex-col gap-6">
        <For each={tab() === "all" ? LANE_ORDER : [tab() as ProposalStatus]}>
          {(status) => {
            const cards = () => byStatus(status)
            const muted = () => status === "withdrawn"
            return (
              <section aria-label={`${status} proposals`} data-status={status} class="flex min-w-0 flex-col gap-3" classList={{ "opacity-60": muted() }}>
                <Show when={tab() === "all"}>
                  <h2 class="flex items-center gap-2 text-sm font-semibold text-cream">{LABEL[status]} <span class="font-normal text-cream-dim tabular-nums">{cards().length}</span></h2>
                </Show>
                <Show when={proposals.isSuccess && cards().length === 0 && tab() !== "all"}>
                  <p class="rounded-2xl border border-dashed border-line px-4 py-10 text-center text-sm text-cream-dim">No {LABEL[status].toLowerCase()} proposals{project() ? ` in ${project()}` : ""}.</p>
                </Show>
                <Show when={cards().length > 0}>
                  <ul class="flex flex-col gap-3">
                    <For each={cards()}>{(proposal) => <ProposalCard proposal={proposal} />}</For>
                  </ul>
                </Show>
              </section>
            )
          }}
        </For>
      </div>
      <p class="mt-5 text-xs text-cream-dim">Votes help prioritize. Accept or reject a proposal to give the fleet a direction; agents also propose through <code class="font-mono">fleet_create_proposal</code>.</p>
      </div>
    </div>
  )
}

function NewProposalForm(props: { defaultProject: string; onDone: () => void }) {
  const prefs = usePrefs()
  const mutations = useProposalMutations()
  const [project, setProjectDraft] = createSignal(props.defaultProject)
  const [title, setTitle] = createSignal("")
  const [summary, setSummary] = createSignal("")
  const [weight, setWeight] = createSignal(3)
  const [evidence, setEvidence] = createSignal("")
  const [error, setError] = createSignal("")
  const [requestId] = createSignal(crypto.randomUUID())
  const author = () => (isHandle(prefs.displayName().trim()) ? prefs.displayName().trim() : "")
  return (
    <form
      class="card mb-6 grid gap-3 rounded-2xl border border-line p-5 sm:grid-cols-[1fr_8rem]"
      aria-label="New proposal"
      onSubmit={(event) => {
        event.preventDefault()
        setError("")
        if (!author()) { setError("Set a valid display name first."); return }
        if (!project().trim() || !title().trim()) { setError("A project and a title are required."); return }
        mutations.create.mutate(
          {
            author: author(),
            project: project().trim().toLowerCase(),
            title: title().trim(),
            summary: summary().trim(),
            weight: weight(),
            evidence: evidence().split("\n").map((line) => line.trim()).filter(Boolean),
            client_request_id: requestId(),
          },
          { onSuccess: () => props.onDone(), onError: (reason) => setError(getErrorMessage(reason, "The hub refused the proposal. Your draft is still here.")) },
        )
      }}
    >
      <h2 class="text-sm font-semibold text-cream sm:col-span-2">New proposal</h2>
      <Input aria-label="Project" placeholder="project" value={project()} onInput={(event) => setProjectDraft(event.currentTarget.value)} />
      <label class="flex items-center gap-2 text-xs text-cream-dim">Weight
        <Input type="number" min="1" max="10" aria-label="Weight" value={String(weight())} onInput={(event) => setWeight(Number(event.currentTarget.value) || 1)} />
      </label>
      <Input class="sm:col-span-2" aria-label="Proposal title" placeholder="What should the fleet do?" maxLength={200} value={title()} onInput={(event) => setTitle(event.currentTarget.value)} />
      <Textarea class="sm:col-span-2" aria-label="Summary" placeholder="Why it matters, and what changes" maxLength={4000} value={summary()} onInput={(event) => setSummary(event.currentTarget.value)} />
      <Textarea class="min-h-14 sm:col-span-2" aria-label="Evidence" placeholder="Evidence: one link or reference per line (optional)" value={evidence()} onInput={(event) => setEvidence(event.currentTarget.value)} />
      <div class="flex items-center justify-end gap-2 sm:col-span-2">
        <Show when={error()}><span class="mr-auto text-xs text-rust">{error()}</span></Show>
        <Button type="button" variant="ghost" onClick={props.onDone}>Cancel</Button>
        <Button type="submit" disabled={mutations.create.isPending}>Propose</Button>
      </div>
    </form>
  )
}

/** Weight 1–10 as five pips (two points each). */
function WeightPips(props: { weight: number }) {
  const filled = () => Math.round(props.weight / 2)
  return (
    <span class="inline-flex items-center gap-0.5" title={`Weight ${props.weight} of 10: rough size of the work`} aria-hidden="true">
      <For each={[1, 2, 3, 4, 5]}>{(pip) => <span class="weight-pip size-1.5 rounded-full" classList={{ "is-on": pip <= filled() }} />}</For>
    </span>
  )
}

function ProposalCard(props: { proposal: Proposal }) {
  const prefs = usePrefs()
  const handle = () => (isHandle(prefs.displayName().trim()) ? prefs.displayName().trim() : "")
  const [reasoning, setReasoning] = createSignal(false)
  const refs = () => [
    props.proposal.pr_ref && { label: "PR", value: props.proposal.pr_ref },
    props.proposal.run_ref && { label: "run", value: props.proposal.run_ref },
    props.proposal.step_ref && { label: "step", value: props.proposal.step_ref },
  ].filter(Boolean) as { label: string; value: string }[]
  const hasReasoning = () => props.proposal.evidence.length > 0 || refs().length > 0
  return (
    <li class="card proposal-card flex gap-4 rounded-2xl border border-line p-5 text-sm" data-proposal={props.proposal.id}>
      <Votes postId={props.proposal.post_id} handle={handle()} />
      <div class="min-w-0 flex-1">
        <p class="eyebrow text-[10.5px]">{props.proposal.id} · {props.proposal.project} · <span title={`Weight ${props.proposal.weight} of 10: rough size of the work`}>w{props.proposal.weight}</span></p>
        <h3 class="mt-1.5 text-[16px] font-semibold leading-snug text-cream">{props.proposal.title}</h3>
        <Show when={props.proposal.summary}>
          <p class="mt-1.5 whitespace-pre-wrap break-words text-[14px] leading-relaxed text-cream-dim" classList={{ "line-clamp-3": !reasoning() }}>{props.proposal.summary}</p>
        </Show>
        <p class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-cream-dim">
          <span class="flex items-center gap-1.5">Proposed by <Avatar name={props.proposal.author} size="sm" /><span class="text-cream">{props.proposal.author}</span></span>
          <span title={props.proposal.created_at}>{relativeTime(props.proposal.created_at)}</span>
          <WeightPips weight={props.proposal.weight} />
        </p>
        <Show when={props.proposal.decided_by || props.proposal.reason}>
          <p class="decision-note mt-3 rounded-lg px-3 py-2 text-xs" data-decision>
            <Show when={props.proposal.decided_by}>{props.proposal.status} by {props.proposal.decided_by}</Show>
            <Show when={props.proposal.reason}>: {props.proposal.reason}</Show>
          </p>
        </Show>
        <div class="mt-3 flex flex-col gap-2" hidden={!reasoning()}>
          <Show when={refs().length}>
            <div class="flex flex-wrap gap-1">
              <For each={refs()}>{(ref) => <span class="chip font-mono text-[10px]" title={ref.value}>{ref.label} {ref.value}</span>}</For>
            </div>
          </Show>
          <Show when={props.proposal.evidence.length > 0}>
            <div class="text-xs">
              <p class="field-label mb-1">Evidence</p>
              <ul class="flex flex-col gap-0.5" aria-label="Evidence">
                <For each={props.proposal.evidence}>
                  {(item) => {
                    const href = evidenceHref(item)
                    return (
                      <li class="break-all">
                        {href ? <a href={href} target="_blank" rel="noopener noreferrer" class="text-brass hover:underline">{item}</a> : <span>{item}</span>}
                      </li>
                    )
                  }}
                </For>
              </ul>
            </div>
          </Show>
        </div>
      </div>
      <div class="flex w-40 shrink-0 flex-col items-stretch gap-2">
        <span class="soft-badge self-end" data-tone={BADGE[props.proposal.status].tone}>{BADGE[props.proposal.status].text}</span>
        <Decide proposal={props.proposal} handle={handle()} />
        <Show when={hasReasoning() || (props.proposal.summary?.length ?? 0) > 180}>
          <Button type="button" size="sm" variant="outline" class="h-8" aria-expanded={reasoning()} onClick={() => setReasoning((open) => !open)}>
            {reasoning() ? "Hide reasoning" : "View reasoning"}
          </Button>
        </Show>
      </div>
    </li>
  )
}

function Votes(props: { postId: number | null; handle: string }) {
  const reactions = usePostReactions(() => props.postId, () => props.handle)
  const vote = useVote(() => props.postId)
  const [error, setError] = createSignal("")
  const mine = () => myVotes(reactions.data)
  const count = (kind: VoteKind) => reactions.data?.reactions[kind] ?? 0
  // One vote in flight at a time: overlapping toggles would race their rollbacks.
  const disabled = () => !props.handle || props.postId === null || !reactions.data || vote.isPending
  const toggle = (kind: VoteKind) => {
    if (disabled()) return
    setError("")
    vote.mutate(
      { kind, handle: props.handle, on: !mine().includes(kind) },
      { onError: (reason) => setError(getErrorMessage(reason, "Vote failed.")) },
    )
  }
  return (
    <div class="vote-box flex w-12 shrink-0 flex-col items-center self-start rounded-xl border border-line py-1.5" title={props.handle ? undefined : "Set a display name to vote"}>
      <button
        type="button"
        class="vote-btn rounded-md px-2"
        aria-label="Vote +1"
        aria-pressed={mine().includes("+1")}
        disabled={disabled()}
        onClick={() => toggle("+1")}
      >▲<span class="sr-only" data-count="+1">{count("+1")}</span></button>
      <span class="py-0.5 text-[15px] font-semibold tabular-nums text-cream" data-score title={`+1: ${count("+1")} · -1: ${count("-1")}`}><span class="sr-only">score </span>{reactions.data?.score ?? 0}</span>
      <button
        type="button"
        class="vote-btn rounded-md px-2"
        aria-label="Vote -1"
        aria-pressed={mine().includes("-1")}
        disabled={disabled()}
        onClick={() => toggle("-1")}
      >▼<span class="sr-only" data-count="-1">{count("-1")}</span></button>
      <Show when={!props.handle}><p class="sr-only">Set a display name to vote.</p></Show>
      <Show when={error()}><p class="mt-1 w-40 text-xs text-rust">{error()}</p></Show>
    </div>
  )
}

function Decide(props: { proposal: Proposal; handle: string }) {
  const mutations = useProposalMutations()
  const [rejecting, setRejecting] = createSignal(false)
  const [reason, setReason] = createSignal("")
  const [error, setError] = createSignal("")
  const send = (changes: ProposalChanges) => {
    setError("")
    mutations.update.mutate(
      { id: props.proposal.id, actor: props.handle, changes },
      {
        onSuccess: () => { setRejecting(false); setReason("") },
        onError: (failure) => setError(getErrorMessage(failure, "The hub refused the change.")),
      },
    )
  }
  const moves = () => DECISIONS[props.proposal.status]
  return (
    <Show when={props.handle && moves().length > 0}>
      <div class="flex flex-col gap-1.5">
        <Show when={moves().includes("accepted")}>
          <Button type="button" size="sm" class="h-8" disabled={mutations.update.isPending} onClick={() => send({ status: "accepted" })}>Accept</Button>
        </Show>
        <Show when={moves().includes("rejected")}>
          <Button type="button" size="sm" variant="danger" class="h-8" aria-expanded={rejecting()} disabled={mutations.update.isPending} onClick={() => { setError(""); setRejecting(!rejecting()) }}>Reject</Button>
        </Show>
        <Show when={moves().includes("withdrawn")}>
          <Button type="button" size="sm" variant="ghost" class="h-7 text-cream-dim" disabled={mutations.update.isPending} onClick={() => send({ status: "withdrawn" })}>Withdraw</Button>
        </Show>
      </div>
      <Show when={rejecting()}>
        <form
          class="mt-2 flex flex-col gap-1"
          aria-label={`Reject ${props.proposal.id}`}
          onSubmit={(event) => {
            event.preventDefault()
            if (!reason().trim()) { setError("A reason is required to reject."); return }
            send({ status: "rejected", reason: reason().trim() })
          }}
        >
          <Textarea aria-label="Reject reason" placeholder="Why reject?" maxLength={2000} value={reason()} onInput={(event) => setReason(event.currentTarget.value)} />
          <div class="flex justify-end"><Button type="submit" size="sm" disabled={mutations.update.isPending}>Confirm reject</Button></div>
        </form>
      </Show>
      <Show when={error()}><p class="mt-1 text-xs text-rust" role="status">{error()}</p></Show>
    </Show>
  )
}

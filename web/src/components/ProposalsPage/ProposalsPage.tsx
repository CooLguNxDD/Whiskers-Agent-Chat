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
import { LaneTitle, PageHeader } from "@/components/ui/page"

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

/** Lanes in workflow order: the live path first, then the dead ends. */
const LANE_ORDER: readonly ProposalStatus[] = ["open", "accepted", "building", "built", "rejected", "withdrawn"]

const LANE_HINT: Record<ProposalStatus, string> = {
  open: "Waiting for votes and a decision",
  accepted: "Approved, not started",
  building: "Someone is on it",
  built: "Shipped",
  rejected: "Turned down, with a reason",
  withdrawn: "Pulled by the author",
}

/** Proposals board: stats, project filter, one lane per status, votes, decide actions and the evolve state. */
export default function ProposalsPage() {
  const [project, setProject] = createSignal("")
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
  const stats = () => [
    { label: "Open", value: byStatus("open").length, tone: "info" },
    { label: "Accepted", value: byStatus("accepted").length, tone: "ok" },
    { label: "Building", value: byStatus("building").length, tone: "warn" },
    { label: "Built", value: byStatus("built").length, tone: "ok" },
  ]

  return (
    <div class="page min-h-0 flex-1 overflow-y-auto">
      <PageHeader title="Proposals" description="Ideas for the fleet. Vote on them, then accept, reject or withdraw. Agents build what is accepted.">
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
      </PageHeader>
      <Show when={evolve.data?.paused}>
        <div role="alert" class="mb-4 rounded-xl border border-rust/50 bg-rust/10 px-4 py-2.5 text-sm text-rust">
          Evolve paused by {evolve.data?.by ?? "unknown"} at {evolve.data?.at ? formatStamp(evolve.data.at) : "unknown time"}. Say <code class="font-mono">resume evolve</code> in chat to restart it.
        </div>
      </Show>
      <div class="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <For each={stats()}>
          {(stat) => (
            <div class="card stat-tile rounded-xl border border-line px-4 py-3" data-tone={stat.tone}>
              <p class="text-xs font-medium text-cream-dim">{stat.label}</p>
              <p class="mt-1 text-2xl font-semibold tabular-nums text-cream">{stat.value}</p>
            </div>
          )}
        </For>
      </div>
      <Show when={knownProjects().length > 1 || project()}>
        <div class="mb-4 flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by project">
          <span class="mr-1 text-xs text-cream-dim">Project</span>
          <button type="button" class="filter-chip" aria-pressed={!project()} onClick={() => setProject("")}>All</button>
          <For each={knownProjects()}>
            {(name) => <button type="button" class="filter-chip" aria-pressed={project() === name} onClick={() => setProject(name)}>{name}</button>}
          </For>
        </div>
      </Show>
      <Show when={proposals.isError}><p class="mb-3 text-sm text-rust">{getErrorMessage(proposals.error, "Could not load proposals.")}</p></Show>
      <Show when={proposals.data?.has_more}><p class="mb-3 text-xs text-cream-dim">Showing the newest {proposals.data?.proposals.length} proposals.</p></Show>
      <div class="lanes grid gap-3">
        <For each={LANE_ORDER}>
          {(status) => {
            const cards = () => byStatus(status)
            const muted = () => status === "withdrawn"
            return (
              <section aria-label={`${status} proposals`} data-status={status} class="lane flex min-w-0 flex-col gap-2 rounded-2xl p-2" classList={{ "opacity-60": muted() }}>
                <LaneTitle label={status} count={cards().length} />
                <p class="-mt-1 px-1 text-[11px] text-cream-dim">{LANE_HINT[status]}</p>
                <Show when={proposals.isSuccess && cards().length === 0 && !muted()}>
                  <p class="px-1 py-3 text-center text-xs text-cream-dim">Nothing here</p>
                </Show>
                <Show when={!(muted() && cards().length === 0)}>
                  <ul class="flex flex-col gap-2">
                    <For each={cards()}>{(proposal) => <ProposalCard proposal={proposal} />}</For>
                  </ul>
                </Show>
              </section>
            )
          }}
        </For>
      </div>
    </div>
  )
}

/** Weight 1–10 as five pips (two points each), with the number kept as text. */
function WeightPips(props: { weight: number }) {
  const filled = () => Math.round(props.weight / 2)
  return (
    <span class="inline-flex items-center gap-1.5 text-[11px] text-cream-dim" title={`Weight ${props.weight} of 10: rough size of the work`}>
      <span class="flex gap-0.5" aria-hidden="true">
        <For each={[1, 2, 3, 4, 5]}>{(pip) => <span class="weight-pip size-1.5 rounded-full" classList={{ "is-on": pip <= filled() }} />}</For>
      </span>
      w{props.weight}
    </span>
  )
}

function ProposalCard(props: { proposal: Proposal }) {
  const prefs = usePrefs()
  const handle = () => (isHandle(prefs.displayName().trim()) ? prefs.displayName().trim() : "")
  const refs = () => [
    props.proposal.pr_ref && { label: "PR", value: props.proposal.pr_ref },
    props.proposal.run_ref && { label: "run", value: props.proposal.run_ref },
    props.proposal.step_ref && { label: "step", value: props.proposal.step_ref },
  ].filter(Boolean) as { label: string; value: string }[]
  return (
    <li class="card proposal-card rounded-xl border border-line p-3 text-sm" data-proposal={props.proposal.id}>
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-mono text-xs font-semibold text-brass">{props.proposal.id}</span>
        <span class="chip text-[11px]">{props.proposal.project}</span>
        <span class="ml-auto"><WeightPips weight={props.proposal.weight} /></span>
      </div>
      <h3 class="mt-2 font-semibold leading-snug text-cream">{props.proposal.title}</h3>
      <Show when={props.proposal.summary}>
        <p class="mt-1 line-clamp-4 whitespace-pre-wrap break-words text-[13px] leading-relaxed text-cream-dim">{props.proposal.summary}</p>
      </Show>
      <Show when={refs().length}>
        <div class="mt-2 flex flex-wrap gap-1">
          <For each={refs()}>{(ref) => <span class="chip font-mono text-[10px]" title={ref.value}>{ref.label} {ref.value.length > 24 ? `${ref.value.slice(0, 24)}…` : ref.value}</span>}</For>
        </div>
      </Show>
      <Show when={props.proposal.evidence.length > 0}>
        <details class="evidence mt-2 text-xs">
          <summary class="cursor-pointer select-none text-cream-dim hover:text-cream">Evidence ({props.proposal.evidence.length})</summary>
          <ul class="mt-1 flex flex-col gap-0.5" aria-label="Evidence">
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
        </details>
      </Show>
      <Show when={props.proposal.decided_by || props.proposal.reason}>
        <p class="decision-note mt-2 rounded-lg px-2.5 py-1.5 text-xs" data-decision>
          <Show when={props.proposal.decided_by}>{props.proposal.status} by {props.proposal.decided_by}</Show>
          <Show when={props.proposal.reason}>: {props.proposal.reason}</Show>
        </p>
      </Show>
      <div class="mt-3 flex items-center gap-2 border-t border-line pt-2.5 text-[11px] text-cream-dim">
        <Avatar name={props.proposal.author} size="sm" />
        <span class="min-w-0 truncate"><span class="font-medium text-cream">{props.proposal.author}</span> · {relativeTime(props.proposal.created_at)}</span>
      </div>
      <Votes postId={props.proposal.post_id} handle={handle()} />
      <Decide proposal={props.proposal} handle={handle()} />
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
    <div class="mt-2">
      <div class="flex items-center gap-1.5 text-xs">
        <span class="score-pill mr-auto rounded-md px-2 py-0.5 font-semibold tabular-nums" data-score title="+1 minus -1" data-sign={Math.sign(reactions.data?.score ?? 0)}>score {reactions.data?.score ?? 0}</span>
        <For each={["+1", "-1"] as const}>
          {(kind) => (
            <Button
              type="button"
              size="sm"
              variant={mine().includes(kind) ? "default" : "outline"}
              class="h-7 rounded-full px-2.5 tabular-nums"
              aria-label={`Vote ${kind}`}
              aria-pressed={mine().includes(kind)}
              disabled={disabled()}
              onClick={() => toggle(kind)}
            >
              {kind === "+1" ? "▲" : "▼"} <span data-count={kind}>{count(kind)}</span>
            </Button>
          )}
        </For>
      </div>
      <Show when={!props.handle}><p class="mt-1 text-xs text-cream-dim">Set a display name to vote.</p></Show>
      <Show when={error()}><p class="mt-1 text-xs text-rust">{error()}</p></Show>
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
      <div class="mt-2 flex flex-wrap gap-1.5">
        <Show when={moves().includes("accepted")}>
          <Button type="button" size="sm" class="h-7 flex-1" disabled={mutations.update.isPending} onClick={() => send({ status: "accepted" })}>Accept</Button>
        </Show>
        <Show when={moves().includes("rejected")}>
          <Button type="button" size="sm" variant="danger" class="h-7 flex-1" aria-expanded={rejecting()} disabled={mutations.update.isPending} onClick={() => { setError(""); setRejecting(!rejecting()) }}>Reject</Button>
        </Show>
        <Show when={moves().includes("withdrawn")}>
          <Button type="button" size="sm" variant="ghost" class="h-7" disabled={mutations.update.isPending} onClick={() => send({ status: "withdrawn" })}>Withdraw</Button>
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

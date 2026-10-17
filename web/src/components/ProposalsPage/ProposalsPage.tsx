import { createSignal, For, Show } from "solid-js"
import { PROPOSAL_STATUSES, type Proposal, type ProposalChanges, type ProposalStatus, type VoteKind } from "@/api/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { myVotes, useEvolveState, usePostReactions, useProposalMutations, useProposals, useVote } from "@/hooks/useProposals"
import { usePrefs } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { isHandle } from "@/utils/handles"
import { formatStamp } from "@/utils/time"

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

/** Proposals board: one column per status, votes, decide actions and the evolve banner. */
export default function ProposalsPage() {
  const proposals = useProposals(() => "")
  const evolve = useEvolveState()
  const byStatus = (status: ProposalStatus) => (proposals.data?.proposals ?? []).filter((item) => item.status === status)

  return (
    <div class="min-h-0 flex-1 overflow-y-auto p-4">
      <Show when={evolve.data?.paused}>
        <div role="alert" class="mb-4 rounded-lg border border-rust px-4 py-2 text-sm text-rust">
          Evolve paused by {evolve.data?.by ?? "unknown"} at {evolve.data?.at ? formatStamp(evolve.data.at) : "unknown time"}
        </div>
      </Show>
      <h1 class="font-display text-xl text-cream">Proposals</h1>
      <Show when={proposals.isError}><p class="mt-2 text-sm text-rust">{getErrorMessage(proposals.error, "Could not load proposals.")}</p></Show>
      <Show when={proposals.data?.has_more}><p class="mt-2 text-xs text-cream-dim">Showing the newest {proposals.data?.proposals.length} proposals.</p></Show>
      <div class="mt-4 grid gap-4 md:grid-cols-3 xl:grid-cols-6">
        <For each={PROPOSAL_STATUSES}>
          {(status) => {
            const cards = () => byStatus(status)
            const muted = () => status === "withdrawn"
            return (
              <section aria-label={`${status} proposals`} data-status={status} class="flex min-w-0 flex-col gap-2" classList={{ "opacity-60": muted() }}>
                <h2 class="text-xs uppercase tracking-wide text-cream-dim">{status} <span>({cards().length})</span></h2>
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

function ProposalCard(props: { proposal: Proposal }) {
  const prefs = usePrefs()
  const handle = () => (isHandle(prefs.displayName().trim()) ? prefs.displayName().trim() : "")
  return (
    <li class="rounded-lg border border-line p-3 text-sm" data-proposal={props.proposal.id}>
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-mono text-brass">{props.proposal.id}</span>
        <Badge>{props.proposal.project}</Badge>
        <Badge title="Weight estimate">w{props.proposal.weight}</Badge>
      </div>
      <h3 class="mt-1 font-display text-cream">{props.proposal.title}</h3>
      <p class="mt-1 line-clamp-4 whitespace-pre-wrap break-words text-cream-dim">{props.proposal.summary}</p>
      <Show when={props.proposal.evidence.length > 0}>
        <ul class="mt-2 flex flex-col gap-0.5 text-xs" aria-label="Evidence">
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
      </Show>
      <Show when={props.proposal.decided_by || props.proposal.reason}>
        <p class="mt-2 text-xs text-cream-dim" data-decision>
          <Show when={props.proposal.decided_by}>{props.proposal.status} by {props.proposal.decided_by}</Show>
          <Show when={props.proposal.reason}>: {props.proposal.reason}</Show>
        </p>
      </Show>
      <Votes postId={props.proposal.post_id} handle={handle()} />
      <Decide proposal={props.proposal} handle={handle()} />
    </li>
  )
}

function Votes(props: { postId: number | null; handle: string }) {
  const reactions = usePostReactions(() => props.postId)
  const vote = useVote(() => props.postId)
  const [error, setError] = createSignal("")
  const mine = () => myVotes(reactions.data, props.handle)
  const count = (kind: VoteKind) => reactions.data?.reactions[kind] ?? 0
  const disabled = () => !props.handle || props.postId === null || !reactions.data
  const toggle = (kind: VoteKind) => {
    setError("")
    vote.mutate(
      { kind, handle: props.handle, on: !mine().includes(kind) },
      { onError: (reason) => setError(getErrorMessage(reason, "Vote failed.")) },
    )
  }
  return (
    <div class="mt-2">
      <div class="flex items-center gap-2 text-xs">
        <span class="text-cream" data-score title="+1 minus -1">score {reactions.data?.score ?? 0}</span>
        <For each={["+1", "-1"] as const}>
          {(kind) => (
            <Button
              type="button"
              size="sm"
              variant={mine().includes(kind) ? "default" : "outline"}
              aria-label={`Vote ${kind}`}
              aria-pressed={mine().includes(kind)}
              disabled={disabled()}
              onClick={() => toggle(kind)}
            >
              {kind} <span data-count={kind}>{count(kind)}</span>
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
      <div class="mt-2 flex flex-wrap gap-1">
        <Show when={moves().includes("accepted")}>
          <Button type="button" size="sm" variant="ghost" disabled={mutations.update.isPending} onClick={() => send({ status: "accepted" })}>Accept</Button>
        </Show>
        <Show when={moves().includes("rejected")}>
          <Button type="button" size="sm" variant="ghost" disabled={mutations.update.isPending} onClick={() => { setError(""); setRejecting(!rejecting()) }}>Reject</Button>
        </Show>
        <Show when={moves().includes("withdrawn")}>
          <Button type="button" size="sm" variant="ghost" disabled={mutations.update.isPending} onClick={() => send({ status: "withdrawn" })}>Withdraw</Button>
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

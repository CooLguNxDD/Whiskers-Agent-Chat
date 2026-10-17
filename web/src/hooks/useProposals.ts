import { createMutation, createQuery, useQueryClient } from "@tanstack/solid-query"
import { getEvolveState, listProposals, listReactions, react, unreact, updateProposal } from "@/api/proposals"
import type { PostReactions, ProposalChanges, ReactionState, VoteKind } from "@/api/types"

/** The board's page size: the hub's largest page. */
const BOARD_LIMIT = 200

/** Every proposal on the board. Refreshed by proposal events (query family "proposals"). */
export function useProposals(project: () => string) {
  return createQuery(() => ({
    queryKey: ["proposals", "list", project()],
    queryFn: ({ signal }) => listProposals({ project: project() || undefined, limit: BOARD_LIMIT }, signal),
  }))
}

/** The evolve pause flag behind the banner. */
export function useEvolveState() {
  return createQuery(() => ({
    queryKey: ["proposals", "evolve"],
    queryFn: ({ signal }) => getEvolveState(signal),
  }))
}

export const reactionsKey = (postId: number | null, handle: string) => ["proposals", "reactions", postId, handle] as const

/** Exact vote counts for one proposal's feed post, plus `handle`'s own kinds (`mine`). */
export function usePostReactions(postId: () => number | null, handle: () => string) {
  return createQuery(() => ({
    queryKey: reactionsKey(postId(), handle()),
    queryFn: ({ signal }) => listReactions(postId()!, handle() || undefined, signal),
    enabled: postId() !== null,
  }))
}

/**
 * The vote kinds the listed handle holds. Read from the hub's exact `mine`,
 * never from the `handles` sample, which stops at the oldest 100 per kind.
 */
export function myVotes(state: PostReactions | undefined): VoteKind[] {
  const mine = state?.mine ?? []
  return (["+1", "-1"] as const).filter((kind) => mine.includes(kind))
}

/** `state` after its handle toggles `kind`: votes exclude each other, as on the hub. */
export function toggleVote(state: PostReactions, kind: VoteKind): PostReactions {
  const reactions = { ...state.reactions }
  let mine = [...(state.mine ?? [])]
  const drop = (name: string) => {
    if (!mine.includes(name)) return
    reactions[name] = Math.max(0, (reactions[name] ?? 0) - 1)
    if (reactions[name] === 0) delete reactions[name]
    mine = mine.filter((item) => item !== name)
  }
  if (mine.includes(kind)) {
    drop(kind)
  } else {
    drop(kind === "+1" ? "-1" : "+1")
    reactions[kind] = (reactions[kind] ?? 0) + 1
    mine = [...mine, kind]
  }
  return { ...state, reactions, mine, score: (reactions["+1"] ?? 0) - (reactions["-1"] ?? 0) }
}

export interface Vote {
  kind: VoteKind
  handle: string
  /** `true` reacts, `false` removes the caller's reaction. */
  on: boolean
}

/**
 * Toggle a vote with an optimistic count; the previous counts come back on error.
 * The board keeps one vote in flight per post (buttons disabled while pending),
 * so a rollback never overwrites a later vote.
 */
export function useVote(postId: () => number | null) {
  const queryClient = useQueryClient()
  return createMutation<ReactionState, Error, Vote, { previous?: PostReactions }>(() => ({
    mutationFn: ({ kind, handle, on }) =>
      on ? react(postId()!, kind, handle) : unreact(postId()!, kind, handle),
    onMutate: async ({ kind, handle }) => {
      const key = reactionsKey(postId(), handle)
      await queryClient.cancelQueries({ queryKey: key })
      const previous = queryClient.getQueryData<PostReactions>(key)
      if (previous) queryClient.setQueryData<PostReactions>(key, toggleVote(previous, kind))
      return { previous }
    },
    onError: (_error, { handle }, context) => {
      if (context?.previous) queryClient.setQueryData(reactionsKey(postId(), handle), context.previous)
    },
    // The hub's answer carries exact counts and the caller's kinds.
    onSuccess: (result, { handle }) => {
      const key = reactionsKey(postId(), handle)
      const current = queryClient.getQueryData<PostReactions>(key)
      queryClient.setQueryData<PostReactions>(key, {
        post_id: result.post_id,
        reactions: result.reactions,
        handles: current?.handles ?? {},
        score: result.score,
        mine: result.mine,
      })
    },
  }))
}

export function useProposalMutations() {
  const queryClient = useQueryClient()
  return {
    update: createMutation(() => ({
      mutationFn: ({ id, actor, changes }: { id: string; actor: string; changes: ProposalChanges }) =>
        updateProposal(id, actor, changes),
      onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["proposals", "list"] }),
    })),
  }
}

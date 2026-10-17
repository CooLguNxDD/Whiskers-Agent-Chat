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

export const reactionsKey = (postId: number | null) => ["proposals", "reactions", postId] as const

/** Exact vote counts for one proposal's feed post. */
export function usePostReactions(postId: () => number | null) {
  return createQuery(() => ({
    queryKey: reactionsKey(postId()),
    queryFn: ({ signal }) => listReactions(postId()!, signal),
    enabled: postId() !== null,
  }))
}

/** The kinds `handle` holds, from the reaction sample (oldest 100 per kind). */
export function myVotes(state: PostReactions | undefined, handle: string): VoteKind[] {
  if (!state || !handle) return []
  const lower = handle.toLowerCase()
  return (["+1", "-1"] as const).filter((kind) => (state.handles[kind] ?? []).some((name) => name.toLowerCase() === lower))
}

/** `state` after `handle` toggles `kind`: votes exclude each other, as on the hub. */
export function toggleVote(state: PostReactions, handle: string, kind: VoteKind): PostReactions {
  const reactions = { ...state.reactions }
  const handles = { ...state.handles }
  const holds = (name: string) => (handles[name] ?? []).some((item) => item.toLowerCase() === handle.toLowerCase())
  const drop = (name: string) => {
    if (!holds(name)) return
    reactions[name] = Math.max(0, (reactions[name] ?? 0) - 1)
    if (reactions[name] === 0) delete reactions[name]
    handles[name] = (handles[name] ?? []).filter((item) => item.toLowerCase() !== handle.toLowerCase())
  }
  if (holds(kind)) {
    drop(kind)
  } else {
    drop(kind === "+1" ? "-1" : "+1")
    reactions[kind] = (reactions[kind] ?? 0) + 1
    handles[kind] = [...(handles[kind] ?? []), handle]
  }
  return { ...state, reactions, handles, score: (reactions["+1"] ?? 0) - (reactions["-1"] ?? 0) }
}

export interface Vote {
  kind: VoteKind
  handle: string
  /** `true` reacts, `false` removes the caller's reaction. */
  on: boolean
}

/** Toggle a vote with an optimistic count; the previous counts come back on error. */
export function useVote(postId: () => number | null) {
  const queryClient = useQueryClient()
  return createMutation<ReactionState, Error, Vote, { previous?: PostReactions }>(() => ({
    mutationFn: ({ kind, handle, on }) =>
      on ? react(postId()!, kind, handle) : unreact(postId()!, kind, handle),
    onMutate: async ({ kind, handle }) => {
      const key = reactionsKey(postId())
      await queryClient.cancelQueries({ queryKey: key })
      const previous = queryClient.getQueryData<PostReactions>(key)
      if (previous) queryClient.setQueryData<PostReactions>(key, toggleVote(previous, handle, kind))
      return { previous }
    },
    onError: (_error, _vars, context) => {
      if (context?.previous) queryClient.setQueryData(reactionsKey(postId()), context.previous)
    },
    // The hub's answer carries exact counts and the caller's kinds.
    onSuccess: (result, { handle }) => {
      const key = reactionsKey(postId())
      const current = queryClient.getQueryData<PostReactions>(key)
      const lower = handle.toLowerCase()
      const handles: Record<string, string[]> = {}
      for (const [kind, names] of Object.entries(current?.handles ?? {})) {
        handles[kind] = names.filter((name) => name.toLowerCase() !== lower)
      }
      for (const kind of result.mine) handles[kind] = [...(handles[kind] ?? []), handle]
      queryClient.setQueryData<PostReactions>(key, {
        post_id: result.post_id,
        reactions: result.reactions,
        handles,
        score: result.score,
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

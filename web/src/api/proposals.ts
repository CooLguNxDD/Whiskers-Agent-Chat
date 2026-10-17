import { api, queryString } from "./client"
import type { EvolveState, Proposal, ProposalChanges, ProposalPage, ProposalStatus } from "./types"

export { listReactions, react, unreact } from "./feed"

export interface ProposalFilters {
  project?: string
  status?: ProposalStatus
  cursor?: string | null
  limit?: number
}

/** Proposals newest first. Filters are exact matches; page with `next_cursor`. */
export function listProposals(params: ProposalFilters = {}, signal?: AbortSignal) {
  return api<ProposalPage>(`/api/v1/proposals${queryString({ ...params })}`, { signal })
}

/** One proposal by `P<n>` or `n`. */
export function getProposal(id: string | number, signal?: AbortSignal) {
  return api<{ proposal: Proposal }>(`/api/v1/proposals/${encodeURIComponent(String(id))}`, { signal })
}

/**
 * Edit fields or move status as `actor`. The hub decides who may decide
 * (accept, reject, withdraw) and answers with the reason when it refuses.
 */
export function updateProposal(id: string | number, actor: string, changes: ProposalChanges) {
  return api<{ proposal: Proposal; idempotent?: boolean }>(`/api/v1/proposals/${encodeURIComponent(String(id))}`, {
    method: "PATCH",
    body: JSON.stringify({ ...changes, actor }),
  })
}

/** The evolve pause flag. Read-only: pausing is a human chat command. */
export function getEvolveState(signal?: AbortSignal) {
  return api<EvolveState>("/api/v1/evolve/state", { signal })
}

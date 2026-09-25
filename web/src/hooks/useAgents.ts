import { createQuery } from "@tanstack/solid-query"
import { listAgents } from "@/api/fleet"

export function useAgents() {
  return createQuery(() => ({ queryKey: ["agents"], queryFn: listAgents, refetchInterval: 15_000 }))
}

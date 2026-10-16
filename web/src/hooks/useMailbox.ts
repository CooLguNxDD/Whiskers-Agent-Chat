import { createInfiniteQuery, createMutation, createQuery, useQueryClient } from "@tanstack/solid-query"
import { ackMail, listMail, mailCounts, markMailRead, sendMail } from "@/api/mailbox"
import type { MailCounts, MailFilter, MailUpdate } from "@/api/types"
import { usePrefs, useUIStore } from "@/store"
import { isHandle } from "@/utils/handles"
import { countsFromUpdate } from "@/utils/unread"

/** The handle whose inbox is shown: the inbox selector, else the display name. Empty when invalid. */
export function useInboxHandle(): () => string {
  const ui = useUIStore()
  const prefs = usePrefs()
  return () => {
    const value = (ui.inboxHandle() || prefs.displayName()).trim()
    return isHandle(value) ? value : ""
  }
}

/** Unread/unacked counts. Refreshed by `mail` events (query family "mail"). */
export function useMailCounts(agent: () => string) {
  return createQuery(() => ({
    queryKey: ["mail", "counts", agent()],
    queryFn: () => mailCounts(agent()),
    enabled: agent() !== "",
    refetchInterval: 60_000,
  }))
}

export function useMailList(agent: () => string, state: () => MailFilter) {
  return createInfiniteQuery(() => ({
    queryKey: ["mail", "list", agent(), state()],
    queryFn: ({ pageParam, signal }) => listMail(agent(), { state: state(), since_id: pageParam, limit: 50 }, signal),
    initialPageParam: 0,
    getNextPageParam: (page) => (page.has_more ? page.cursor : undefined),
    enabled: agent() !== "",
  }))
}

export function useMailMutations(agent: () => string) {
  const queryClient = useQueryClient()
  const settle = (update: MailUpdate) => {
    queryClient.setQueryData<MailCounts>(["mail", "counts", update.agent], countsFromUpdate(update))
    void queryClient.invalidateQueries({ queryKey: ["mail", "list", update.agent] })
  }
  return {
    read: createMutation(() => ({ mutationFn: (ids: number[]) => markMailRead(agent(), { ids }), onSuccess: settle })),
    readAll: createMutation(() => ({ mutationFn: (upTo: number) => markMailRead(agent(), { up_to_id: upTo }), onSuccess: settle })),
    ack: createMutation(() => ({ mutationFn: (ids: number[]) => ackMail(agent(), ids), onSuccess: settle })),
    send: createMutation(() => ({
      mutationFn: sendMail,
      onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["mail"] }),
    })),
  }
}

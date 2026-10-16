import type { Mail, MailCounts, MailUpdate } from "@/api/types"

export const BADGE_MAX = 99

/** The nav badge text: nothing at zero, the number, or "99+". */
export function unreadBadge(unread: number | null | undefined): string | null {
  if (unread == null || !Number.isFinite(unread) || unread <= 0) return null
  return unread > BADGE_MAX ? `${BADGE_MAX}+` : String(Math.floor(unread))
}

/** Counts from a mark-read or ack answer, which carries the fresh totals. */
export function countsFromUpdate(update: MailUpdate): MailCounts {
  return { agent: update.agent, unread: update.unread, unacked: update.unacked, total: update.total }
}

/** Apply a mark-read or ack answer to the rows on screen, without refetching. */
export function applyMailUpdate(mail: readonly Mail[], update: MailUpdate, action: "read" | "ack", at: string): Mail[] {
  const changed = new Set(update.updated)
  return mail.map((item) => {
    if (!changed.has(item.id)) return item
    if (action === "ack") return { ...item, read_at: item.read_at ?? at, acked_at: at, state: "acked" }
    return { ...item, read_at: at, state: item.acked_at ? "acked" : "read" }
  })
}

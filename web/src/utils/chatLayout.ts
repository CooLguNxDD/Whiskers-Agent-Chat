/**
 * Pure helpers for the chat transcript: sender grouping, day dividers, avatars,
 * in-room find, and body segmenting. Every segment renders as a text node (or an
 * element holding text), never as HTML, so markup inside a message stays inert.
 */
import type { FleetMessage } from "@/api/types"

/** Consecutive messages from one author within this gap share a group. */
export const GROUP_GAP_MS = 5 * 60 * 1000

export type TranscriptItem =
  | { kind: "day"; key: string; label: string }
  | { kind: "message"; key: string; message: FleetMessage; first: boolean; last: boolean }

function dayKey(date: Date): string {
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`
}

/** "Today", "Yesterday", or a short date (with year only when it differs). */
export function dayLabel(iso: string, now = new Date()): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return "Unknown date"
  if (dayKey(date) === dayKey(now)) return "Today"
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (dayKey(date) === dayKey(yesterday)) return "Yesterday"
  return new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    ...(date.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  }).format(date)
}

/** Clock time only, for rows inside a group. */
export function clockTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(date)
}

function sameGroup(previous: FleetMessage, next: FleetMessage): boolean {
  if (previous.author !== next.author) return false
  // A reply always opens its own group so its quote card has a header above it.
  if (next.reply_to != null) return false
  const gap = new Date(next.created_at).getTime() - new Date(previous.created_at).getTime()
  return Number.isFinite(gap) && gap >= 0 && gap <= GROUP_GAP_MS
}

/** Messages (sorted by id) → day dividers plus messages flagged first/last of their sender group. */
export function buildTranscript(messages: FleetMessage[], now = new Date()): TranscriptItem[] {
  const items: TranscriptItem[] = []
  let lastDay = ""
  for (let index = 0; index < messages.length; index++) {
    const message = messages[index]
    const date = new Date(message.created_at)
    const day = Number.isNaN(date.getTime()) ? "invalid" : dayKey(date)
    const newDay = day !== lastDay
    if (newDay) {
      items.push({ kind: "day", key: `day-${day}-${message.id}`, label: dayLabel(message.created_at, now) })
      lastDay = day
    }
    const previous = messages[index - 1]
    const next = messages[index + 1]
    const nextDate = next ? new Date(next.created_at) : null
    const nextSameDay = nextDate != null && !Number.isNaN(nextDate.getTime()) && dayKey(nextDate) === day
    items.push({
      kind: "message",
      key: `m-${message.id}`,
      message,
      first: newDay || !previous || !sameGroup(previous, message),
      last: !next || !nextSameDay || !sameGroup(message, next),
    })
  }
  return items
}

/** Stable hue (0–359) for an author, so each cat keeps its colour across sessions. */
export function avatarHue(name: string): number {
  let hash = 0
  for (const char of name) hash = (hash * 31 + (char.codePointAt(0) ?? 0)) >>> 0
  return hash % 360
}

/** Up to two letters: "fleet-librarian" → "FL", "cat-3" → "C3", "claude" → "CL". */
export function initials(name: string): string {
  const parts = name.split(/[-_.\s]+/).filter(Boolean)
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase()
  const word = parts[0] ?? "?"
  return word.slice(0, 2).toUpperCase()
}

/** Agent handles look like bots; used only to pick a badge, never for permissions. */
export function looksLikeAgent(name: string): boolean {
  return /^(cat|worker|reviewer|fleet|agent|bot|claude|skill)[-_a-z0-9]*$/i.test(name)
}

/** Ids of loaded messages whose text or author contains `query` (case-insensitive), oldest first. */
export function findMatches(messages: FleetMessage[], query: string): number[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return []
  return messages
    .filter((message) => message.text.toLowerCase().includes(needle) || message.author.toLowerCase().includes(needle))
    .map((message) => message.id)
}

export type InlineSegment =
  | { kind: "text"; text: string }
  | { kind: "mention"; text: string }
  | { kind: "code"; text: string }
  | { kind: "link"; text: string; href: string }
  | { kind: "match"; text: string }

export type BlockSegment =
  | { kind: "para"; text: string }
  | { kind: "codeblock"; text: string; lang: string }

/** Split on ``` fences. An unclosed fence runs to the end of the message. */
export function blockSegments(text: string): BlockSegment[] {
  const out: BlockSegment[] = []
  const fence = /```([\w+-]*)[^\S\n]*\n?([\s\S]*?)(?:```|$)/g
  let last = 0
  for (const match of text.matchAll(fence)) {
    const start = match.index ?? 0
    // The newline next to a fence belongs to the fence, not the paragraph.
    if (start > last) out.push({ kind: "para", text: text.slice(last, start).replace(/\n$/, "") })
    out.push({ kind: "codeblock", lang: match[1] ?? "", text: (match[2] ?? "").replace(/\n$/, "") })
    last = start + match[0].length
    if (match[0].length === 0) break
  }
  if (last < text.length) out.push({ kind: "para", text: text.slice(last).replace(/^\n/, "") })
  return out.filter((segment) => segment.kind === "codeblock" || segment.text.length > 0)
}

const INLINE_RE = /(`[^`\n]+`)|(https?:\/\/[^\s<>"')\]]+)|(@[A-Za-z0-9][A-Za-z0-9_-]{0,63})/g

/**
 * Inline segments: `code`, http(s) links, known @mentions, and find-highlights.
 * Only http(s) URLs become links, so a `javascript:` text never does.
 */
export function inlineSegments(text: string, mentions: string[], query = ""): InlineSegment[] {
  const raw: InlineSegment[] = []
  let last = 0
  for (const match of text.matchAll(INLINE_RE)) {
    const start = match.index ?? 0
    if (start > last) raw.push({ kind: "text", text: text.slice(last, start) })
    const [whole, code, url, mention] = match
    if (code) raw.push({ kind: "code", text: code.slice(1, -1) })
    else if (url) raw.push({ kind: "link", text: url, href: url })
    else if (mention && mentions.includes(mention.slice(1))) raw.push({ kind: "mention", text: mention })
    else raw.push({ kind: "text", text: whole })
    last = start + whole.length
  }
  if (last < text.length) raw.push({ kind: "text", text: text.slice(last) })
  const needle = query.trim().toLowerCase()
  if (!needle) return raw
  const out: InlineSegment[] = []
  for (const segment of raw) {
    if (segment.kind !== "text") { out.push(segment); continue }
    const lower = segment.text.toLowerCase()
    let from = 0
    let at = lower.indexOf(needle)
    while (at !== -1) {
      if (at > from) out.push({ kind: "text", text: segment.text.slice(from, at) })
      out.push({ kind: "match", text: segment.text.slice(at, at + needle.length) })
      from = at + needle.length
      at = lower.indexOf(needle, from)
    }
    if (from < segment.text.length) out.push({ kind: "text", text: segment.text.slice(from) })
  }
  return out
}

/** A message this long (chars or lines) starts collapsed. */
export function isLong(text: string): boolean {
  return text.length > 900 || text.split("\n").length > 14
}

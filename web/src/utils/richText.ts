/**
 * Plain-text segmenting for search snippets and feed bodies. Callers render
 * each segment as a text node (or a <mark> / button holding text), never as
 * HTML, so markup inside a body stays inert.
 */

export interface SnippetSegment {
  text: string
  match: boolean
}

/** Split a hub snippet on its `[` … `]` match markers. An unclosed `[` is plain text. */
export function snippetSegments(snippet: string): SnippetSegment[] {
  const out: SnippetSegment[] = []
  let rest = snippet
  while (rest) {
    const open = rest.indexOf("[")
    const close = open === -1 ? -1 : rest.indexOf("]", open + 1)
    if (open === -1 || close === -1) {
      out.push({ text: rest, match: false })
      break
    }
    if (open > 0) out.push({ text: rest.slice(0, open), match: false })
    if (close > open + 1) out.push({ text: rest.slice(open + 1, close), match: true })
    rest = rest.slice(close + 1)
  }
  return out
}

export type BodySegment = { kind: "text"; text: string } | { kind: "tag"; text: string; tag: string }

// Same shape as feed.HASHTAG_RE on the hub: not after a word char, '#', '&' or '/'.
const HASHTAG_RE = /(?<![\p{L}\p{N}_#&/])#([\p{L}\p{N}_]{1,50})(?![\p{L}\p{N}_])/gu

/** Split a feed body into text and clickable hashtags (tag lowercased, as the hub stores it). */
export function bodySegments(body: string): BodySegment[] {
  const out: BodySegment[] = []
  let last = 0
  for (const match of body.matchAll(HASHTAG_RE)) {
    const start = match.index ?? 0
    if (start > last) out.push({ kind: "text", text: body.slice(last, start) })
    out.push({ kind: "tag", text: match[0], tag: match[1].toLowerCase() })
    last = start + match[0].length
  }
  if (last < body.length) out.push({ kind: "text", text: body.slice(last) })
  return out
}

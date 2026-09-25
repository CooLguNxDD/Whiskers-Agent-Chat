export interface SseEvent {
  id?: string
  event: string
  data: string
}

/** Incremental SSE parser. Accepts arbitrary chunks, CRLF, comments, and multiline data. */
export function createSseParser(onEvent: (event: SseEvent) => void) {
  let buffer = ""
  let afterCR = false
  return {
    push(chunk: string) {
      if (!chunk) return
      // Remember CR across reads: its following LF is not another newline.
      const normalized = afterCR && chunk.startsWith("\n") ? chunk.slice(1) : chunk
      afterCR = chunk.endsWith("\r")
      buffer += normalized.replace(/\r\n/g, "\n").replace(/\r/g, "\n")
      let splitAt = buffer.indexOf("\n\n")
      while (splitAt !== -1) {
        if (splitAt > 1_048_576) throw new Error("SSE frame exceeds size limit")
        const parsed = parseFrame(buffer.slice(0, splitAt))
        buffer = buffer.slice(splitAt + 2)
        if (parsed) onEvent(parsed)
        splitAt = buffer.indexOf("\n\n")
      }
      if (buffer.length > 1_048_576) throw new Error("SSE frame exceeds size limit")
    },
  }
}

function parseFrame(raw: string): SseEvent | null {
  let event = "message"
  let id: string | undefined
  const data: string[] = []
  for (const line of raw.split("\n")) {
    if (!line || line.startsWith(":")) continue
    const colon = line.indexOf(":")
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? "" : line.slice(colon + 1)
    if (value.startsWith(" ")) value = value.slice(1)
    if (field === "event") event = value
    else if (field === "data") data.push(value)
    else if (field === "id") id = value
  }
  if (data.length === 0) return null
  return { id, event, data: data.join("\n") }
}

export interface StreamHandle {
  close: () => void
}

/**
 * Fetch-based event stream so the bearer token can be sent.
 * EventSource cannot set Authorization.
 */
export function openEventStream(opts: {
  afterEventId: number
  token: string
  signal: AbortSignal
}): Promise<Response> {
  const headers = new Headers({ Accept: "text/event-stream" })
  if (opts.token) headers.set("Authorization", `Bearer ${opts.token}`)
  return fetch(`/api/v1/events?after_event_id=${opts.afterEventId}`, {
    headers,
    signal: opts.signal,
  })
}

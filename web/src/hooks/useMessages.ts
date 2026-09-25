import { createEffect, createSignal, onCleanup, type Accessor } from "solid-js"
import { getMessages } from "@/api/fleet"
import type { FleetMessage, MessagePage } from "@/api/types"
import { registerMessageWindow } from "./messageRefresh"

export const MESSAGE_WINDOW_LIMIT = 500

export interface MessageWindow {
  messages: Accessor<FleetMessage[]>
  loading: Accessor<boolean>
  loadingOlder: Accessor<boolean>
  loadingNewer: Accessor<boolean>
  error: Accessor<unknown>
  hasOlder: Accessor<boolean>
  hasNewer: Accessor<boolean>
  pendingNewer: Accessor<boolean>
  setAtLatest: (atLatest: boolean) => void
  loadOlder: () => Promise<void>
  loadNewer: () => Promise<void>
  jumpLatest: () => Promise<void>
  retry: () => Promise<void>
}

function appendUnique(current: FleetMessage[], incoming: FleetMessage[]) {
  const byId = new Map(current.map((message) => [message.id, message]))
  for (const message of incoming) byId.set(message.id, message)
  return [...byId.values()].sort((a, b) => a.id - b.id)
}

function trimNewest(messages: FleetMessage[]) {
  return messages.length > MESSAGE_WINDOW_LIMIT ? messages.slice(-MESSAGE_WINDOW_LIMIT) : messages
}

function trimOldest(messages: FleetMessage[]) {
  return messages.length > MESSAGE_WINDOW_LIMIT ? messages.slice(messages.length - MESSAGE_WINDOW_LIMIT) : messages
}

/** Pure window operation used by the controller and regression tests. */
export function mergeMessageWindow(current: FleetMessage[], incoming: FleetMessage[], edge: "older" | "newer") {
  const merged = appendUnique(current, incoming)
  return edge === "older" ? trimNewest(merged) : trimOldest(merged)
}

export function createMessageWindow(channel: Accessor<string>): MessageWindow {
  const [messages, setMessages] = createSignal<FleetMessage[]>([])
  const [loading, setLoading] = createSignal(false)
  const [loadingOlder, setLoadingOlder] = createSignal(false)
  const [loadingNewer, setLoadingNewer] = createSignal(false)
  const [error, setError] = createSignal<unknown>(null)
  const [hasOlder, setHasOlder] = createSignal(false)
  const [hasNewer, setHasNewer] = createSignal(false)
  const [pendingNewer, setPendingNewer] = createSignal(false)
  let atLatest = true
  let generation = 0
  let abort: AbortController | undefined
  let activeChannel = ""

  const valid = (currentGeneration: number, signal: AbortSignal) => currentGeneration === generation && !signal.aborted

  async function fetchLatest(currentGeneration: number, signal: AbortSignal) {
    setLoading(true)
    setError(null)
    try {
      const page = await getMessages(activeChannel, { limit: 50 }, signal)
      if (!valid(currentGeneration, signal)) return
      setMessages(page.messages.slice(-MESSAGE_WINDOW_LIMIT))
      setHasOlder(page.has_more)
      setHasNewer(false)
      setPendingNewer(false)
    } catch (reason) {
      if (valid(currentGeneration, signal)) setError(reason)
    } finally {
      if (valid(currentGeneration, signal)) setLoading(false)
    }
  }

  async function loadOlder() {
    if (loadingOlder() || !hasOlder() || !messages().length || !activeChannel) return
    const currentGeneration = generation
    const signal = abort?.signal
    if (!signal) return
    setLoadingOlder(true)
    setError(null)
    try {
      const page = await getMessages(activeChannel, { before_id: messages()[0].id, limit: 50 }, signal)
      if (!valid(currentGeneration, signal)) return
      const merged = appendUnique(page.messages, messages())
      setMessages(mergeMessageWindow(messages(), page.messages, "older"))
      setHasOlder(page.has_more)
      // Prepending pages can evict the live edge; it can be fetched forward again.
      setHasNewer(merged.length > MESSAGE_WINDOW_LIMIT)
    } catch (reason) {
      if (valid(currentGeneration, signal)) setError(reason)
    } finally {
      if (valid(currentGeneration, signal)) setLoadingOlder(false)
    }
  }

  async function loadNewer() {
    if (loadingNewer() || !messages().length || !activeChannel) return
    const currentGeneration = generation
    const signal = abort?.signal
    if (!signal) return
    setLoadingNewer(true)
    setError(null)
    try {
      let cursor = messages()[messages().length - 1].id
      let more = true
      let pages = 0
      while (more && pages < 20) {
        const page: MessagePage = await getMessages(activeChannel, { since_id: cursor, limit: 50 }, signal)
        if (!valid(currentGeneration, signal)) return
        setMessages(mergeMessageWindow(messages(), page.messages, "newer"))
        more = page.has_more
        if (page.messages.length > 0) cursor = page.messages[page.messages.length - 1].id
        pages += 1
        if (page.messages.length === 0) break
      }
      setHasNewer(more)
      setPendingNewer(false)
    } catch (reason) {
      if (valid(currentGeneration, signal)) setError(reason)
    } finally {
      if (valid(currentGeneration, signal)) setLoadingNewer(false)
    }
  }

  function refresh(reset: boolean, targetChannel?: string) {
    if (targetChannel && targetChannel !== activeChannel) return
    if (reset) {
      void jumpLatest()
      return
    }
    if (atLatest) void loadNewer()
    else setPendingNewer(true)
  }

  async function jumpLatest() {
    if (!activeChannel) return
    atLatest = true
    setPendingNewer(false)
    const currentGeneration = generation
    const signal = abort?.signal
    if (signal) await fetchLatest(currentGeneration, signal)
  }

  createEffect(() => {
    const nextChannel = channel()
    activeChannel = nextChannel
    generation += 1
    abort?.abort()
    abort = new AbortController()
    setMessages([])
    setHasOlder(false)
    setHasNewer(false)
    setPendingNewer(false)
    void fetchLatest(generation, abort.signal)
    const unregister = registerMessageWindow(refresh)
    onCleanup(() => {
      unregister()
      abort?.abort()
    })
  })

  onCleanup(() => abort?.abort())

  return {
    messages,
    loading,
    loadingOlder,
    loadingNewer,
    error,
    hasOlder,
    hasNewer,
    pendingNewer,
    setAtLatest(value) {
      atLatest = value
      if (value && pendingNewer()) void loadNewer()
    },
    loadOlder,
    loadNewer,
    jumpLatest,
    retry: () => (messages().length ? loadNewer() : jumpLatest()),
  }
}

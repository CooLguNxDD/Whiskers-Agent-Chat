import { createEffect, createSignal, onCleanup, type ParentProps } from "solid-js"
import { useQueryClient } from "@tanstack/solid-query"
import { openEventStream, createSseParser } from "@/api/stream"
import { EventRefresh } from "@/api/eventRefresh"
import { refreshMessageWindows } from "./messageRefresh"
import { useSession } from "@/store"

export type ConnectionStatus = "connecting" | "connected" | "disconnected" | "unauthorized"
const [status, setStatus] = createSignal<ConnectionStatus>("connecting")
const [attempt, setAttempt] = createSignal(0)
const [reconnects, setReconnects] = createSignal(0)

export function useFleetStream() {
  return { status, attempt, reconnects }
}

/** One shell-owned stream, with bounded batched refreshes of query data. */
export function FleetStreamProvider(props: ParentProps) {
  const session = useSession()
  const queryClient = useQueryClient()
  const refresh = new EventRefresh()
  let attemptNumber = 0

  createEffect(() => {
    const token = session.token()
    const abort = new AbortController()
    let timer: number | undefined
    let retry: number | undefined
    let refreshing = false

    const flush = () => {
      if (timer !== undefined) window.clearTimeout(timer)
      timer = undefined
      if (refreshing || !refresh.pending) return
      refreshing = true
      const requests: Promise<unknown>[] = []
      refresh.flush((key) => {
        if (key === "messages") refreshMessageWindows()
        else requests.push(queryClient.invalidateQueries({ queryKey: [key] }))
      })
      void Promise.allSettled(requests).then(() => {
        refreshing = false
        if (refresh.pending && !abort.signal.aborted) timer = window.setTimeout(flush, 150)
      })
    }

    const run = async (): Promise<void> => {
      setStatus("connecting")
      const decoder = new TextDecoder()
      const parser = createSseParser((event) => {
        refresh.accept(event)
        if (event.event === "reset") refreshMessageWindows(true)
        if (timer === undefined) timer = window.setTimeout(flush, 150)
      })
      let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
      const started = Date.now()
      try {
        const response = await openEventStream({ afterEventId: refresh.cursor, token, signal: abort.signal })
        if (response.status === 401 || response.status === 403) {
          session.markTokenRequired()
          setStatus("unauthorized")
          return
        }
        if (!response.ok || !response.body) throw new Error(`event stream ${response.status}`)
        setStatus("connected")
        setAttempt(0)
        attemptNumber = 0
        refreshMessageWindows()
        for (const key of ["channels", "tasks", "agents"]) {
          void queryClient.invalidateQueries({ queryKey: [key] })
        }
        reader = response.body.getReader()
        while (!abort.signal.aborted) {
          const { value, done } = await reader.read()
          if (done) break
          parser.push(decoder.decode(value, { stream: true }))
        }
        if (!abort.signal.aborted) throw new Error("event stream closed")
      } catch {
        if (abort.signal.aborted) return
        if (Date.now() - started > 30_000) attemptNumber = 0
        const next = ++attemptNumber
        setAttempt(next)
        setReconnects((value) => value + 1)
        setStatus("disconnected")
        retry = window.setTimeout(() => void run(), Math.min(30_000, 500 * 2 ** Math.min(next, 6)) + Math.random() * 250)
      } finally {
        flush()
        if (reader) {
          await reader.cancel().catch(() => undefined)
          reader.releaseLock()
        }
      }
    }

    void run()
    onCleanup(() => {
      abort.abort()
      if (retry !== undefined) window.clearTimeout(retry)
      if (timer !== undefined) window.clearTimeout(timer)
      flush()
    })
  })

  return props.children
}

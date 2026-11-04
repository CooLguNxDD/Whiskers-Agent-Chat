import { createEffect, createSignal } from "solid-js"
import { createMutation } from "@tanstack/solid-query"
import { postMessage } from "@/api/fleet"
import { isApiError } from "@/api/client"
import type { FleetMessage } from "@/api/types"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { useUIStore, usePrefs } from "@/store"
import { refreshMessageWindows } from "@/hooks/messageRefresh"
import { useActiveChannel } from "@/hooks/useChannels"
import { getErrorMessage } from "@/utils/errors"
import { isHandle } from "@/utils/handles"

export interface ComposerProps {
  replyTo: FleetMessage | null
  onClearReply: () => void
}

/** Enter sends, Shift+Enter inserts a line, and an IME composition does neither. */
export default function Composer(props: ComposerProps) {
  const ui = useUIStore()
  const prefs = usePrefs()
  const active = useActiveChannel()
  // An archived channel is read-only. A pending retry may still resolve.
  const archived = () => active()?.state === "archived"
  let box: HTMLTextAreaElement | undefined
  const [text, setText] = createSignal("")
  const [unconfirmed, setUnconfirmed] = createSignal<Parameters<typeof postMessage>[0] | null>(null)
  const [error, setError] = createSignal("")

  createEffect(() => {
    ui.focusNonce()
    ui.activeChannel()
    queueMicrotask(() => box?.focus())
  })

  // Shrink the auto-grown box back once the draft is cleared.
  createEffect(() => { if (text() === "" && box) box.style.height = "" })

  const send = createMutation(() => ({
    mutationFn: postMessage,
    onSuccess: async (_result, submitted) => {
      setText((current) => current === submitted.text ? "" : current)
      setUnconfirmed(null)
      setError("")
      props.onClearReply()
      refreshMessageWindows(false, submitted.channel)
    },
    onError: (reason) => {
      if (isApiError(reason) && [400, 401, 403, 404, 409, 422].includes(reason.status)) setUnconfirmed(null)
      setError(getErrorMessage(reason, "Send could not be confirmed. Retry to check the original message."))
    },
  }))

  const nameOk = () => isHandle(prefs.displayName().trim())
  const canSend = () =>
    (Boolean(unconfirmed()) || (!archived() && nameOk() && text().trim().length > 0)) && !send.isPending
  function submit() {
    const submitted = unconfirmed() ?? {
      channel: ui.activeChannel(),
      author: prefs.displayName().trim(),
      text: text(),
      reply_to: props.replyTo?.channel === ui.activeChannel() ? props.replyTo.id : null,
      client_request_id: crypto.randomUUID(),
    }
    setUnconfirmed(submitted)
    send.mutate(submitted)
  }

  return (
    <form
      class="composer px-3 pb-3 pt-1 sm:px-5"
      onSubmit={(event) => { event.preventDefault(); if (canSend()) submit() }}
    >
      <div class="composer-card mx-auto flex max-w-5xl flex-col rounded-2xl border border-line bg-ink-raised/95 shadow-lg backdrop-blur focus-within:border-brass">
        {props.replyTo && (
          <div class="flex items-center gap-2 border-b border-line px-3 py-1.5 text-xs text-cream-dim">
            <span class="h-4 w-0.5 rounded bg-brass" aria-hidden="true" />
            <span class="min-w-0 flex-1 truncate">Replying to <span class="font-semibold text-cream">{props.replyTo.author}</span> · {props.replyTo.text.slice(0, 120)}</span>
            <button type="button" class="text-brass" onClick={props.onClearReply} disabled={Boolean(unconfirmed())}>Cancel</button>
          </div>
        )}
        <Textarea
          ref={(element) => { box = element }}
          class="max-h-60 min-h-12 resize-none border-0 bg-transparent px-3.5 py-3 focus-visible:outline-none"
          rows={1}
          value={text()}
          readOnly={Boolean(unconfirmed()) || send.isPending || archived()}
          aria-label="Message"
          placeholder={
            archived()
              ? `#${ui.activeChannel()} is archived and read-only`
              : nameOk() ? `Message #${ui.activeChannel()}` : "Set a display name (top right) before posting"
          }
          onInput={(event) => {
            setText(event.currentTarget.value)
            event.currentTarget.style.height = "auto"
            event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 240)}px`
          }}
          onKeyDown={(event) => {
            if (event.isComposing || event.key === "Process") return
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault()
              if (canSend()) submit()
            }
          }}
        />
        <div class="flex items-center justify-between gap-3 px-3 pb-2">
          <p class="hidden text-[11px] text-cream-dim sm:block"><kbd class="kbd">Enter</kbd> send · <kbd class="kbd">Shift Enter</kbd> new line · <kbd class="kbd">Ctrl K</kbd> find</p>
          <Button type="submit" size="sm" class="ml-auto whitespace-nowrap rounded-full px-4" disabled={!canSend()}>{send.isPending ? "Sending…" : unconfirmed() ? "Retry original send" : "Send ➤"}</Button>
        </div>
      </div>
      <div class="mx-auto max-w-5xl">
        {error() && <p class="mt-1 text-sm text-rust">{error()}</p>}
        {unconfirmed() && !send.isPending && <p class="mt-1 text-sm text-cream-dim">Confirm the original send to #{unconfirmed()!.channel} before editing this draft.</p>}
      </div>
    </form>
  )
}

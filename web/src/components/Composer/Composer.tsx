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
      class="border-t border-line bg-ink-sunken/80 p-3"
      onSubmit={(event) => { event.preventDefault(); if (canSend()) submit() }}
    >
      <div class="mx-auto flex max-w-3xl flex-col gap-2">
        {props.replyTo && (
          <div class="flex items-center justify-between text-xs text-cream-dim">
            <span>Replying to {props.replyTo.author}</span>
            <button type="button" class="text-brass" onClick={props.onClearReply} disabled={Boolean(unconfirmed())}>Cancel</button>
          </div>
        )}
        <Textarea
          ref={(element) => { box = element }}
          value={text()}
          readOnly={Boolean(unconfirmed()) || send.isPending || archived()}
          aria-label="Message"
          placeholder={
            archived()
              ? `#${ui.activeChannel()} is archived and read-only`
              : nameOk() ? `Message #${ui.activeChannel()}` : "Set a display name before posting"
          }
          onInput={(event) => setText(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.isComposing || event.key === "Process") return
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault()
              if (canSend()) submit()
            }
          }}
        />
        <div class="flex items-center justify-between gap-3">
          <p class="text-xs text-cream-dim">Enter to send · Shift+Enter for a new line</p>
          <Button type="submit" disabled={!canSend()}>{send.isPending ? "Sending…" : unconfirmed() ? "Retry original send" : "Send"}</Button>
        </div>
        {error() && <p class="text-sm text-rust">{error()}</p>}
        {unconfirmed() && !send.isPending && <p class="text-sm text-cream-dim">Confirm the original send to #{unconfirmed()!.channel} before editing this draft.</p>}
      </div>
    </form>
  )
}

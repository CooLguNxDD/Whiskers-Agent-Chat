import { createEffect, createSignal, For, Show } from "solid-js"
import { isApiError } from "@/api/client"
import { CHANNEL_STATES, type Channel, type ChannelState } from "@/api/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Modal } from "@/components/ui/dialog"
import { useSetChannelState } from "@/hooks/useChannels"
import { usePrefs } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { isHandle } from "@/utils/handles"

const STATE_HELP: Record<ChannelState, string> = {
  active: "Normal chat.",
  paused: "Work is on hold.",
  blocked: "Waiting on something outside the fleet.",
  review: "Work is ready for a look.",
  done: "Finished; still writable.",
  archived: "Read-only. Open tasks must be finished or cancelled.",
}

/**
 * Channel settings: lifecycle state with an optional note. Only "archived"
 * changes behaviour (read-only). Archiving a channel with unfinished tasks
 * asks before cancelling them. `fleet` can't be archived.
 */
export default function ChannelSettings(props: { channel: Channel | undefined; open: boolean; onOpenChange: (open: boolean) => void }) {
  const prefs = usePrefs()
  const setState = useSetChannelState()
  const [choice, setChoice] = createSignal<ChannelState>("active")
  const [note, setNote] = createSignal("")
  const [error, setError] = createSignal("")
  const [pendingForce, setPendingForce] = createSignal<number | null>(null)

  createEffect(() => {
    if (!props.open) return
    setChoice(props.channel?.state ?? "active")
    setNote("")
    setError("")
    setPendingForce(null)
  })

  function save(force = false) {
    const channel = props.channel
    if (!channel) return
    const actor = prefs.displayName().trim()
    setError("")
    if (!isHandle(actor)) { setError("Set a display name first."); return }
    setState.mutate(
      { name: channel.name, state: choice(), actor, note: note().trim(), force },
      {
        onSuccess: () => { setPendingForce(null); props.onOpenChange(false) },
        onError: (err) => {
          if (isApiError(err) && err.code === "channel_has_open_tasks") {
            setPendingForce(Array.isArray(err.details?.task_ids) ? err.details.task_ids.length : 0)
            return
          }
          setPendingForce(null)
          setError(getErrorMessage(err, "Could not change the channel state."))
        },
      },
    )
  }

  return (
    <Modal open={props.open} onOpenChange={props.onOpenChange} title={`#${props.channel?.name ?? ""} settings`}>
      <form class="flex flex-col gap-4" onSubmit={(event) => { event.preventDefault(); save() }}>
        <Show when={props.channel?.topic}><p class="text-sm text-cream-dim">{props.channel?.topic}</p></Show>
        <fieldset>
          <legend class="mb-2 text-sm font-medium text-cream">State</legend>
          <div class="grid gap-1.5 sm:grid-cols-2">
            <For each={CHANNEL_STATES.filter((state) => state !== "archived" || props.channel?.name !== "fleet")}>
              {(state) => (
                <label class="state-option flex cursor-pointer items-start gap-2 rounded-lg border border-line px-3 py-2 text-sm" classList={{ "is-selected": choice() === state }}>
                  <input type="radio" name="channel-state" class="mt-1" value={state} checked={choice() === state} onChange={() => setChoice(state)} />
                  <span>
                    <span class="state-chip rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide" data-state={state}>{state}</span>
                    <span class="mt-1 block text-xs text-cream-dim">{STATE_HELP[state]}</span>
                  </span>
                </label>
              )}
            </For>
          </div>
        </fieldset>
        <label class="text-sm">Note <span class="text-cream-dim">(optional)</span>
          <Input class="mt-1" value={note()} maxLength={2000} placeholder="Why is it changing?" onInput={(event) => setNote(event.currentTarget.value)} />
        </label>
        <Show when={props.channel?.state_updated_by}>
          <p class="text-xs text-cream-dim">Last set to {props.channel?.state} by {props.channel?.state_updated_by}{props.channel?.state_note ? ` — ${props.channel.state_note}` : ""}</p>
        </Show>
        <Show when={error()}><p class="text-sm text-rust">{error()}</p></Show>
        <Show when={pendingForce() !== null}>
          <div class="rounded-lg border border-rust/40 bg-rust/10 p-3 text-sm text-cream">
            This channel has {pendingForce()} unfinished task(s). Archiving cancels them.
            <div class="mt-3 flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={() => setPendingForce(null)}>Keep channel open</Button>
              <Button type="button" disabled={setState.isPending} onClick={() => save(true)}>Cancel tasks and archive</Button>
            </div>
          </div>
        </Show>
        <div class="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={() => props.onOpenChange(false)}>Close</Button>
          <Button type="submit" disabled={setState.isPending || pendingForce() !== null || (choice() === props.channel?.state && !note().trim())}>
            {setState.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </Modal>
  )
}

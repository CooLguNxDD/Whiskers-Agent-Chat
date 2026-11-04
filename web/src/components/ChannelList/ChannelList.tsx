import { createSignal, Show } from "solid-js"
import { For } from "solid-js"
import { isApiError } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Modal } from "@/components/ui/dialog"
import { CHANNEL_STATES, type ChannelState } from "@/api/types"
import { useActiveChannel, useChannels, useCreateChannel, useSetChannelState } from "@/hooks/useChannels"
import { cn } from "@/lib/utils"
import { usePrefs, useUIStore } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { CHANNEL_RE, isHandle } from "@/utils/handles"

export type ChannelListProps = Record<string, never>

/**
 * Shared channels. A duplicate name offers a jump to the existing room.
 * Each row shows a non-active state. The open channel's state is set from a
 * picker, with an optional note. Archived channels are hidden unless "Show
 * archived" is on (the open one always shows). Choosing "archived" for a
 * channel with unfinished tasks asks before cancelling them.
 */
export default function ChannelList() {
  const channels = useChannels()
  const create = useCreateChannel()
  const setState = useSetChannelState()
  const active = useActiveChannel()
  const ui = useUIStore()
  const prefs = usePrefs()
  const [showArchived, setShowArchived] = createSignal(false)
  const [stateError, setStateError] = createSignal("")
  const [stateNote, setStateNote] = createSignal("")
  const [pendingForce, setPendingForce] = createSignal<{ name: string; count: number } | null>(null)
  let picker: HTMLSelectElement | undefined
  const [open, setOpen] = createSignal(false)
  const [name, setName] = createSignal("")
  const [topic, setTopic] = createSignal("")
  const [formError, setFormError] = createSignal("")
  const [existing, setExisting] = createSignal<string | null>(null)

  function select(next: string) {
    ui.setActiveChannel(next)
    ui.requestComposerFocus()
    setOpen(false)
  }

  function close() {
    setOpen(false)
    setFormError("")
    setExisting(null)
  }

  const [filter, setFilter] = createSignal("")
  const visible = () =>
    (channels.data?.channels ?? []).filter(
      (channel) =>
        (channel.state !== "archived" || showArchived() || channel.name === ui.activeChannel()) &&
        (!filter().trim() || channel.name.includes(filter().trim().toLowerCase()) || channel.name === ui.activeChannel()),
    )

  // The picker is uncontrolled while a change is in flight; put it back on failure.
  function resetPicker() {
    if (picker) picker.value = active()?.state ?? "active"
  }

  function changeState(state: ChannelState, force = false) {
    const channel = active()
    if (!channel) return
    const actor = prefs.displayName().trim()
    setStateError("")
    if (!isHandle(actor)) {
      setStateError("Set a display name first.")
      resetPicker()
      return
    }
    setState.mutate(
      { name: channel.name, state, actor, note: stateNote().trim(), force },
      {
        onSuccess: () => {
          setPendingForce(null)
          setStateNote("")
        },
        onError: (err) => {
          resetPicker()
          if (isApiError(err) && err.code === "channel_has_open_tasks") {
            const ids = Array.isArray(err.details?.task_ids) ? err.details.task_ids : []
            setPendingForce({ name: channel.name, count: ids.length })
            return
          }
          setPendingForce(null)
          setStateError(getErrorMessage(err, "Could not change the channel state."))
        },
      },
    )
  }

  return (
    <aside class="sidebar flex min-h-0 flex-col border-b border-line md:border-b-0 md:border-r">
      <div class="flex items-center justify-between px-4 pb-2 pt-4">
        <h2 class="text-[11px] font-semibold uppercase tracking-wider text-cream-dim">Channels</h2>
        <Button type="button" size="sm" variant="outline" class="h-7 rounded-full px-2.5" onClick={() => setOpen(true)} aria-label="New channel">＋ New</Button>
      </div>
      <div class="hidden px-3 pb-2 md:block">
        <Input
          class="h-8 rounded-full text-xs"
          placeholder="Filter channels"
          aria-label="Filter channels"
          value={filter()}
          onInput={(event) => setFilter(event.currentTarget.value)}
        />
      </div>
      <Show when={channels.isPending}><p class="px-3 text-sm text-cream-dim">Loading…</p></Show>
      <Show when={channels.isError}><p class="px-3 text-sm text-rust">{getErrorMessage(channels.error, "Could not load channels.")}</p></Show>
      <ul class="flex gap-1 overflow-x-auto px-2 pb-3 md:min-h-0 md:flex-1 md:flex-col md:overflow-y-auto md:overflow-x-visible">
        <For each={visible()}>
          {(channel) => (
            <li>
              <button
                type="button"
                class={cn(
                  "channel-row flex w-full min-w-0 items-center gap-2 whitespace-nowrap rounded-lg px-2.5 py-1.5 text-left text-sm transition",
                  channel.name === ui.activeChannel() ? "is-active font-medium text-cream" : "text-cream-dim hover:bg-ink-raised hover:text-cream",
                  channel.state === "archived" && "opacity-60",
                )}
                aria-current={channel.name === ui.activeChannel() ? "page" : undefined}
                title={channel.topic ? `${channel.name} — ${channel.topic}${channel.state_note ? ` (${channel.state_note})` : ""}` : channel.state_note || channel.name}
                onClick={() => select(channel.name)}
              >
                <span class="text-cream-dim" aria-hidden="true">#</span>
                <span class="min-w-0 flex-1 truncate">{channel.name}</span>
                <Show when={channel.state !== "active"}>
                  <span class="state-chip shrink-0 rounded-full px-1.5 text-[9px] font-semibold uppercase tracking-wide" data-state={channel.state}>{channel.state}</span>
                </Show>
              </button>
            </li>
          )}
        </For>
      </ul>
      <div class="mt-auto hidden flex-col gap-2 border-t md:flex border-line px-4 py-3 text-xs">
        <label class="flex items-center gap-2 text-cream-dim">
          <input type="checkbox" checked={showArchived()} onChange={(event) => setShowArchived(event.currentTarget.checked)} />
          Show archived
        </label>
        <Show when={active()}>
          {(channel) => (
            <div class="flex flex-col gap-1.5">
              <label class="flex items-center justify-between gap-2 text-cream-dim">
                State of #{channel().name}
                <select
                  ref={(element) => { picker = element }}
                  class="rounded-md border border-line bg-ink-raised px-1.5 py-1 text-cream"
                  value={channel().state}
                  disabled={setState.isPending}
                  onChange={(event) => changeState(event.currentTarget.value as ChannelState)}
                >
                  <For each={CHANNEL_STATES.filter((state) => state !== "archived" || channel().name !== "fleet")}>
                    {(state) => <option value={state}>{state}</option>}
                  </For>
                </select>
              </label>
              <Input
                class="h-7 text-xs"
                placeholder="Note for the next change (optional)"
                value={stateNote()}
                maxLength={2000}
                onInput={(event) => setStateNote(event.currentTarget.value)}
              />
            </div>
          )}
        </Show>
        <Show when={stateError()}><p class="text-rust">{stateError()}</p></Show>
      </div>
      <Modal
        open={pendingForce() !== null}
        title={`Archive #${pendingForce()?.name ?? ""}?`}
        onOpenChange={(next) => { if (!next) setPendingForce(null) }}
      >
        <p class="text-sm text-cream">
          This channel has {pendingForce()?.count ?? 0} unfinished task(s). Archiving cancels them.
        </p>
        <div class="mt-4 flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => setPendingForce(null)}>Keep channel open</Button>
          <Button type="button" disabled={setState.isPending} onClick={() => changeState("archived", true)}>Cancel tasks and archive</Button>
        </div>
      </Modal>
      <Modal open={open()} title="Create channel" onOpenChange={(next) => next ? setOpen(true) : close()}>
        <form
          class="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault()
            setFormError("")
            setExisting(null)
            if (!CHANNEL_RE.test(name())) {
              setFormError("Use a lowercase slug: letters, numbers, _ or -.")
              return
            }
            create.mutate(
              { name: name(), topic: topic() },
              {
                onSuccess: (result) => {
                  setName("")
                  setTopic("")
                  select(result.channel.name)
                },
                onError: (err) => {
                  if (isApiError(err) && err.code === "channel_exists") {
                    const existingName = String(err.details?.name ?? name())
                    setExisting(existingName)
                    setFormError(`#${existingName} already exists.`)
                    return
                  }
                  setFormError(getErrorMessage(err, "Could not create the channel."))
                },
              },
            )
          }}
        >
          <label class="text-sm">Name
            <Input class="mt-1" value={name()} onInput={(event) => setName(event.currentTarget.value.toLowerCase())} autofocus maxLength={64} />
          </label>
          <label class="text-sm">Topic
            <Input class="mt-1" value={topic()} onInput={(event) => setTopic(event.currentTarget.value)} maxLength={500} />
          </label>
          <Show when={formError()}><p class="text-sm text-rust">{formError()}</p></Show>
          <div class="flex justify-end gap-2">
            <Show when={existing()}>
              {(room) => <Button type="button" variant="outline" onClick={() => select(room())}>Open #{room()}</Button>}
            </Show>
            <Button type="submit" disabled={create.isPending}>Create</Button>
          </div>
        </form>
      </Modal>
    </aside>
  )
}

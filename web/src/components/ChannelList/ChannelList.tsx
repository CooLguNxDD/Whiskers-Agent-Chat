import { createSignal, Show } from "solid-js"
import { useNavigate } from "@solidjs/router"
import { For } from "solid-js"
import { isApiError } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Icon } from "@/components/ui/icons"
import { Input } from "@/components/ui/input"
import { Modal } from "@/components/ui/dialog"
import { useChannels, useCreateChannel } from "@/hooks/useChannels"
import { cn } from "@/lib/utils"
import { useUIStore } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { CHANNEL_RE } from "@/utils/handles"

export type ChannelListProps = Record<string, never>

/**
 * Sidebar channel navigation. A duplicate name offers a jump to the existing
 * room. Each row shows a non-active state. Archived channels are hidden unless
 * "Show archived" is on (the open one always shows). Channel state lives in
 * the room header's settings dialog (`ChannelSettings`), not here.
 */
export default function ChannelList() {
  const channels = useChannels()
  const create = useCreateChannel()
  const ui = useUIStore()
  const navigate = useNavigate()
  const [showArchived, setShowArchived] = createSignal(false)
  const [open, setOpen] = createSignal(false)
  const [name, setName] = createSignal("")
  const [topic, setTopic] = createSignal("")
  const [formError, setFormError] = createSignal("")
  const [existing, setExisting] = createSignal<string | null>(null)

  function select(next: string) {
    ui.setActiveChannel(next)
    navigate("/")
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

  return (
    <section class="flex min-h-0 flex-col" aria-label="Channels">
      <div class="flex items-center justify-between pb-1.5 pl-3">
        <h2 class="side-label">Channels</h2>
        <div class="flex items-center gap-0.5">
          <button
            type="button"
            class="icon-btn"
            aria-pressed={showArchived()}
            title={showArchived() ? "Hide archived channels" : "Show archived channels"}
            aria-label="Show archived channels"
            onClick={() => setShowArchived((value) => !value)}
          ><Icon name="archive" class="size-4" /></button>
          <button type="button" class="icon-btn" onClick={() => setOpen(true)} title="New channel" aria-label="New channel"><Icon name="plus" class="size-4" /></button>
        </div>
      </div>
      <Show when={(channels.data?.channels.length ?? 0) > 8}>
      <div class="px-2 pb-1.5">
        <Input
          class="h-8 rounded-full text-xs"
          placeholder="Filter channels"
          aria-label="Filter channels"
          value={filter()}
          onInput={(event) => setFilter(event.currentTarget.value)}
        />
      </div>
      </Show>
      <Show when={channels.isPending}><p class="px-3 text-sm text-cream-dim">Loading…</p></Show>
      <Show when={channels.isError}><p class="px-3 text-sm text-rust">{getErrorMessage(channels.error, "Could not load channels.")}</p></Show>
      <ul class="flex flex-col gap-0.5 pb-2">
        <For each={visible()}>
          {(channel) => (
            <li>
              <button
                type="button"
                class={cn(
                  "channel-row flex w-full min-w-0 items-center gap-2.5 whitespace-nowrap rounded-xl px-3 py-2 text-left text-[14.5px] transition",
                  channel.name === ui.activeChannel() ? "is-active font-medium text-cream" : "text-cream-dim hover:bg-ink-raised hover:text-cream",
                  channel.state === "archived" && "opacity-60",
                )}
                aria-current={channel.name === ui.activeChannel() ? "page" : undefined}
                title={channel.topic ? `${channel.name} — ${channel.topic}${channel.state_note ? ` (${channel.state_note})` : ""}` : channel.state_note || channel.name}
                onClick={() => select(channel.name)}
              >
                <span class="text-base text-cream-dim" aria-hidden="true">#</span>
                <span class="min-w-0 flex-1 truncate">{channel.name}</span>
                <Show when={channel.state !== "active"}>
                  <span class="state-chip shrink-0 rounded-full px-1.5 text-[9px] font-semibold uppercase tracking-wide" data-state={channel.state}>{channel.state}</span>
                </Show>
              </button>
            </li>
          )}
        </For>
      </ul>
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
    </section>
  )
}

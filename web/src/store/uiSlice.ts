import { batch, createSignal } from "solid-js"

export interface UISlice {
  activeChannel: () => string
  expandedTaskId: () => number | null
  focusNonce: () => number
  /** Handle whose inbox the portal shows; empty means the display name. Memory only. */
  inboxHandle: () => string
  /** Message the chat should scroll to and mark, e.g. after opening a search hit. */
  focusMessageId: () => number | null
  setActiveChannel: (name: string) => void
  setExpandedTaskId: (id: number | null) => void
  requestComposerFocus: () => void
  setInboxHandle: (handle: string) => void
  focusMessage: (channel: string, id: number | null) => void
}

const [activeChannel, setActiveChannelSignal] = createSignal("fleet")
const [expandedTaskId, setExpandedTaskId] = createSignal<number | null>(null)
const [focusNonce, setFocusNonce] = createSignal(0)
const [inboxHandle, setInboxHandle] = createSignal("")
const [focusMessageId, setFocusMessageId] = createSignal<number | null>(null, { equals: false })

export const ui: UISlice = {
  activeChannel,
  expandedTaskId,
  focusNonce,
  inboxHandle,
  focusMessageId,
  setInboxHandle,
  setActiveChannel(name) {
    setActiveChannelSignal(name)
    setExpandedTaskId(null)
    setFocusMessageId(null)
  },
  focusMessage(channel, id) {
    batch(() => {
      ui.setActiveChannel(channel)
      setFocusMessageId(id)
    })
  },
  setExpandedTaskId,
  requestComposerFocus() {
    setFocusNonce((value) => value + 1)
  },
}

export function useUIStore() {
  return ui
}

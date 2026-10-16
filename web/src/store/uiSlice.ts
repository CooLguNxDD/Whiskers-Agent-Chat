import { createSignal } from "solid-js"

export interface UISlice {
  activeChannel: () => string
  expandedTaskId: () => number | null
  focusNonce: () => number
  /** Handle whose inbox the portal shows; empty means the display name. Memory only. */
  inboxHandle: () => string
  setActiveChannel: (name: string) => void
  setExpandedTaskId: (id: number | null) => void
  requestComposerFocus: () => void
  setInboxHandle: (handle: string) => void
}

const [activeChannel, setActiveChannelSignal] = createSignal("fleet")
const [expandedTaskId, setExpandedTaskId] = createSignal<number | null>(null)
const [focusNonce, setFocusNonce] = createSignal(0)
const [inboxHandle, setInboxHandle] = createSignal("")

export const ui: UISlice = {
  activeChannel,
  expandedTaskId,
  focusNonce,
  inboxHandle,
  setInboxHandle,
  setActiveChannel(name) {
    setActiveChannelSignal(name)
    setExpandedTaskId(null)
  },
  setExpandedTaskId,
  requestComposerFocus() {
    setFocusNonce((value) => value + 1)
  },
}

export function useUIStore() {
  return ui
}

import { createSignal } from "solid-js"

export interface UISlice {
  activeChannel: () => string
  expandedTaskId: () => number | null
  focusNonce: () => number
  setActiveChannel: (name: string) => void
  setExpandedTaskId: (id: number | null) => void
  requestComposerFocus: () => void
}

const [activeChannel, setActiveChannelSignal] = createSignal("fleet")
const [expandedTaskId, setExpandedTaskId] = createSignal<number | null>(null)
const [focusNonce, setFocusNonce] = createSignal(0)

export const ui: UISlice = {
  activeChannel,
  expandedTaskId,
  focusNonce,
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

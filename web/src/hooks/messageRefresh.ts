type RefreshListener = (reset: boolean, channel?: string) => void
const listeners = new Set<RefreshListener>()

export function registerMessageWindow(listener: RefreshListener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function refreshMessageWindows(reset = false, channel?: string) {
  for (const listener of listeners) listener(reset, channel)
}

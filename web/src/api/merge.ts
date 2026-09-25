import type { FleetMessage } from "./types"

/** History plus anything newer than the snapshot. Same id never appears twice. */
export function mergeMessages(history: FleetMessage[], live: FleetMessage[]): FleetMessage[] {
  const watermark = history.reduce((max, message) => Math.max(max, message.id), 0)
  const byId = new Map<number, FleetMessage>()
  for (const message of history) byId.set(message.id, message)
  for (const message of live) {
    if (message.id > watermark || !byId.has(message.id)) byId.set(message.id, message)
  }
  return [...byId.values()].sort((a, b) => a.id - b.id)
}

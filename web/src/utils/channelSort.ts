import type { Channel } from "@/api/types"
import type { ChannelSort } from "@/store/prefsSlice"

function activity(channel: Channel): number {
  const at = Date.parse(channel.last_message_at ?? channel.created_at)
  return Number.isNaN(at) ? 0 : at
}

/**
 * Orders sidebar channels. `latest` puts the most recent message first (a channel with no
 * messages counts from its creation); ties and `name` fall back to alphabetical.
 */
export function sortChannels(channels: readonly Channel[], mode: ChannelSort): Channel[] {
  const byName = (a: Channel, b: Channel) => a.name.localeCompare(b.name)
  if (mode === "name") return [...channels].sort(byName)
  return [...channels].sort((a, b) => activity(b) - activity(a) || byName(a, b))
}

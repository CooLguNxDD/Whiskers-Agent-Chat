import { useFleetStream } from "@/hooks/useFleetStream"
import { cn } from "@/lib/utils"

export type ConnectionPillProps = Record<string, never>

const LABEL = {
  connecting: "Connecting",
  connected: "Live",
  disconnected: "Reconnecting",
  unauthorized: "Needs token",
} as const

export default function ConnectionPill() {
  const stream = useFleetStream()
  return (
    <span
      class={cn(
        "inline-flex items-center gap-2 rounded-full border border-line px-2.5 py-1 text-xs",
        stream.status() === "connected" && "text-moss",
        stream.status() === "disconnected" && "text-brass",
        stream.status() === "unauthorized" && "text-rust",
        stream.status() === "connecting" && "text-cream-dim",
      )}
      title={stream.reconnects() ? `reconnects observed: ${stream.reconnects()}` : undefined}
    >
      <span
        class={cn(
          "size-2 rounded-full",
          stream.status() === "connected" && "bg-moss",
          stream.status() === "disconnected" && "bg-brass",
          stream.status() === "unauthorized" && "bg-rust",
          stream.status() === "connecting" && "bg-cream-dim",
        )}
      />
      {LABEL[stream.status()]}
      {stream.status() === "disconnected" && stream.attempt() > 0 ? ` · try ${stream.attempt()}` : ""}
    </span>
  )
}

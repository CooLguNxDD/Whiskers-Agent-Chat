import { For, Show } from "solid-js"
import type { FleetMessage } from "@/api/types"
import { Badge } from "@/components/ui/badge"
import { formatStamp } from "@/utils/time"

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

export interface MessageRowProps {
  message: FleetMessage
  onReply: (message: FleetMessage) => void
}

/**
 * Plain text only. Mentions are split into elements, never HTML.
 * Attachments render as chips. The hub has no object-store access, so there is
 * no download link; agents fetch bytes with the Whiskers `fleet_get_attachment` tool.
 */
export default function MessageRow(props: MessageRowProps) {
  const parts = () => props.message.text.split(/(@[A-Za-z0-9][A-Za-z0-9_-]{0,63})/g)
  return (
    <article class="group px-4 py-3">
      <header class="flex items-baseline gap-2">
        <span class="font-medium text-cream">{props.message.author}</span>
        <time class="font-mono text-xs text-cream-dim" dateTime={props.message.created_at} title={props.message.created_at}>
          {formatStamp(props.message.created_at)}
        </time>
        <button
          type="button"
          class="ml-auto text-xs text-cream-dim opacity-0 transition group-hover:opacity-100 focus-visible:opacity-100"
          onClick={() => props.onReply(props.message)}
        >
          Reply
        </button>
      </header>
      {props.message.reply_to != null && <p class="mt-1 text-xs text-cream-dim">reply to #{props.message.reply_to}</p>}
      <Show when={props.message.origin || props.message.destination}>
        <p class="mt-1 flex flex-wrap gap-2">
          <Show when={props.message.origin}>
            {(origin) => (
              <Badge title={`Discord channel ${origin().channel_id}`}>
                via Discord{origin().author ? ` · ${origin().author}` : ""}
              </Badge>
            )}
          </Show>
          <Show when={props.message.destination}>
            {(destination) => <Badge class="text-brass">→ {destination()}</Badge>}
          </Show>
        </p>
      </Show>
      <p class="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed">
        {parts().map((part) =>
          part.startsWith("@") && props.message.mentions.includes(part.slice(1)) ? (
            <span class="text-brass">{part}</span>
          ) : (
            <span>{part}</span>
          ),
        )}
      </p>
      <Show when={props.message.attachments?.length}>
        <ul class="mt-2 flex flex-wrap gap-2" aria-label="Attachments">
          <For each={props.message.attachments}>
            {(file) => (
              <li
                class="rounded-md border border-line bg-ink-sunken px-2 py-1 font-mono text-xs text-cream"
                title={`${file.bucket}/${file.object_key}`}
              >
                📎 {file.filename}
                <span class="ml-2 text-cream-dim">{formatBytes(file.size_bytes)} · {file.content_type} · #{file.id}</span>
              </li>
            )}
          </For>
        </ul>
      </Show>
    </article>
  )
}

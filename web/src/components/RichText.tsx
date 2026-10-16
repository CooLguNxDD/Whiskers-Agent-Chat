import { For } from "solid-js"
import { bodySegments, snippetSegments } from "@/utils/richText"

/** A search snippet with its matches in <mark>. Text nodes only, never HTML. */
export function Snippet(props: { text: string }) {
  return (
    <For each={snippetSegments(props.text)}>
      {(segment) => (segment.match ? <mark class="rounded bg-brass/25 px-0.5 text-cream">{segment.text}</mark> : segment.text)}
    </For>
  )
}

/** A feed body with clickable hashtags. Text nodes only, never HTML. */
export function FeedBody(props: { body: string; onTag?: (tag: string) => void }) {
  return (
    <span class="whitespace-pre-wrap break-words">
      <For each={bodySegments(props.body)}>
        {(segment) =>
          segment.kind === "tag" ? (
            <button type="button" class="text-brass hover:underline" onClick={() => props.onTag?.(segment.tag)}>{segment.text}</button>
          ) : (
            segment.text
          )
        }
      </For>
    </span>
  )
}

export const selectClass =
  "h-9 rounded-lg border border-line bg-ink-sunken px-3 text-sm text-cream focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brass"

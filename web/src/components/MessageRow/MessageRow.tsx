import { createSignal, For, Match, Show, Switch } from "solid-js"
import type { FleetMessage } from "@/api/types"
import { avatarHue, blockSegments, clockTime, initials, inlineSegments, isLong, looksLikeAgent } from "@/utils/chatLayout"
import { formatStamp } from "@/utils/time"

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

export interface MessageRowProps {
  message: FleetMessage
  onReply: (message: FleetMessage) => void
  /** First message of a sender group: shows avatar, name and full time. */
  first?: boolean
  /** Last message of a sender group: rounds the bubble tail and adds spacing. */
  last?: boolean
  /** Posted by the reader's display name: right-aligned accent bubble. */
  mine?: boolean
  /** The parent message, when it is in the loaded window. */
  parent?: FleetMessage
  onJump?: (id: number) => void
  /** In-room find text to highlight. */
  query?: string
  /** This row is the current find hit or an opened search hit. */
  marked?: boolean
}

/** Initials avatar with a stable per-author hue. Decorative; the name is always shown as text too. */
export function Avatar(props: { name: string; size?: "sm" | "md" }) {
  return (
    <span
      aria-hidden="true"
      class="avatar inline-flex shrink-0 select-none items-center justify-center rounded-xl font-semibold"
      classList={{ "size-9 text-xs": props.size !== "sm", "size-7 text-[10px]": props.size === "sm" }}
      style={{ "--avatar-hue": String(avatarHue(props.name)) }}
    >
      {initials(props.name)}
    </span>
  )
}

/** Message text: fenced code blocks, inline code, links, mentions and find-highlights. Text nodes only. */
export function MessageBody(props: { text: string; mentions: string[]; query?: string }) {
  return (
    <For each={blockSegments(props.text)}>
      {(block) =>
        block.kind === "codeblock" ? (
          <pre class="code-block my-1.5 overflow-x-auto rounded-lg px-3 py-2 font-mono text-xs leading-relaxed" data-lang={block.lang || undefined}><code>{block.text}</code></pre>
        ) : (
          <span class="whitespace-pre-wrap break-words [overflow-wrap:anywhere]">
            <For each={inlineSegments(block.text, props.mentions, props.query)}>
              {(segment) => (
                <Switch fallback={segment.text}>
                  <Match when={segment.kind === "mention"}><span class="mention rounded px-0.5 font-medium">{segment.text}</span></Match>
                  <Match when={segment.kind === "code"}><code class="inline-code rounded px-1 py-px font-mono text-[0.85em]">{segment.text}</code></Match>
                  <Match when={segment.kind === "link" && segment}>
                    {(link) => <a class="text-brass underline decoration-brass/40 underline-offset-2 hover:decoration-brass" href={(link() as { href: string }).href} target="_blank" rel="noopener noreferrer nofollow">{link().text}</a>}
                  </Match>
                  <Match when={segment.kind === "match"}><mark class="find-hit rounded px-0.5">{segment.text}</mark></Match>
                </Switch>
              )}
            </For>
          </span>
        )
      }
    </For>
  )
}

/**
 * One message as a chat bubble. Grouped rows from the same sender drop the
 * avatar and header. Long messages start collapsed. Attachments are metadata
 * cards with no download link; agents fetch bytes with `fleet_get_attachment`.
 */
export default function MessageRow(props: MessageRowProps) {
  const first = () => props.first ?? true
  const last = () => props.last ?? true
  const long = () => isLong(props.message.text)
  const [expanded, setExpanded] = createSignal(false)
  const [copied, setCopied] = createSignal(false)
  const collapsed = () => long() && !expanded() && !props.query

  function copy() {
    void navigator.clipboard?.writeText(props.message.text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    }).catch(() => undefined)
  }

  return (
    <article
      class="msg group relative flex gap-2.5 px-3 sm:px-5"
      classList={{ "flex-row-reverse": props.mine, "pt-3": first(), "pt-0.5": !first(), "pb-1": last(), "is-marked": props.marked }}
      data-message-id={props.message.id}
      aria-label={`${props.message.author} at ${formatStamp(props.message.created_at)}`}
    >
      <div class="w-9 shrink-0">
        <Show when={first()}><Avatar name={props.message.author} /></Show>
      </div>
      <div class="flex min-w-0 max-w-[min(46rem,100%-3rem)] flex-col" classList={{ "items-end": props.mine }}>
        <Show when={first()}>
          <header class="mb-1 flex items-baseline gap-2 px-1" classList={{ "flex-row-reverse": props.mine }}>
            <span class="author-name text-sm font-semibold" style={{ "--avatar-hue": String(avatarHue(props.message.author)) }}>{props.message.author}</span>
            <Show when={looksLikeAgent(props.message.author)}>
              <span class="rounded-full border border-line px-1.5 text-[10px] uppercase tracking-wide text-cream-dim">agent</span>
            </Show>
            <time class="font-mono text-[11px] text-cream-dim" dateTime={props.message.created_at} title={props.message.created_at}>
              {formatStamp(props.message.created_at)}
            </time>
          </header>
        </Show>
        <div
          class="bubble relative min-w-0 rounded-2xl px-3.5 py-2 text-sm leading-relaxed shadow-sm"
          classList={{
            "bubble-mine": props.mine,
            "bubble-other": !props.mine,
            "rounded-tl-md": !props.mine && !first(),
            "rounded-bl-md": !props.mine && !last(),
            "rounded-tr-md": props.mine && !first(),
            "rounded-br-md": props.mine && !last(),
          }}
        >
          <Show when={props.message.reply_to != null}>
            <button
              type="button"
              class="reply-quote mb-1.5 flex w-full min-w-0 flex-col rounded-lg border-l-2 px-2 py-1 text-left text-xs"
              onClick={() => props.onJump?.(props.message.reply_to!)}
              title="Jump to the original message"
            >
              <Show when={props.parent} fallback={<span class="text-cream-dim">↩ reply to #{props.message.reply_to}</span>}>
                {(parent) => (
                  <>
                    <span class="font-semibold">↩ {parent().author}</span>
                    <span class="truncate text-cream-dim">{parent().text.replace(/```[\w+-]*/g, " ").replace(/\s+/g, " ").slice(0, 160)}</span>
                  </>
                )}
              </Show>
            </button>
          </Show>
          <Show when={props.message.origin || props.message.destination}>
            <p class="mb-1 flex flex-wrap gap-1.5 text-[11px]">
              <Show when={props.message.origin}>
                {(origin) => (
                  <span class="chip" title={`${origin().source ?? "discord"} channel ${origin().channel_id}`}>
                    via {origin().source ?? "Discord"}{origin().author ? ` · ${origin().author}` : ""}
                  </span>
                )}
              </Show>
              <Show when={props.message.destination}>
                {(destination) => <span class="chip text-brass">→ {destination()}</span>}
              </Show>
            </p>
          </Show>
          <div class="relative" classList={{ "collapsed-body": collapsed() }}>
            <MessageBody text={props.message.text} mentions={props.message.mentions} query={props.query} />
          </div>
          <Show when={long() && !props.query}>
            <button type="button" class="mt-1 text-xs font-medium text-brass hover:underline" aria-expanded={expanded()} onClick={() => setExpanded((value) => !value)}>
              {expanded() ? "Show less" : "Show more"}
            </button>
          </Show>
          <Show when={props.message.attachments?.length}>
            <ul class="mt-2 grid gap-1.5" aria-label="Attachments">
              <For each={props.message.attachments}>
                {(file) => (
                  <li class="attachment-card flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs" title={`${file.bucket}/${file.object_key}`}>
                    <span aria-hidden="true" class="text-base">📎</span>
                    <span class="min-w-0 flex-1">
                      <span class="block truncate font-medium">{file.filename}</span>
                      <span class="block truncate font-mono text-[10px] text-cream-dim">{formatBytes(file.size_bytes)} · {file.content_type} · #{file.id}</span>
                    </span>
                  </li>
                )}
              </For>
            </ul>
          </Show>
        </div>
        <Show when={!first()}>
          <time class="sr-only" dateTime={props.message.created_at}>{formatStamp(props.message.created_at)}</time>
        </Show>
      </div>
      <div
        class="msg-actions absolute -top-3 z-10 flex items-center gap-0.5 rounded-lg border border-line bg-ink-raised p-0.5 opacity-0 shadow-lg transition group-hover:opacity-100 focus-within:opacity-100"
        classList={{ "right-5": !props.mine, "left-5": props.mine }}
      >
        <Show when={!first()}>
          <span class="px-1.5 font-mono text-[10px] text-cream-dim" title={props.message.created_at}>{clockTime(props.message.created_at)}</span>
        </Show>
        <button type="button" class="rounded-md px-2 py-1 text-xs text-cream hover:bg-ink-sunken" onClick={() => props.onReply(props.message)}>Reply</button>
        <button type="button" class="rounded-md px-2 py-1 text-xs text-cream hover:bg-ink-sunken" onClick={copy}>{copied() ? "Copied" : "Copy"}</button>
        <span class="px-1.5 font-mono text-[10px] text-cream-dim">#{props.message.id}</span>
      </div>
    </article>
  )
}

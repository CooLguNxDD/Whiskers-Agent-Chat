import { createSignal, For, Match, Show, Switch } from "solid-js"
import { useNavigate } from "@solidjs/router"
import type { FleetMessage } from "@/api/types"
import { useUIStore } from "@/store"
import {
  avatarHue, clockTime, inlineSegments, isLong, LOG_FOLD_LINES, looksLikeAgent, messageActions, richBlocks,
  type InlineSegment,
} from "@/utils/chatLayout"
import { formatStamp } from "@/utils/time"
import { Avatar } from "@/components/ui/avatar"

export { Avatar }

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

export interface MessageRowProps {
  message: FleetMessage
  onReply: (message: FleetMessage) => void
  /** First message of a sender group: shows avatar, name and time. */
  first?: boolean
  /** Last message of a sender group: adds spacing below. */
  last?: boolean
  /** Posted by the reader's display name. */
  mine?: boolean
  /** The parent message, when it is in the loaded window. */
  parent?: FleetMessage
  onJump?: (id: number) => void
  /** In-room find text to highlight. */
  query?: string
  /** This row is the current find hit or an opened search hit. */
  marked?: boolean
}

function Inline(props: { segments: InlineSegment[] }) {
  return (
    <For each={props.segments}>
      {(segment) => (
        <Switch fallback={segment.text}>
          <Match when={segment.kind === "mention"}><span class="mention rounded px-0.5 font-medium">{segment.text}</span></Match>
          <Match when={segment.kind === "code"}><code class="inline-code rounded px-1 py-px font-mono text-[0.85em]">{segment.text}</code></Match>
          <Match when={segment.kind === "status" && segment}>
            {(status) => <span class="status-chip" data-tone={(status() as { tone: string }).tone}>{status().text}</span>}
          </Match>
          <Match when={segment.kind === "link" && segment}>
            {(link) => <a class="text-brass underline decoration-brass/40 underline-offset-2 hover:decoration-brass" href={(link() as { href: string }).href} target="_blank" rel="noopener noreferrer nofollow">{link().text}</a>}
          </Match>
          <Match when={segment.kind === "match"}><mark class="find-hit rounded px-0.5">{segment.text}</mark></Match>
        </Switch>
      )}
    </For>
  )
}

/** A code block; long output folds into an accordion. */
function CodeBlock(props: { text: string; lang: string; open?: boolean }) {
  const lines = () => props.text.split("\n").length
  const pre = () => (
    <pre class="code-block overflow-x-auto rounded-lg px-3 py-2 font-mono text-xs leading-relaxed" data-lang={props.lang || undefined}><code>{props.text}</code></pre>
  )
  return (
    <Show when={lines() >= LOG_FOLD_LINES} fallback={<div class="my-1.5">{pre()}</div>}>
      <details class="log-fold my-1.5 rounded-lg" open={props.open}>
        <summary class="flex cursor-pointer select-none items-center gap-2 rounded-lg px-3 py-1.5 text-xs font-medium">
          <span class="log-caret" aria-hidden="true">▸</span>
          {props.lang ? `${props.lang} output` : "Output"} · {lines()} lines
        </summary>
        {pre()}
      </details>
    </Show>
  )
}

/** Message text: fact cards, code, links, mentions, status chips and find-highlights. Text nodes only. */
export function MessageBody(props: { text: string; mentions: string[]; query?: string }) {
  return (
    <For each={richBlocks(props.text)}>
      {(block) => (
        <Switch>
          <Match when={block.kind === "codeblock" && block}>
            {(code) => <CodeBlock text={(code() as { text: string }).text} lang={(code() as { lang: string }).lang} open={Boolean(props.query)} />}
          </Match>
          <Match when={block.kind === "facts" && block}>
            {(facts) => (
              <dl class="facts-card my-1.5 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 rounded-lg px-3 py-2 text-[13px]">
                <For each={(facts() as { rows: { key: string; value: string }[] }).rows}>
                  {(row) => (
                    <>
                      <dt class="text-cream-dim">{row.key}</dt>
                      <dd class="min-w-0 break-words font-medium"><Inline segments={inlineSegments(row.value, props.mentions, props.query)} /></dd>
                    </>
                  )}
                </For>
              </dl>
            )}
          </Match>
          <Match when={block.kind === "para" && block}>
            {(para) => (
              <p class="whitespace-pre-wrap break-words [overflow-wrap:anywhere]">
                <Inline segments={inlineSegments((para() as { text: string }).text, props.mentions, props.query)} />
              </p>
            )}
          </Match>
        </Switch>
      )}
    </For>
  )
}

/**
 * One message. Every message is left-aligned in a single multi-party timeline.
 * Human messages sit in a tinted card with an accent rail; agent messages are
 * plain rows with structured output. Long messages start collapsed. Attachments
 * are metadata cards with no download link; agents fetch bytes with `fleet_get_attachment`.
 */
export default function MessageRow(props: MessageRowProps) {
  const ui = useUIStore()
  const navigate = useNavigate()
  const first = () => props.first ?? true
  const last = () => props.last ?? true
  const agent = () => looksLikeAgent(props.message.author)
  const long = () => isLong(props.message.text)
  const actions = () => messageActions(props.message.text)
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
      class="msg group relative flex gap-3 px-4 sm:px-6"
      classList={{ "pt-3": first(), "pt-0.5": !first(), "pb-1.5": last(), "is-marked": props.marked, "is-human": !agent(), "is-mine": props.mine }}
      data-message-id={props.message.id}
      aria-label={`${props.message.author} at ${formatStamp(props.message.created_at)}`}
    >
      <div class="w-9 shrink-0 pt-0.5">
        <Show when={first()} fallback={
          <time class="msg-gutter-time block pt-1 text-right font-mono text-[11px] text-cream-dim opacity-0 group-hover:opacity-100" dateTime={props.message.created_at} title={props.message.created_at}>
            {clockTime(props.message.created_at)}
          </time>
        }>
          <Avatar name={props.message.author} />
        </Show>
      </div>
      <div class="msg-body min-w-0 max-w-[52rem] flex-1">
        <div classList={{ "human-card rounded-xl px-4 py-2.5": !agent(), "agent-row": agent() }}>
          <Show when={first()}>
            <header class="mb-0.5 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <span class="author-name text-sm font-semibold" style={{ "--avatar-hue": String(avatarHue(props.message.author)) }}>{props.message.author}</span>
              <Show when={agent()} fallback={<Show when={props.mine}><span class="role-tag">you</span></Show>}>
                <span class="role-tag">agent</span>
              </Show>
              <time class="font-mono text-xs text-cream-dim" dateTime={props.message.created_at} title={props.message.created_at}>
                {formatStamp(props.message.created_at)}
              </time>
            </header>
          </Show>
          <Show when={props.message.reply_to != null}>
            <button
              type="button"
              class="reply-quote my-1 flex w-full min-w-0 max-w-xl flex-col rounded-lg border-l-2 px-2.5 py-1 text-left text-xs"
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
            <p class="my-1 flex flex-wrap gap-1.5 text-xs">
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
          <div class="msg-text relative text-[14.5px] leading-relaxed" classList={{ "collapsed-body": collapsed() }}>
            <MessageBody text={props.message.text} mentions={props.message.mentions} query={props.query} />
          </div>
          <Show when={long() && !props.query}>
            <button type="button" class="mt-1 text-xs font-medium text-brass hover:underline" aria-expanded={expanded()} onClick={() => setExpanded((value) => !value)}>
              {expanded() ? "Show less" : "Show more"}
            </button>
          </Show>
          <Show when={props.message.attachments?.length}>
            <ul class="mt-2 grid max-w-md gap-1.5" aria-label="Attachments">
              <For each={props.message.attachments}>
                {(file) => (
                  <li class="attachment-card flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs" title={`${file.bucket}/${file.object_key}`}>
                    <span aria-hidden="true" class="text-base">📎</span>
                    <span class="min-w-0 flex-1">
                      <span class="block truncate font-medium">{file.filename}</span>
                      <span class="block truncate font-mono text-[11px] text-cream-dim">{formatBytes(file.size_bytes)} · {file.content_type} · #{file.id}</span>
                    </span>
                  </li>
                )}
              </For>
            </ul>
          </Show>
          <Show when={actions().length}>
            <div class="mt-2 flex flex-wrap gap-1.5">
              <For each={actions()}>
                {(action) => (
                  <button
                    type="button"
                    class="action-btn rounded-lg px-2.5 py-1 text-xs font-medium"
                    onClick={() => {
                      if (action.kind === "task") { ui.setExpandedTaskId(action.id); navigate("/tasks") }
                      else navigate("/proposals")
                    }}
                  >
                    {action.kind === "task" ? `Open task #${action.id} →` : `Open proposal P${action.id} →`}
                  </button>
                )}
              </For>
            </div>
          </Show>
        </div>
      </div>
      <div class="msg-actions absolute -top-3 right-6 z-10 flex items-center gap-0.5 rounded-lg border border-line bg-ink-raised p-0.5 opacity-0 shadow-lg transition group-hover:opacity-100 focus-within:opacity-100">
        <button type="button" class="rounded-md px-2 py-1 text-xs text-cream hover:bg-ink-sunken" onClick={() => props.onReply(props.message)}>Reply</button>
        <button type="button" class="rounded-md px-2 py-1 text-xs text-cream hover:bg-ink-sunken" onClick={copy}>{copied() ? "Copied" : "Copy"}</button>
        <span class="px-1.5 font-mono text-[11px] text-cream-dim">#{props.message.id}</span>
      </div>
    </article>
  )
}

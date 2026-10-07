import { createSignal, For, Show } from "solid-js"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { useChannels } from "@/hooks/useChannels"
import { useWebhookMutations, useWebhooks } from "@/hooks/useWebhooks"
import { usePrefs } from "@/store"
import { EVENT_KINDS, type Webhook, type WebhookDirection, type WebhookFormat } from "@/api/types"
import { getErrorMessage } from "@/utils/errors"
import { CHANNEL_RE, isHandle } from "@/utils/handles"
import { formatStamp } from "@/utils/time"

const selectClass =
  "h-9 w-full rounded-lg border border-line bg-ink-sunken px-3 text-sm text-cream focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brass"

const KIND_PRESETS: { label: string; kinds: readonly string[] }[] = [
  { label: "Everything", kinds: [] },
  { label: "Messages", kinds: ["message.created"] },
  { label: "Tasks & channels", kinds: EVENT_KINDS.filter((kind) => kind !== "message.created") },
]

/** What the one-time secret screen needs. Held in memory only; closing the modal drops it. */
interface SecretNotice {
  name: string
  direction: WebhookDirection
  secret: string
}

type Mutations = ReturnType<typeof useWebhookMutations>

async function copyText(text: string, note: (message: string) => void) {
  try {
    await navigator.clipboard.writeText(text)
    note("Copied")
  } catch {
    note("Copy failed. Select the text and copy it by hand.")
  }
}

function inboundUrl(name: string) {
  return `${window.location.origin}/hooks/in/${name}`
}

/**
 * The Webhooks tab: outbound and inbound hooks. The list is on the left and the
 * setup form on the right (stacked on narrow screens), so a new hook shows up
 * in the list as soon as it is created.
 */
export default function Webhooks() {
  const hooks = useWebhooks(() => true)
  const mutations = useWebhookMutations()
  const [notice, setNotice] = createSignal<SecretNotice | null>(null)

  return (
    <div class="min-h-0 flex-1 overflow-y-auto p-4">
      <div class="mx-auto max-w-5xl">
        <h1 class="font-display text-xl text-cream">Webhooks</h1>
        <p class="mb-4 mt-1 text-sm text-cream-dim">
          Outbound hooks push hub activity to Discord, Slack or any URL so you can follow the fleet. Inbound hooks let other apps post into a channel, which is how you reach an agent from outside.
        </p>
        <Show when={notice()}>{(current) => <SecretScreen notice={current()} onDismiss={() => setNotice(null)} />}</Show>
        <div class="grid gap-6 lg:grid-cols-2">
          <section aria-labelledby="hooks-heading">
            <h2 id="hooks-heading" class="mb-3 font-display text-lg text-cream">Your webhooks</h2>
            <HookList hooks={hooks.data?.webhooks} error={hooks.error} mutations={mutations} onSecret={setNotice} />
          </section>
          <section aria-labelledby="new-hook-heading" class="rounded-lg border border-line p-4">
            <h2 id="new-hook-heading" class="mb-3 font-display text-lg text-cream">Set up a webhook</h2>
            <NewHookForm mutations={mutations} onCreated={setNotice} />
          </section>
        </div>
      </div>
    </div>
  )
}

function SecretScreen(props: { notice: SecretNotice; onDismiss: () => void }) {
  const [note, setNote] = createSignal("")
  const curl = () =>
    `curl -X POST ${inboundUrl(props.notice.name)} \\\n  -H "Authorization: Bearer ${props.notice.secret}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"text":"@agent hello from outside"}'`
  return (
    <div class="mb-4 rounded-lg border border-brass p-3" role="status">
      <p class="text-sm text-cream">
        Secret for <strong>{props.notice.name}</strong>. It is shown once and cannot be read again.
      </p>
      <code class="mt-2 block break-all rounded bg-ink-sunken p-2 text-xs text-cream">{props.notice.secret}</code>
      <Show when={props.notice.direction === "in"}>
        <p class="mt-3 text-xs text-cream-dim">Send a POST with this secret as a bearer token, or sign the body with HMAC-SHA256 in <code>X-CatFleet-Signature</code>.</p>
        <pre class="mt-1 overflow-x-auto rounded bg-ink-sunken p-2 text-xs text-cream">{curl()}</pre>
      </Show>
      <Show when={props.notice.direction === "out"}>
        <p class="mt-3 text-xs text-cream-dim">
          Generic receivers can verify the <code>X-CatFleet-Signature</code> header: HMAC-SHA256 of the raw body with this secret.
        </p>
      </Show>
      <div class="mt-3 flex flex-wrap items-center gap-2">
        <Button type="button" size="sm" onClick={() => void copyText(props.notice.secret, setNote)}>Copy secret</Button>
        <Show when={props.notice.direction === "in"}>
          <Button type="button" size="sm" variant="outline" onClick={() => void copyText(curl(), setNote)}>Copy curl</Button>
        </Show>
        <Button type="button" size="sm" variant="ghost" onClick={props.onDismiss}>Done</Button>
        <span class="text-xs text-cream-dim" aria-live="polite">{note()}</span>
      </div>
    </div>
  )
}

function HookList(props: {
  hooks: Webhook[] | undefined
  error: unknown
  mutations: Mutations
  onSecret: (notice: SecretNotice) => void
}) {
  return (
    <Show when={!props.error} fallback={<p class="text-sm text-rust">{getErrorMessage(props.error)}</p>}>
      <Show when={props.hooks} fallback={<p class="text-sm text-cream-dim">Loading…</p>}>
        {(hooks) => (
          <Show when={hooks().length > 0} fallback={<p class="text-sm text-cream-dim">No webhooks yet. Create one with “New webhook”.</p>}>
            <ul class="flex flex-col gap-3">
              <For each={hooks()}>{(hook) => <HookRow hook={hook} mutations={props.mutations} onSecret={props.onSecret} />}</For>
            </ul>
          </Show>
        )}
      </Show>
    </Show>
  )
}

function summarize(hook: Webhook): string {
  if (hook.direction === "in") {
    return `posts to #${hook.channel} as ${hook.author}${hook.allow_override ? " (caller may override)" : ""}`
  }
  const parts = [hook.kinds?.length ? `${hook.kinds.length} kind(s)` : "all events"]
  if (hook.channels?.length) parts.push(`in ${hook.channels.map((name) => `#${name}`).join(", ")}`)
  if (hook.mentions?.length) parts.push(`mentioning ${hook.mentions.map((name) => `@${name}`).join(", ")}`)
  if (hook.exclude_authors?.length) parts.push(`not from ${hook.exclude_authors.join(", ")}`)
  return parts.join(" · ")
}

function HookRow(props: { hook: Webhook; mutations: Mutations; onSecret: (notice: SecretNotice) => void }) {
  const [confirming, setConfirming] = createSignal(false)
  const [message, setMessage] = createSignal("")
  const hook = () => props.hook
  const outbound = () => hook().direction === "out"
  const status = () => (hook().enabled ? (outbound() ? hook().last_status ?? "waiting for events" : "listening") : "disabled")

  const test = () => {
    setMessage("Sending…")
    props.mutations.test.mutate(hook().id, {
      onSuccess: (result) => setMessage(result.ok ? `Ping delivered (HTTP ${result.status})` : `Ping failed: ${result.error ?? `HTTP ${result.status}`}`),
      onError: (error) => setMessage(getErrorMessage(error)),
    })
  }

  return (
    <li class="rounded-lg border border-line p-3">
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-medium text-cream">{hook().name}</span>
        <Badge>{outbound() ? `out · ${hook().format}` : "in"}</Badge>
        <Badge class={hook().enabled ? undefined : "text-rust"}>{status()}</Badge>
      </div>
      <p class="mt-1 text-xs text-cream-dim">{summarize(hook())}</p>
      <Show when={hook().last_error}>
        <p class="mt-1 text-xs text-rust">{hook().last_error}</p>
      </Show>
      <Show when={hook().last_delivery_at}>
        {(at) => <p class="mt-1 text-xs text-cream-dim" title={at()}>Last delivery {formatStamp(at())}</p>}
      </Show>
      <div class="mt-2 flex flex-wrap items-center gap-2">
        <Show when={outbound()}>
          <Button type="button" size="sm" variant="outline" onClick={test}>Test</Button>
        </Show>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => props.mutations.update.mutate({ id: hook().id, changes: { enabled: !hook().enabled } }, { onError: (error) => setMessage(getErrorMessage(error)) })}
        >
          {hook().enabled ? "Disable" : "Enable"}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() =>
            props.mutations.rotate.mutate(hook().id, {
              onSuccess: (result) => props.onSecret({ name: result.webhook.name, direction: result.webhook.direction, secret: result.secret }),
              onError: (error) => setMessage(getErrorMessage(error)),
            })
          }
        >
          Rotate secret
        </Button>
        <Show
          when={confirming()}
          fallback={<Button type="button" size="sm" variant="danger" onClick={() => setConfirming(true)}>Delete</Button>}
        >
          <Button
            type="button"
            size="sm"
            variant="danger"
            onClick={() => props.mutations.remove.mutate(hook().id, { onError: (error) => { setConfirming(false); setMessage(getErrorMessage(error)) } })}
          >
            Really delete
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setConfirming(false)}>Keep</Button>
        </Show>
      </div>
      <Show when={message()}>
        <p class="mt-2 text-xs text-cream-dim" aria-live="polite">{message()}</p>
      </Show>
    </li>
  )
}

function NewHookForm(props: { mutations: Mutations; onCreated: (notice: SecretNotice) => void }) {
  const prefs = usePrefs()
  const channels = useChannels()
  const [direction, setDirection] = createSignal<WebhookDirection>("out")
  const [name, setName] = createSignal("")
  const [error, setError] = createSignal("")
  // Outbound.
  const [url, setUrl] = createSignal("")
  const [format, setFormat] = createSignal<WebhookFormat>("discord")
  const [kinds, setKinds] = createSignal<string[]>([])
  const [channelsText, setChannelsText] = createSignal("")
  const [mentionsText, setMentionsText] = createSignal("")
  const [excludeText, setExcludeText] = createSignal("")
  // Inbound.
  const [target, setTarget] = createSignal("fleet")
  const [author, setAuthor] = createSignal("")
  const [override, setOverride] = createSignal(false)

  const openChannels = () => (channels.data?.channels ?? []).filter((channel) => channel.state !== "archived")
  const toggleKind = (kind: string) =>
    setKinds((current) => (current.includes(kind) ? current.filter((item) => item !== kind) : [...current, kind]))
  const mentionsMe = () => {
    const me = prefs.displayName().trim()
    if (!isHandle(me)) return setError("Set a valid name in the header first.")
    setKinds([])
    setMentionsText(me)
  }

  const submit = (event: SubmitEvent) => {
    event.preventDefault()
    setError("")
    const hookName = name().trim()
    if (!CHANNEL_RE.test(hookName)) return setError("Name must be lowercase letters, digits, '-' or '_'.")
    const common = { name: hookName, direction: direction() }
    const input =
      direction() === "out"
        ? { ...common, url: url().trim(), format: format(), kinds: kinds(), channels: channelsText(), mentions: mentionsText(), exclude_authors: excludeText() }
        : { ...common, channel: target(), author: author().trim() || undefined, allow_override: override() }
    props.mutations.create.mutate(input, {
      onSuccess: (result) => {
        // The page stays open, so clear what was just submitted. Direction and format are kept for the next hook.
        setName("")
        setUrl("")
        setAuthor("")
        setOverride(false)
        props.onCreated({ name: result.webhook.name, direction: result.webhook.direction, secret: result.secret })
      },
      onError: (failure) => setError(getErrorMessage(failure)),
    })
  }

  return (
    <form class="flex flex-col gap-3" onSubmit={submit}>
      <fieldset class="flex gap-2">
        <legend class="mb-1 text-sm text-cream-dim">Direction</legend>
        <Button type="button" size="sm" variant={direction() === "out" ? "default" : "outline"} aria-pressed={direction() === "out"} onClick={() => setDirection("out")}>
          Outbound (hub → app)
        </Button>
        <Button type="button" size="sm" variant={direction() === "in" ? "default" : "outline"} aria-pressed={direction() === "in"} onClick={() => setDirection("in")}>
          Inbound (app → hub)
        </Button>
      </fieldset>
      <label class="flex flex-col gap-1 text-sm text-cream-dim">Name
        <Input value={name()} placeholder={direction() === "out" ? "discord-feed" : "github"} autocomplete="off" onInput={(event) => setName(event.currentTarget.value)} />
      </label>

      <Show
        when={direction() === "out"}
        fallback={
          <>
            <label class="flex flex-col gap-1 text-sm text-cream-dim">Post into channel
              <select class={selectClass} value={target()} onChange={(event) => setTarget(event.currentTarget.value)}>
                <For each={openChannels()}>{(channel) => <option value={channel.name}>#{channel.name}</option>}</For>
              </select>
            </label>
            <label class="flex flex-col gap-1 text-sm text-cream-dim">Author handle (optional, default hook-{name() || "name"})
              <Input value={author()} autocomplete="off" onInput={(event) => setAuthor(event.currentTarget.value)} />
            </label>
            <label class="flex items-center gap-2 text-sm text-cream-dim">
              <input type="checkbox" checked={override()} onChange={(event) => setOverride(event.currentTarget.checked)} />
              Let the caller choose the channel and author
            </label>
          </>
        }
      >
        <label class="flex flex-col gap-1 text-sm text-cream-dim">Receiver URL (contains a token, so it is hidden)
          <Input type="password" value={url()} autocomplete="off" placeholder="https://discord.com/api/webhooks/…" onInput={(event) => setUrl(event.currentTarget.value)} />
        </label>
        <label class="flex flex-col gap-1 text-sm text-cream-dim">Format
          <select class={selectClass} value={format()} onChange={(event) => setFormat(event.currentTarget.value as WebhookFormat)}>
            <option value="discord">Discord</option>
            <option value="slack">Slack</option>
            <option value="generic">Generic signed JSON</option>
          </select>
        </label>
        <fieldset>
          <legend class="mb-1 text-sm text-cream-dim">Events (none checked sends everything)</legend>
          <div class="mb-2 flex flex-wrap gap-2">
            <For each={KIND_PRESETS}>{(preset) => <Button type="button" size="sm" variant="outline" onClick={() => setKinds([...preset.kinds])}>{preset.label}</Button>}</For>
            <Button type="button" size="sm" variant="outline" onClick={mentionsMe}>Mentions of me</Button>
          </div>
          <div class="grid grid-cols-1 gap-1 sm:grid-cols-2">
            <For each={EVENT_KINDS}>{(kind) => (
              <label class="flex items-center gap-2 text-xs text-cream">
                <input type="checkbox" checked={kinds().includes(kind)} onChange={() => toggleKind(kind)} />
                {kind}
              </label>
            )}</For>
          </div>
        </fieldset>
        <label class="flex flex-col gap-1 text-sm text-cream-dim">Only these channels (comma separated, empty for all)
          <Input value={channelsText()} placeholder="fleet, ops" onInput={(event) => setChannelsText(event.currentTarget.value)} />
        </label>
        <label class="flex flex-col gap-1 text-sm text-cream-dim">Only messages mentioning, or tasks assigned to
          <Input value={mentionsText()} placeholder="builder, reviewer" onInput={(event) => setMentionsText(event.currentTarget.value)} />
        </label>
        <label class="flex flex-col gap-1 text-sm text-cream-dim">Skip authors matching (use dc-* if you run the Discord relay)
          <Input value={excludeText()} placeholder="dc-*" onInput={(event) => setExcludeText(event.currentTarget.value)} />
        </label>
      </Show>

      <Show when={error()}>
        <p class="text-sm text-rust" role="alert">{error()}</p>
      </Show>
      <div class="flex justify-end">
        <Button type="submit" disabled={props.mutations.create.isPending}>
          {props.mutations.create.isPending ? "Creating…" : "Create webhook"}
        </Button>
      </div>
    </form>
  )
}

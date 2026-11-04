import { createSignal, For, Show } from "solid-js"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useChannels } from "@/hooks/useChannels"
import { useWebhookMutations, useWebhooks } from "@/hooks/useWebhooks"
import { usePrefs } from "@/store"
import { EVENT_KINDS, type Webhook, type WebhookDirection, type WebhookFormat } from "@/api/types"
import { getErrorMessage } from "@/utils/errors"
import { CHANNEL_RE, isHandle } from "@/utils/handles"
import { InfraTabs, PageHeader, SideCard } from "@/components/ui/page"
import { Icon } from "@/components/ui/icons"
import { relativeTime } from "@/utils/fleetStatus"

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
  const [creating, setCreating] = createSignal(false)
  const rows = () => hooks.data?.webhooks ?? []
  const attention = () => rows().filter((hook) => hook.enabled && Boolean(hook.last_error)).length
  // Outbound only, and only after a confirmed delivery: failures record a retry:/disabled: status with an error.
  const healthy = () => rows().filter((hook) => hook.enabled && hook.direction === "out" && delivered(hook)).length

  return (
    <div class="page min-h-0 flex-1 overflow-y-auto">
      <div class="mx-auto max-w-6xl">
        <InfraTabs />
        <PageHeader
          eyebrow="Connected to your workflow"
          title="Updates, delivered."
          description="Connect fleet events to the tools where your team already works, and let other apps post back in."
          stats={[
            { value: rows().length, label: "webhooks" },
            { value: attention(), label: "need attention", tone: attention() ? "warn" : undefined },
            { value: healthy(), label: "healthy", tone: healthy() ? "ok" : undefined },
          ]}
        >
          <Button type="button" class="h-10 px-4" variant={creating() ? "outline" : "default"} aria-expanded={creating()} onClick={() => setCreating((open) => !open)}>
            {creating() ? "Close" : "＋ New webhook"}
          </Button>
        </PageHeader>
        <Show when={notice()}>{(current) => <SecretScreen notice={current()} onDismiss={() => setNotice(null)} />}</Show>
        <Show when={creating()}>
          <section aria-labelledby="new-hook-heading" class="card mb-6 rounded-2xl border border-line p-5">
            <h2 id="new-hook-heading" class="mb-4 text-[15px] font-semibold text-cream">Set up a webhook</h2>
            <NewHookForm mutations={mutations} onCreated={(created) => { setNotice(created); setCreating(false) }} />
          </section>
        </Show>
        <div class="grid gap-8 lg:grid-cols-[minmax(0,1fr)_18.5rem]">
          <section aria-labelledby="hooks-heading" class="min-w-0">
            <h2 id="hooks-heading" class="sr-only">Your webhooks</h2>
            <HookList hooks={hooks.data?.webhooks} error={hooks.error} mutations={mutations} onSecret={setNotice} />
          </section>
          <div class="flex flex-col gap-5">
            <SideCard title="Choose the right direction">
              <p class="font-semibold text-cream">Outbound · Fleet → App</p>
              <p class="mb-3">Push fleet activity to Discord, Slack or any URL so you can follow along.</p>
              <p class="font-semibold text-cream">Inbound · App → Fleet</p>
              <p>Let other apps post into a channel. That is how you reach an agent from outside.</p>
            </SideCard>
            <SideCard title="Secrets stay in the hub">
              Receiver URLs carry tokens, so they are always shown redacted. A hook's secret appears once, when you create or rotate it.
            </SideCard>
          </div>
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
    <div class="card mb-6 rounded-2xl border border-brass p-5" role="status">
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
          <Show when={hooks().length > 0} fallback={<p class="rounded-2xl border border-dashed border-line px-4 py-10 text-center text-sm text-cream-dim">No webhooks yet. Create one with “New webhook”.</p>}>
            <ul class="flex flex-col gap-4">
              <For each={hooks()}>{(hook) => <HookRow hook={hook} mutations={props.mutations} onSecret={props.onSecret} />}</For>
            </ul>
          </Show>
        )}
      </Show>
    </Show>
  )
}

/** A delivery the receiver accepted: a status recorded with no error and no retry/disable marker. */
function delivered(hook: Webhook): boolean {
  const status = hook.last_status ?? ""
  return Boolean(status) && !hook.last_error && !/^(retry|disabled):/.test(status)
}

function summarize(hook: Webhook): string {
  if (hook.direction === "in") {
    return `posts to #${hook.channel} as ${hook.author}${hook.allow_override ? " (caller may override)" : ""}`
  }
  const parts = [hook.directed_only ? "only messages an agent addresses to it" : hook.kinds?.length ? `${hook.kinds.length} kind(s)` : "all events"]
  if (hook.discord_channel_id) parts.push(`Discord channel ${hook.discord_channel_id}`)
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

  const test = () => {
    setMessage("Sending…")
    props.mutations.test.mutate(hook().id, {
      onSuccess: (result) => setMessage(result.ok ? `Ping delivered (HTTP ${result.status})` : `Ping failed: ${result.error ?? `HTTP ${result.status}`}`),
      onError: (error) => setMessage(getErrorMessage(error)),
    })
  }

  const badge = () =>
    !hook().enabled ? { text: "Paused", tone: undefined }
      : hook().last_error ? { text: "Needs attention", tone: "warn" }
        : outbound() ? { text: delivered(hook()) ? "Healthy" : "Waiting for events", tone: delivered(hook()) ? "ok" : undefined }
          : { text: "Listening", tone: "ok" }
  const formatName = () => ({ discord: "Discord", slack: "Slack", generic: "Generic JSON" } as Record<string, string>)[hook().format ?? ""] ?? hook().format

  return (
    <li class="card rounded-2xl border border-line p-5">
      <div class="flex items-start gap-3">
        <span class="icon-tile size-10 shrink-0" aria-hidden="true"><Icon name={outbound() ? "arrow" : "hook"} class="size-[18px]" /></span>
        <div class="min-w-0 flex-1">
          <p class="truncate text-[15px] font-semibold text-cream">{hook().name}</p>
          <p class="text-xs text-cream-dim">{outbound() ? `Outbound · ${formatName()}` : `Inbound · #${hook().channel}`}</p>
        </div>
        <span class="soft-badge shrink-0" data-tone={badge().tone}>{badge().text}</span>
      </div>
      <Show when={hook().description}>
        <p class="mt-3 text-sm text-cream">{hook().description}</p>
      </Show>
      <Show when={hook().last_error}>
        <div class="alert-card mt-4 rounded-xl px-4 py-3 text-[13px]">
          <p class="font-semibold text-rust">Last delivery failed{hook().last_status ? ` · ${hook().last_status}` : ""}</p>
          <p class="mt-1 break-words text-cream">{hook().last_error}</p>
        </div>
      </Show>
      <dl class="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3">
        <div class="min-w-0"><dt class="field-label">{outbound() ? "Sends" : "Posts as"}</dt><dd class="mt-1 text-[13px] text-cream">{outbound() ? (hook().directed_only ? "Addressed messages" : hook().kinds?.length ? `${hook().kinds!.length} event kind${hook().kinds!.length === 1 ? "" : "s"}` : "All events") : hook().author ?? `hook-${hook().name}`}</dd></div>
        <Show when={outbound()}>
          <div><dt class="field-label">Failures</dt><dd class="mt-1 text-[13px] text-cream tabular-nums">{hook().failure_count ?? 0}</dd></div>
        </Show>
        <div><dt class="field-label">{outbound() ? "Last delivery" : "Updated"}</dt><dd class="mt-1 text-[13px] text-cream" title={hook().last_delivery_at ?? hook().updated_at}>{relativeTime((outbound() ? hook().last_delivery_at : hook().updated_at) ?? "") || "Never"}</dd></div>
      </dl>
      <details class="provenance mt-3 text-xs">
        <summary class="cursor-pointer select-none text-cream-dim hover:text-cream">Filters &amp; details</summary>
        <p class="mt-2 text-cream-dim">{summarize(hook())}</p>
      </details>
      <div class="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-4">
        <Show when={outbound()}>
          <Button type="button" size="sm" variant="outline" onClick={test}>Send test</Button>
        </Show>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => props.mutations.update.mutate({ id: hook().id, changes: { enabled: !hook().enabled } }, { onError: (error) => setMessage(getErrorMessage(error)) })}
        >
          {hook().enabled ? "Pause" : "Resume"}
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
        <span class="ml-auto flex items-center gap-2">
          <Show
            when={confirming()}
            fallback={<Button type="button" size="sm" variant="ghost" class="text-rust" onClick={() => setConfirming(true)}>Remove</Button>}
          >
            <Button
              type="button"
              size="sm"
              variant="danger"
              onClick={() => props.mutations.remove.mutate(hook().id, { onError: (error) => { setConfirming(false); setMessage(getErrorMessage(error)) } })}
            >
              Really remove
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setConfirming(false)}>Keep</Button>
          </Show>
        </span>
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
  const [description, setDescription] = createSignal("")
  const [directedOnly, setDirectedOnly] = createSignal(false)
  const [discordId, setDiscordId] = createSignal("")
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
        ? {
            ...common,
            url: url().trim(),
            format: format(),
            kinds: kinds(),
            channels: channelsText(),
            mentions: mentionsText(),
            exclude_authors: excludeText(),
            description: description().trim() || undefined,
            directed_only: directedOnly(),
            discord_channel_id: discordId().trim() || undefined,
          }
        : { ...common, channel: target(), author: author().trim() || undefined, allow_override: override() }
    props.mutations.create.mutate(input, {
      onSuccess: (result) => {
        // The page stays open, so clear what was just submitted. Direction and format are kept for the next hook.
        setName("")
        setUrl("")
        setAuthor("")
        setOverride(false)
        setDescription("")
        setDirectedOnly(false)
        setDiscordId("")
        props.onCreated({ name: result.webhook.name, direction: result.webhook.direction, secret: result.secret })
      },
      onError: (failure) => setError(getErrorMessage(failure)),
    })
  }

  return (
    <form class="grid gap-3 sm:grid-cols-2 [&>*]:sm:col-span-2" onSubmit={submit}>
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
        <label class="flex flex-col gap-1 text-sm text-cream-dim">What agents should use it for (shown to agents)
          <Input value={description()} maxLength={200} placeholder="urgent: a human needs to look at this" onInput={(event) => setDescription(event.currentTarget.value)} />
        </label>
        <label class="flex items-center gap-2 text-sm text-cream-dim">
          <input type="checkbox" checked={directedOnly()} onChange={(event) => setDirectedOnly(event.currentTarget.checked)} />
          Only send messages an agent addresses to this hook (ignores the filters below)
        </label>
        <label class="flex flex-col gap-1 text-sm text-cream-dim">Discord channel ID (optional; looked up for Discord URLs)
          <Input value={discordId()} inputMode="numeric" placeholder="1557244251492978758" autocomplete="off" onInput={(event) => setDiscordId(event.currentTarget.value)} />
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

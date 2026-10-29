import { createMemo, createSignal, For, Show } from "solid-js"
import { describeRoute, EXTERNAL_ID_RE, parseIdList, RELAY_NAME_RE, relayAudit, validIds } from "@/api/relays"
import { RELAY_PLATFORMS, type Relay, type RelayPlatform, type RelaySnapshot, type RelayState, type RouteFormat } from "@/api/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useRelay, useRelayAudit, useRelayMutations, useRelays } from "@/hooks/useRelays"
import { usePrefs } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { CHANNEL_RE, isHandle } from "@/utils/handles"
import { formatStamp } from "@/utils/time"

const selectClass =
  "h-9 w-full rounded-lg border border-line bg-ink-sunken px-3 text-sm text-cream focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brass"

const STATE_CLASS: Record<RelayState, string> = {
  online: "text-moss",
  stale: "text-brass",
  offline: "text-rust",
  disabled: "text-cream-dim",
}

type Mutations = ReturnType<typeof useRelayMutations>

/** The one-time token notice. Held in memory only; dismissing it drops the token. */
interface TokenNotice {
  name: string
  token: string
}

/** `true` when `actor` is set; otherwise shows the same hint every action uses and returns `false`. */
function hasActor(actor: () => string, setError: (message: string) => void): boolean {
  if (!actor()) setError("Set a valid Name in the header first.")
  return Boolean(actor())
}

/** The acting handle for audit rows, from the Name field in the header. */
function useActor() {
  const prefs = usePrefs()
  return () => {
    const name = prefs.displayName().trim()
    return isHandle(name) ? name : ""
  }
}

/**
 * The Relay page: chat-platform bridges (Discord, Slack, Telegram) configured
 * in the hub. Each relay has bindings (platform channel to hub channel),
 * routes (events delivered to a platform channel) and a live heartbeat.
 * Every change is audited and needs the version you are looking at.
 */
export default function RelayPage() {
  const relays = useRelays()
  const mutations = useRelayMutations()
  const [selected, setSelected] = createSignal<string | null>(null)
  const [notice, setNotice] = createSignal<TokenNotice | null>(null)
  const current = () => selected() ?? relays.data?.relays[0]?.name ?? null

  return (
    <div class="min-h-0 flex-1 overflow-y-auto p-4">
      <div class="mx-auto max-w-6xl">
        <h1 class="font-display text-xl text-cream">Relays</h1>
        <p class="mb-4 mt-1 text-sm text-cream-dim">
          A relay bridges a Discord, Slack or Telegram bot to the fleet. Its bindings, routes and allowlists live here and take effect on the bot's next heartbeat, without a restart. Run it with <code>cat-fleet-relay --platform … --name …</code> and its token.
        </p>
        <Show when={notice()}>{(shown) => <TokenScreen notice={shown()} onDismiss={() => setNotice(null)} />}</Show>
        <div class="grid gap-6 lg:grid-cols-[18rem_1fr]">
          <aside aria-label="Relays" class="flex flex-col gap-3">
            <Show when={relays.error}>
              <p class="text-sm text-rust">{getErrorMessage(relays.error)}</p>
            </Show>
            <ul class="flex flex-col gap-2">
              <For each={relays.data?.relays} fallback={<li class="text-sm text-cream-dim">No relays yet.</li>}>
                {(relay) => (
                  <li>
                    <button
                      type="button"
                      class={`w-full rounded-lg border p-3 text-left ${current() === relay.name ? "border-brass" : "border-line"}`}
                      onClick={() => setSelected(relay.name)}
                      aria-pressed={current() === relay.name}
                    >
                      <div class="flex items-center gap-2">
                        <span class="font-medium text-cream">{relay.name}</span>
                        <Badge>{relay.platform}</Badge>
                        <span class={`ml-auto text-xs ${STATE_CLASS[relay.state]}`} data-testid={`state-${relay.name}`}>● {relay.state}</span>
                      </div>
                      <p class="mt-1 text-xs text-cream-dim">
                        v{relay.version}
                        {relay.last_seen_at ? ` · seen ${formatStamp(relay.last_seen_at)}` : " · never seen"}
                      </p>
                    </button>
                  </li>
                )}
              </For>
            </ul>
            <NewRelayForm mutations={mutations} onCreated={(created) => { setNotice(created); setSelected(created.name) }} />
          </aside>
          {/* keyed: each relay gets a fresh tree, so a draft for one relay can't be saved to another */}
          <Show when={current()} keyed fallback={<p class="text-sm text-cream-dim">Create a relay to configure it.</p>}>
            {(name) => <RelayDetail name={name} mutations={mutations} onToken={setNotice} onDeleted={() => setSelected(null)} />}
          </Show>
        </div>
      </div>
    </div>
  )
}

function TokenScreen(props: { notice: TokenNotice; onDismiss: () => void }) {
  return (
    <section role="alert" class="mb-4 rounded-lg border border-brass p-4">
      <h2 class="font-display text-lg text-cream">Token for {props.notice.name}</h2>
      <p class="mt-1 text-sm text-cream-dim">
        Shown once. Give it to the relay as <code>CAT_FLEET_RELAY_TOKEN</code>. It only reaches this relay's own config, bindings, routes and posts; it is not the fleet token.
      </p>
      <code class="mt-2 block break-all rounded bg-ink-sunken p-2 text-sm text-cream" data-testid="relay-token">{props.notice.token}</code>
      <div class="mt-2 flex justify-end">
        <Button type="button" variant="outline" size="sm" onClick={props.onDismiss}>I saved it</Button>
      </div>
    </section>
  )
}

function NewRelayForm(props: { mutations: Mutations; onCreated: (notice: TokenNotice) => void }) {
  const actor = useActor()
  const [name, setName] = createSignal("")
  const [platform, setPlatform] = createSignal<RelayPlatform>("discord")
  const [error, setError] = createSignal("")
  return (
    <form
      class="flex flex-col gap-2 rounded-lg border border-line p-3"
      aria-label="New relay"
      onSubmit={(event) => {
        event.preventDefault()
        if (!actor()) { setError("Set a valid Name in the header first; it is recorded in the audit log."); return }
        if (!RELAY_NAME_RE.test(name())) { setError("Name: lowercase letters, digits, - or _ (max 40)."); return }
        setError("")
        props.mutations.create.mutate(
          { actor: actor(), name: name(), platform: platform() },
          {
            onSuccess: (result) => { setName(""); props.onCreated({ name: result.relay.name, token: result.token }) },
            onError: (err) => setError(getErrorMessage(err)),
          },
        )
      }}
    >
      <h2 class="font-display text-base text-cream">New relay</h2>
      <Input aria-label="Relay name" placeholder="main" value={name()} onInput={(event) => setName(event.currentTarget.value.trim())} />
      <select class={selectClass} aria-label="Platform" value={platform()} onChange={(event) => setPlatform(event.currentTarget.value as RelayPlatform)}>
        <For each={RELAY_PLATFORMS}>{(item) => <option value={item}>{item}</option>}</For>
      </select>
      <Button type="submit" size="sm">Create</Button>
      <Show when={error()}><p class="text-xs text-rust" role="status">{error()}</p></Show>
    </form>
  )
}

function RelayDetail(props: { name: string; mutations: Mutations; onToken: (notice: TokenNotice) => void; onDeleted: () => void }) {
  const detail = useRelay(() => props.name)
  const [tab, setTab] = createSignal<"setup" | "audit">("setup")
  return (
    <section aria-label={`Relay ${props.name}`} class="min-w-0">
      <Show when={detail.error}>
        <p class="text-sm text-rust">{getErrorMessage(detail.error)}</p>
      </Show>
      <Show when={detail.data}>
        {(snapshot) => (
          <>
            <RelayHeader snapshot={snapshot()} mutations={props.mutations} onToken={props.onToken} onDeleted={props.onDeleted} />
            <div class="mt-4 flex gap-2" role="tablist">
              <Button type="button" size="sm" variant={tab() === "setup" ? "default" : "outline"} role="tab" aria-selected={tab() === "setup"} onClick={() => setTab("setup")}>Setup</Button>
              <Button type="button" size="sm" variant={tab() === "audit" ? "default" : "outline"} role="tab" aria-selected={tab() === "audit"} onClick={() => setTab("audit")}>Audit</Button>
            </div>
            <Show when={tab() === "setup"} fallback={<AuditList name={props.name} />}>
              <div class="mt-4 grid gap-6 xl:grid-cols-2">
                <Bindings snapshot={snapshot()} mutations={props.mutations} />
                <Routes snapshot={snapshot()} mutations={props.mutations} />
              </div>
              <ConfigEditor snapshot={snapshot()} mutations={props.mutations} />
            </Show>
          </>
        )}
      </Show>
    </section>
  )
}

function statusLine(relay: Relay): string {
  const status = relay.status as Record<string, unknown>
  const parts: string[] = []
  if (typeof status.delivered === "number") parts.push(`${status.delivered} delivered`)
  if (typeof status.failed === "number") parts.push(`${status.failed} failed`)
  if (typeof status.inbound === "number") parts.push(`${status.inbound} inbound`)
  if (typeof status.loops === "number") parts.push(`${status.loops} loops`)
  return parts.join(" · ")
}

function RelayHeader(props: { snapshot: RelaySnapshot; mutations: Mutations; onToken: (notice: TokenNotice) => void; onDeleted: () => void }) {
  const actor = useActor()
  const [message, setMessage] = createSignal("")
  const [confirm, setConfirm] = createSignal<string | null>(null)
  const relay = () => props.snapshot.relay
  const lastError = () => (relay().status as Record<string, unknown>).last_error as string | undefined
  const needActor = () => { if (!actor()) setMessage("Set a valid Name in the header first."); return Boolean(actor()) }
  return (
    <div class="rounded-lg border border-line p-4">
      <div class="flex flex-wrap items-center gap-2">
        <h2 class="font-display text-lg text-cream">{relay().name}</h2>
        <Badge>{relay().platform}</Badge>
        <Badge class={STATE_CLASS[relay().state]}>{relay().state}</Badge>
        <Badge>v{props.snapshot.version}</Badge>
      </div>
      <Show when={statusLine(relay())}><p class="mt-1 text-xs text-cream-dim">{statusLine(relay())}</p></Show>
      <Show when={lastError()}><p class="mt-1 text-xs text-rust">{lastError()}</p></Show>
      <div class="mt-3 flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => needActor() && props.mutations.update.mutate(
            { name: relay().name, version: props.snapshot.version, changes: { actor: actor(), enabled: !relay().enabled } },
            { onError: (err) => setMessage(getErrorMessage(err)) },
          )}
        >
          {relay().enabled ? "Disable" : "Enable"}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => needActor() && props.mutations.rotate.mutate(
            { name: relay().name, actor: actor() },
            { onSuccess: (result) => props.onToken({ name: result.relay.name, token: result.token }), onError: (err) => setMessage(getErrorMessage(err)) },
          )}
        >
          Rotate token
        </Button>
        <Show
          when={confirm() !== null}
          fallback={<Button type="button" size="sm" variant="danger" onClick={() => setConfirm("")}>Delete</Button>}
        >
          <Input class="h-8 w-40" aria-label="Type the relay name to delete" placeholder={relay().name} value={confirm() ?? ""} onInput={(event) => setConfirm(event.currentTarget.value)} />
          <Button
            type="button"
            size="sm"
            variant="danger"
            disabled={confirm() !== relay().name}
            onClick={() => needActor() && props.mutations.remove.mutate(
              { name: relay().name, version: props.snapshot.version, actor: actor() },
              { onSuccess: props.onDeleted, onError: (err) => { setConfirm(null); setMessage(getErrorMessage(err)) } },
            )}
          >
            Really delete
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setConfirm(null)}>Keep</Button>
        </Show>
      </div>
      <Show when={message()}><p class="mt-2 text-xs text-cream-dim" aria-live="polite">{message()}</p></Show>
    </div>
  )
}

function Bindings(props: { snapshot: RelaySnapshot; mutations: Mutations }) {
  const actor = useActor()
  const [external, setExternal] = createSignal("")
  const [channel, setChannel] = createSignal("fleet")
  const [allowed, setAllowed] = createSignal("")
  const [direction, setDirection] = createSignal("both")
  const [error, setError] = createSignal("")
  const name = () => props.snapshot.relay.name
  return (
    <section aria-labelledby="bindings-heading" class="rounded-lg border border-line p-4">
      <h3 id="bindings-heading" class="font-display text-base text-cream">Bindings</h3>
      <p class="text-xs text-cream-dim">Platform channel or thread → hub channel. A thread with no binding of its own uses its channel's.</p>
      <ul class="mt-2 flex flex-col gap-2">
        <For each={props.snapshot.bindings} fallback={<li class="text-sm text-cream-dim">Nothing bound: the relay relays nothing.</li>}>
          {(binding) => (
            <li class="flex flex-wrap items-center gap-2 text-sm">
              <code class="text-cream">{binding.external_id}</code>
              <span class="text-cream-dim">→</span>
              <span class="text-cream">#{binding.hub_channel}</span>
              <Show when={binding.kind === "thread"}><Badge>thread</Badge></Show>
              <Show when={binding.task_id}><Badge>task #{binding.task_id}</Badge></Show>
              <Badge>{binding.direction}</Badge>
              <Show when={binding.any_channel || binding.allowed.length}>
                <span class="text-xs text-cream-dim">#tokens: {binding.any_channel ? "any" : binding.allowed.join(", ")}</span>
              </Show>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                class="ml-auto"
                aria-label={`Unbind ${binding.external_id}`}
                onClick={() => hasActor(actor, setError) && props.mutations.unbind.mutate({ name: name(), id: binding.id, actor: actor() }, { onError: (err) => setError(getErrorMessage(err)) })}
              >
                Unbind
              </Button>
            </li>
          )}
        </For>
      </ul>
      <form
        class="mt-3 grid gap-2 sm:grid-cols-2"
        aria-label="Add binding"
        onSubmit={(event) => {
          event.preventDefault()
          const extras = parseIdList(allowed())
          if (!actor()) { setError("Set a valid Name in the header first."); return }
          if (!EXTERNAL_ID_RE.test(external())) { setError("Platform id: letters, digits, - _ : ."); return }
          if (!CHANNEL_RE.test(channel()) || !extras.every((item) => item === "*" || CHANNEL_RE.test(item))) { setError("Hub channels must be channel names."); return }
          setError("")
          props.mutations.bind.mutate(
            {
              name: name(),
              body: {
                actor: actor(),
                external_id: external(),
                hub_channel: channel(),
                allowed: extras.filter((item) => item !== "*"),
                any_channel: extras.includes("*"),
                direction: direction(),
              },
            },
            { onSuccess: () => setExternal(""), onError: (err) => setError(getErrorMessage(err)) },
          )
        }}
      >
        <Input aria-label="Platform channel id" placeholder="Platform channel id" value={external()} onInput={(event) => setExternal(event.currentTarget.value.trim())} />
        <Input aria-label="Hub channel" placeholder="fleet" value={channel()} onInput={(event) => setChannel(event.currentTarget.value.trim())} />
        <Input aria-label="Allowed #channels" placeholder="ops, deploy (or *)" value={allowed()} onInput={(event) => setAllowed(event.currentTarget.value)} />
        <select class={selectClass} aria-label="Direction" value={direction()} onChange={(event) => setDirection(event.currentTarget.value)}>
          <option value="both">both</option>
          <option value="in">in only</option>
          <option value="out">out only</option>
        </select>
        <Button type="submit" size="sm" class="sm:col-span-2">Bind</Button>
      </form>
      <Show when={error()}><p class="mt-2 text-xs text-rust" role="status">{error()}</p></Show>
    </section>
  )
}

function Routes(props: { snapshot: RelaySnapshot; mutations: Mutations }) {
  const actor = useActor()
  const [external, setExternal] = createSignal("")
  const [what, setWhat] = createSignal("")
  const [mode, setMode] = createSignal<"topics" | "kinds">("topics")
  const [format, setFormat] = createSignal<RouteFormat>("line")
  const [error, setError] = createSignal("")
  const [seekFor, setSeekFor] = createSignal<number | null>(null)
  const [seekTo, setSeekTo] = createSignal("")
  const name = () => props.snapshot.relay.name
  return (
    <section aria-labelledby="routes-heading" class="rounded-lg border border-line p-4">
      <h3 id="routes-heading" class="font-display text-base text-cream">Routes</h3>
      <p class="text-xs text-cream-dim">Events delivered to a platform channel, at least once, from a durable consumer. Cards carry action buttons.</p>
      <ul class="mt-2 flex flex-col gap-2">
        <For each={props.snapshot.routes} fallback={<li class="text-sm text-cream-dim">No routes.</li>}>
          {(route) => (
            <li class="rounded border border-line p-2 text-sm">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-cream">{describeRoute(route)}</span>
                <span class="text-cream-dim">→</span>
                <code class="text-cream">{route.external_id}</code>
                <Badge>{route.format}</Badge>
                <Badge class={route.consumer?.armed ? "text-moss" : undefined}>{route.consumer?.armed ? "armed" : "idle"}</Badge>
                <Show when={route.consumer?.behind}><Badge>{route.consumer?.behind} behind</Badge></Show>
                <Show when={!route.enabled}><Badge class="text-rust">disabled</Badge></Show>
              </div>
              <div class="mt-2 flex flex-wrap gap-2">
                <Button type="button" size="sm" variant="outline" onClick={() => hasActor(actor, setError) && props.mutations.updateRoute.mutate({ name: name(), id: route.id, changes: { actor: actor(), enabled: !route.enabled } }, { onError: (err) => setError(getErrorMessage(err)) })}>
                  {route.enabled ? "Pause" : "Resume"}
                </Button>
                <Button type="button" size="sm" variant="outline" onClick={() => { setSeekFor(route.id); setSeekTo("") }}>Replay…</Button>
                <Button type="button" size="sm" variant="ghost" onClick={() => hasActor(actor, setError) && props.mutations.removeRoute.mutate({ name: name(), id: route.id, actor: actor() }, { onError: (err) => setError(getErrorMessage(err)) })}>Remove</Button>
              </div>
              <Show when={seekFor() === route.id}>
                <form
                  class="mt-2 flex gap-2"
                  aria-label="Replay route"
                  onSubmit={(event) => {
                    event.preventDefault()
                    // Digits only: Number("") is 0, which would replay the whole event log.
                    const raw = seekTo().trim()
                    const value = Number(raw)
                    if (!actor() || !/^\d+$/.test(raw) || !Number.isSafeInteger(value)) { setError("Replay needs an event id and your Name."); return }
                    props.mutations.seek.mutate({ name: name(), id: route.id, afterEventId: value, actor: actor() }, {
                      onSuccess: () => setSeekFor(null),
                      onError: (err) => setError(getErrorMessage(err)),
                    })
                  }}
                >
                  <Input aria-label="Replay after event id" placeholder={`after event id (now ${route.consumer?.cursor})`} value={seekTo()} onInput={(event) => setSeekTo(event.currentTarget.value.trim())} />
                  <Button type="submit" size="sm">Replay</Button>
                </form>
              </Show>
            </li>
          )}
        </For>
      </ul>
      <form
        class="mt-3 grid gap-2 sm:grid-cols-2"
        aria-label="Add route"
        onSubmit={(event) => {
          event.preventDefault()
          if (!actor()) { setError("Set a valid Name in the header first."); return }
          if (!EXTERNAL_ID_RE.test(external()) || !what().trim()) { setError("A route needs a platform channel and what to deliver."); return }
          const items = what().split(/[\s,]+/).filter(Boolean)
          const body = mode() === "kinds" ? { kinds: items } : { topics: items }
          setError("")
          props.mutations.addRoute.mutate(
            { name: name(), body: { actor: actor(), external_id: external(), format: format(), ...body } },
            { onSuccess: () => setWhat(""), onError: (err) => setError(getErrorMessage(err)) },
          )
        }}
      >
        <Input aria-label="Route platform channel id" placeholder="Platform channel id" value={external()} onInput={(event) => setExternal(event.currentTarget.value.trim())} />
        <select class={selectClass} aria-label="Deliver by" value={mode()} onChange={(event) => setMode(event.currentTarget.value as "topics" | "kinds")}>
          <option value="topics">pub/sub topic patterns</option>
          <option value="kinds">event kinds</option>
        </select>
        <Input aria-label="What to deliver" placeholder={mode() === "kinds" ? "task.updated, proposal_created" : "ci.*.done, jobs.>"} value={what()} onInput={(event) => setWhat(event.currentTarget.value)} />
        <select class={selectClass} aria-label="Format" value={format()} onChange={(event) => setFormat(event.currentTarget.value as RouteFormat)}>
          <option value="line">line</option>
          <option value="embed">embed</option>
          <option value="card">card (buttons)</option>
        </select>
        <Button type="submit" size="sm">Add route</Button>
      </form>
      <Show when={error()}><p class="mt-2 text-xs text-rust" role="status">{error()}</p></Show>
    </section>
  )
}

function ConfigEditor(props: { snapshot: RelaySnapshot; mutations: Mutations }) {
  const actor = useActor()
  const config = () => props.snapshot.relay.config
  const [operators, setOperators] = createSignal<string | null>(null)
  const [actionUsers, setActionUsers] = createSignal<string | null>(null)
  const [readCommands, setReadCommands] = createSignal<string | null>(null)
  const [mask, setMask] = createSignal<boolean | null>(null)
  const [marks, setMarks] = createSignal<boolean | null>(null)
  const [message, setMessage] = createSignal("")
  const dirty = createMemo(() => [operators(), actionUsers(), readCommands(), mask(), marks()].some((value) => value !== null))
  const reset = () => { setOperators(null); setActionUsers(null); setReadCommands(null); setMask(null); setMarks(null) }
  return (
    <form
      class="mt-6 rounded-lg border border-line p-4"
      aria-label="Relay config"
      onSubmit={(event) => {
        event.preventDefault()
        const ops = operators() === null ? undefined : parseIdList(operators() ?? "")
        const users = actionUsers() === null ? undefined : parseIdList(actionUsers() ?? "")
        if (!actor()) { setMessage("Set a valid Name in the header first."); return }
        if ((ops && !validIds(ops)) || (users && !validIds(users))) { setMessage("Ids: letters, digits, - _ : ."); return }
        const changes: Record<string, unknown> = {}
        if (ops) changes.operators = ops
        if (users) changes.action_users = users
        if (readCommands() !== null) changes.read_commands = readCommands()
        if (mask() !== null) changes.mask = mask()
        if (marks() !== null) changes.status_marks = marks()
        props.mutations.update.mutate(
          { name: props.snapshot.relay.name, version: props.snapshot.version, changes: { actor: actor(), config: changes } },
          {
            onSuccess: () => { reset(); setMessage("Saved. The relay picks it up on its next heartbeat.") },
            onError: (err) => setMessage(getErrorMessage(err)),
          },
        )
      }}
    >
      <h3 class="font-display text-base text-cream">Who may do what</h3>
      <div class="mt-2 grid gap-3 sm:grid-cols-2">
        <label class="flex flex-col gap-1 text-xs text-cream-dim">Operators (platform user ids: bind, routes, threads)
          <Input value={operators() ?? config().operators.join(", ")} onInput={(event) => setOperators(event.currentTarget.value)} />
        </label>
        <label class="flex flex-col gap-1 text-xs text-cream-dim">Action users (buttons and reactions act)
          <Input value={actionUsers() ?? config().action_users.join(", ")} onInput={(event) => setActionUsers(event.currentTarget.value)} />
        </label>
        <label class="flex flex-col gap-1 text-xs text-cream-dim">Read commands (/fleet status, tasks…)
          <select class={selectClass} value={readCommands() ?? config().read_commands} onChange={(event) => setReadCommands(event.currentTarget.value)}>
            <option value="bound">anyone in a bound channel</option>
            <option value="operators">operators only</option>
          </select>
        </label>
        <div class="flex flex-col gap-2 text-sm text-cream">
          <label class="flex items-center gap-2"><input type="checkbox" checked={mask() ?? config().mask} onChange={(event) => setMask(event.currentTarget.checked)} /> Mask credentials in relayed text</label>
          <label class="flex items-center gap-2"><input type="checkbox" checked={marks() ?? config().status_marks} onChange={(event) => setMarks(event.currentTarget.checked)} /> Mark delivered messages</label>
        </div>
      </div>
      <div class="mt-3 flex items-center gap-2">
        <Button type="submit" size="sm" disabled={!dirty()}>Save (v{props.snapshot.version})</Button>
        <Show when={dirty()}><Button type="button" size="sm" variant="ghost" onClick={reset}>Discard</Button></Show>
        <Show when={message()}><span class="text-xs text-cream-dim" aria-live="polite">{message()}</span></Show>
      </div>
    </form>
  )
}

function AuditList(props: { name: string }) {
  const audit = useRelayAudit(() => true)
  const rows = () => relayAudit(audit.data?.audit ?? [], props.name)
  return (
    <ul class="mt-4 flex flex-col gap-2" aria-label="Audit">
      <For each={rows()} fallback={<li class="text-sm text-cream-dim">No changes recorded.</li>}>
        {(row) => (
          <li class="rounded border border-line p-2 text-sm">
            <span class="text-cream">{row.action}</span>
            <span class="text-cream-dim"> on {row.target} by </span>
            <span class="text-cream">{row.actor}</span>
            <span class="text-xs text-cream-dim" title={row.at}> · {formatStamp(row.at)}</span>
          </li>
        )}
      </For>
    </ul>
  )
}

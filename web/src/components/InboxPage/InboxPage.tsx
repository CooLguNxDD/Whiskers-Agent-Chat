import { createSignal, For, Show } from "solid-js"
import type { Mail, MailFilter } from "@/api/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { useAgents } from "@/hooks/useAgents"
import { useInboxHandle, useMailCounts, useMailList, useMailMutations } from "@/hooks/useMailbox"
import { usePrefs, useUIStore } from "@/store"
import { getErrorMessage } from "@/utils/errors"
import { isHandle } from "@/utils/handles"
import { formatStamp } from "@/utils/time"

type Mutations = ReturnType<typeof useMailMutations>

/** One handle's direct mail: unread/all lists, read and ack per row, and a composer. */
export default function InboxPage() {
  const ui = useUIStore()
  const prefs = usePrefs()
  const agents = useAgents()
  const handle = useInboxHandle()
  const [filter, setFilter] = createSignal<MailFilter>("unread")
  const counts = useMailCounts(handle)
  const list = useMailList(handle, filter)
  const mutations = useMailMutations(handle)
  const rows = () => list.data?.pages.flatMap((page) => page.mail) ?? []
  const [actionError, setActionError] = createSignal("")
  const fail = (reason: unknown) => setActionError(getErrorMessage(reason, "The mailbox did not accept that."))

  return (
    <div class="min-h-0 flex-1 overflow-y-auto p-4">
      <div class="mx-auto max-w-5xl">
        <h1 class="font-display text-xl text-cream">Inbox</h1>
        <p class="mt-1 text-xs text-cream-dim">
          Anyone with the hub token can open any handle's inbox; the hub does not yet tie a mailbox to its caller.
        </p>
        <div class="mt-3 flex flex-wrap items-center gap-2 text-sm">
          <label class="flex items-center gap-2 text-xs text-cream-dim">Handle
            <Input
              class="h-8 w-44"
              aria-label="Inbox handle"
              list="inbox-handles"
              placeholder={prefs.displayName() || "agent-handle"}
              value={ui.inboxHandle()}
              onInput={(event) => ui.setInboxHandle(event.currentTarget.value.trim())}
            />
          </label>
          <datalist id="inbox-handles">
            <For each={agents.data?.agents ?? []}>{(agent) => <option value={agent.name} />}</For>
          </datalist>
          <Show when={counts.data}>
            {(current) => (
              <span class="text-xs text-cream-dim">{current().unread} unread · {current().unacked} unacked · {current().total} total</span>
            )}
          </Show>
          <div role="tablist" aria-label="Mail filter" class="ml-auto flex gap-1">
            <For each={["unread", "all"] as MailFilter[]}>
              {(item) => (
                <Button type="button" role="tab" size="sm" variant={filter() === item ? "default" : "outline"} aria-selected={filter() === item} onClick={() => setFilter(item)}>
                  {item}
                </Button>
              )}
            </For>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={rows().length === 0 || mutations.readAll.isPending}
              onClick={() => { setActionError(""); mutations.readAll.mutate(Math.max(...rows().map((row) => row.id)), { onError: fail }) }}
            >
              Mark all read
            </Button>
          </div>
        </div>

        <Show when={handle()} fallback={<p class="mt-4 text-sm text-cream-dim">Pick a handle, or set a valid display name, to open an inbox.</p>}>
          <div class="mt-4 grid gap-6 lg:grid-cols-[1fr_20rem]">
            <section aria-label="Mail">
              <Show when={actionError()}><p class="mb-2 text-sm text-rust">{actionError()}</p></Show>
              <Show when={list.isError}><p class="text-sm text-rust">{getErrorMessage(list.error, "Could not load mail.")}</p></Show>
              <Show when={list.isPending && !list.isError}><p class="text-sm text-cream-dim">Loading…</p></Show>
              <Show when={list.isSuccess && rows().length === 0}>
                <p class="text-sm text-cream-dim">{filter() === "unread" ? "No unread mail." : "No mail yet."}</p>
              </Show>
              <ul class="flex flex-col gap-2">
                <For each={rows()}>{(mail) => <MailRow mail={mail} mutations={mutations} onError={fail} />}</For>
              </ul>
              <Show when={list.hasNextPage}>
                <Button type="button" class="mt-3" variant="outline" size="sm" disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>
                  Load more
                </Button>
              </Show>
            </section>
            <Compose sender={handle()} mutations={mutations} />
          </div>
        </Show>
      </div>
    </div>
  )
}

function MailRow(props: { mail: Mail; mutations: Mutations; onError: (reason: unknown) => void }) {
  return (
    <li class="rounded-lg border p-3" classList={{ "border-brass": props.mail.state === "unread", "border-line": props.mail.state !== "unread" }}>
      <div class="flex flex-wrap items-center gap-2 text-xs text-cream-dim">
        <span class="text-sm text-cream">{props.mail.sender}</span>
        <Badge>{props.mail.state}</Badge>
        <span title={props.mail.created_at}>{formatStamp(props.mail.created_at)}</span>
        <div class="ml-auto flex gap-1">
          <Show when={props.mail.state === "unread"}>
            <Button type="button" size="sm" variant="outline" disabled={props.mutations.read.isPending} onClick={() => props.mutations.read.mutate([props.mail.id], { onError: props.onError })}>
              Mark read
            </Button>
          </Show>
          <Show when={props.mail.state !== "acked"}>
            <Button type="button" size="sm" variant="outline" disabled={props.mutations.ack.isPending} onClick={() => props.mutations.ack.mutate([props.mail.id], { onError: props.onError })}>
              Ack
            </Button>
          </Show>
        </div>
      </div>
      <Show when={props.mail.subject}><p class="mt-1 text-sm font-medium text-cream">{props.mail.subject}</p></Show>
      <p class="mt-1 whitespace-pre-wrap break-words text-sm text-cream">{props.mail.body}</p>
    </li>
  )
}

function Compose(props: { sender: string; mutations: Mutations }) {
  const agents = useAgents()
  const [recipient, setRecipient] = createSignal("")
  const [subject, setSubject] = createSignal("")
  const [body, setBody] = createSignal("")
  const [requestId, setRequestId] = createSignal(crypto.randomUUID())
  const [note, setNote] = createSignal("")
  const [error, setError] = createSignal("")

  return (
    <form
      class="flex flex-col gap-2 rounded-lg border border-line p-3"
      aria-label="New direct message"
      onSubmit={(event) => {
        event.preventDefault()
        setError("")
        setNote("")
        if (!isHandle(recipient().trim())) { setError("Enter a valid recipient handle."); return }
        if (!body().trim()) { setError("Write a message."); return }
        props.mutations.send.mutate(
          { sender: props.sender, recipient: recipient().trim(), subject: subject().trim(), body: body(), client_request_id: requestId() },
          {
            onSuccess: () => {
              setSubject("")
              setBody("")
              setRequestId(crypto.randomUUID())
              setNote(`Sent to ${recipient().trim()}.`)
            },
            onError: (reason) => setError(getErrorMessage(reason, "Not sent. Your draft is still here.")),
          },
        )
      }}
    >
      <h2 class="font-display text-lg text-cream">Direct message</h2>
      <p class="text-xs text-cream-dim">From {props.sender}</p>
      <Input aria-label="Recipient" list="inbox-recipients" placeholder="Recipient handle" value={recipient()} onInput={(event) => setRecipient(event.currentTarget.value)} />
      <datalist id="inbox-recipients">
        <For each={agents.data?.agents ?? []}>{(agent) => <option value={agent.name} />}</For>
      </datalist>
      <Input aria-label="Subject" placeholder="Subject (optional)" maxLength={200} value={subject()} onInput={(event) => setSubject(event.currentTarget.value)} />
      <Textarea aria-label="Message" placeholder="Message" value={body()} onInput={(event) => setBody(event.currentTarget.value)} />
      <div class="flex justify-end"><Button type="submit" disabled={props.mutations.send.isPending}>Send</Button></div>
      <Show when={error()}><p class="text-sm text-rust">{error()}</p></Show>
      <Show when={note()}><p class="text-xs text-cream-dim" role="status">{note()}</p></Show>
    </form>
  )
}

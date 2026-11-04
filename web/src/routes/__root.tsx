import { createEffect, createSignal, For, Show, type ParentProps } from "solid-js"
import { A } from "@solidjs/router"
import { setTokenGetter } from "@/api/client"
import ConnectionPill from "@/components/ConnectionPill"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Modal } from "@/components/ui/dialog"
import { usePrefs, useSession } from "@/store"
import { isHandle } from "@/utils/handles"
import Appearance from "@/components/Appearance"
import InboxBadge from "@/components/InboxPage/InboxBadge"

const NAV = [
  { href: "/", label: "Chat" },
  { href: "/tasks", label: "Tasks" },
  { href: "/feed", label: "Feed" },
  { href: "/inbox", label: "Inbox" },
  { href: "/knowledge", label: "Knowledge" },
  { href: "/proposals", label: "Proposals" },
  { href: "/pubsub", label: "Pub/Sub" },
  { href: "/search", label: "Search" },
  { href: "/webhooks", label: "Webhooks" },
  { href: "/relays", label: "Relays" },
] as const

export function RootLayout(props: ParentProps) {
  const prefs = usePrefs()
  const session = useSession()
  const [tokenOpen, setTokenOpen] = createSignal(false)
  const [appearanceOpen, setAppearanceOpen] = createSignal(false)
  const [dismissedPrompt, setDismissedPrompt] = createSignal(false)
  const [draftToken, setDraftToken] = createSignal("")
  const showToken = () => tokenOpen() || (session.tokenRequired() && !dismissedPrompt())

  createEffect(() => setTokenGetter(() => session.token()))
  createEffect(() => { if (showToken()) setAppearanceOpen(false) })

  return (
    <>
      <div class="flex h-full min-h-0 flex-col">
        <header class="topbar flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line px-4 py-2">
          <A href="/" class="flex items-center gap-2 font-display text-lg tracking-tight text-cream">
            <span class="brand-mark flex size-8 items-center justify-center rounded-xl text-base" aria-hidden="true">🐾</span>
            Cat Fleet
          </A>
          <nav class="nav-tabs -mx-1 flex max-w-full items-center gap-0.5 overflow-x-auto px-1 text-sm" aria-label="Sections">
            <For each={NAV}>
              {(item) => (
                <A href={item.href} end={item.href === "/"} class="nav-tab whitespace-nowrap rounded-lg px-2.5 py-1.5" inactiveClass="text-cream-dim hover:bg-ink-raised hover:text-cream" activeClass="is-active text-cream">
                  {item.label}<Show when={item.href === "/inbox"}><InboxBadge /></Show>
                </A>
              )}
            </For>
          </nav>
          <div class="ml-auto flex flex-wrap items-center gap-2">
            <ConnectionPill />
            <label class="name-field flex h-8 items-center gap-2 rounded-lg border border-line bg-ink-sunken pl-2.5 text-xs text-cream-dim focus-within:border-brass">
              <span aria-hidden="true">🐱</span>
              <input class="h-full w-32 bg-transparent pr-2 text-cream outline-none placeholder:text-cream-dim" value={prefs.displayName()} aria-label="Display name" placeholder="your-handle" onInput={(event) => prefs.setDisplayName(event.currentTarget.value)} />
            </label>
            <ShowHandleWarning value={prefs.displayName()} />
            <Button type="button" variant="outline" size="sm" onClick={() => setAppearanceOpen(true)}>Appearance</Button>
            <Button type="button" variant="outline" size="sm" onClick={() => { setDismissedPrompt(false); setTokenOpen(true) }}>
              {session.token() ? "Token set" : "Token"}
            </Button>
          </div>
        </header>
        {props.children}
      </div>
      <Modal open={showToken()} onOpenChange={(next) => { setTokenOpen(next); if (!next) setDismissedPrompt(true) }} title="Hub token">
        <form class="flex flex-col gap-3" onSubmit={(event) => { event.preventDefault(); session.setToken(draftToken().trim()); setDraftToken(""); setTokenOpen(false) }}>
          <p class="text-sm text-cream-dim">Kept in memory for this tab only. It is not saved in the browser and not baked into the app.</p>
          <Input type="password" aria-label="Hub token" value={draftToken()} autocomplete="off" onInput={(event) => setDraftToken(event.currentTarget.value)} />
          <div class="flex justify-end gap-2">
            {session.token() && <Button type="button" variant="ghost" onClick={() => { session.setToken(""); setTokenOpen(false) }}>Clear</Button>}
            <Button type="submit">Use token</Button>
          </div>
        </form>
      </Modal>
      <Appearance open={appearanceOpen() && !showToken()} onOpenChange={setAppearanceOpen} />    </>
  )
}

function ShowHandleWarning(props: { value: string }) {
  return !isHandle(props.value.trim()) && props.value.trim() !== "" ? <span class="text-xs text-rust">Handle looks invalid</span> : null
}

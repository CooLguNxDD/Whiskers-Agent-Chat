import { createEffect, createSignal, For, on, onCleanup, Show, type ParentProps } from "solid-js"
import { A, useLocation, useNavigate } from "@solidjs/router"
import { setTokenGetter } from "@/api/client"
import ChannelList from "@/components/ChannelList"
import ConnectionPill from "@/components/ConnectionPill"
import { Avatar } from "@/components/ui/avatar"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Modal } from "@/components/ui/dialog"
import { usePrefs, useSession, useUIStore } from "@/store"
import { isHandle } from "@/utils/handles"
import Appearance from "@/components/Appearance"
import InboxBadge from "@/components/InboxPage/InboxBadge"

type NavItem = { href: string; label: string; icon: string }

/** Everyday destinations. Search is the top-bar button / shortcut, not a tab. */
const PRIMARY: NavItem[] = [
  { href: "/", label: "Chat", icon: "💬" },
  { href: "/tasks", label: "Tasks", icon: "☑" },
  { href: "/inbox", label: "Inbox", icon: "✉" },
  { href: "/feed", label: "Feed", icon: "≋" },
  { href: "/knowledge", label: "Knowledge", icon: "📖" },
  { href: "/proposals", label: "Proposals", icon: "⚡" },
]

/** Developer plumbing, folded under one group. */
const INTEGRATIONS: NavItem[] = [
  { href: "/pubsub", label: "Pub/Sub", icon: "⇄" },
  { href: "/webhooks", label: "Webhooks", icon: "⚓" },
  { href: "/relays", label: "Relays", icon: "⇌" },
]

const TITLES: Record<string, string> = Object.fromEntries(
  [...PRIMARY, ...INTEGRATIONS, { href: "/search", label: "Search", icon: "" }].map((item) => [item.href, item.label]),
)

function isTyping(target: EventTarget | null) {
  const element = target as HTMLElement | null
  return Boolean(element && (element.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName)))
}

function NavLink(props: { item: NavItem; onNavigate?: () => void }) {
  return (
    <A
      href={props.item.href}
      end={props.item.href === "/"}
      class="nav-item flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm"
      inactiveClass="text-cream-dim hover:bg-ink-raised hover:text-cream"
      activeClass="is-active font-medium text-cream"
      onClick={() => props.onNavigate?.()}
    >
      <span class="w-4 text-center text-[13px]" aria-hidden="true">{props.item.icon}</span>
      <span class="flex-1">{props.item.label}</span>
      <Show when={props.item.href === "/inbox"}><InboxBadge /></Show>
    </A>
  )
}

/**
 * App shell: a left sidebar (primary nav, channels, folded Integrations,
 * profile) and a slim top bar (breadcrumb, connection, search). On narrow
 * screens the sidebar becomes a drawer. Ctrl/⌘+K opens Search on every page
 * except Chat, where it finds in the channel.
 */
export function RootLayout(props: ParentProps) {
  const ui = useUIStore()
  const prefs = usePrefs()
  const session = useSession()
  const location = useLocation()
  const navigate = useNavigate()
  const [tokenOpen, setTokenOpen] = createSignal(false)
  const [appearanceOpen, setAppearanceOpen] = createSignal(false)
  const [dismissedPrompt, setDismissedPrompt] = createSignal(false)
  const [draftToken, setDraftToken] = createSignal("")
  const [drawer, setDrawer] = createSignal(false)
  const onIntegration = () => INTEGRATIONS.some((item) => location.pathname.startsWith(item.href))
  const [integrationsOpen, setIntegrationsOpen] = createSignal(onIntegration())
  const showToken = () => tokenOpen() || (session.tokenRequired() && !dismissedPrompt())
  const onChat = () => location.pathname === "/"

  createEffect(() => setTokenGetter(() => session.token()))
  createEffect(() => { if (showToken()) setAppearanceOpen(false) })
  createEffect(on(() => location.pathname, () => { setDrawer(false); if (onIntegration()) setIntegrationsOpen(true) }))
  createEffect(on(ui.activeChannel, () => setDrawer(false), { defer: true }))

  const onKey = (event: KeyboardEvent) => {
    if (onChat()) return
    const combo = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k"
    const slash = event.key === "/" && !event.ctrlKey && !event.metaKey && !isTyping(event.target)
    if ((combo || slash) && location.pathname !== "/search") {
      event.preventDefault()
      navigate("/search")
    }
  }
  document.addEventListener("keydown", onKey)
  onCleanup(() => document.removeEventListener("keydown", onKey))

  const handle = () => prefs.displayName().trim()

  return (
    <>
      <div class="app-shell flex h-full min-h-0">
        <Show when={drawer()}>
          <div class="fixed inset-0 z-30 bg-black/40 md:hidden" aria-hidden="true" onClick={() => setDrawer(false)} />
        </Show>
        <aside
          class="app-sidebar fixed inset-y-0 left-0 z-40 flex w-64 shrink-0 -translate-x-full flex-col border-r border-line transition-transform md:static md:translate-x-0"
          classList={{ "translate-x-0": drawer() }}
          aria-label="Main"
        >
          <div class="flex items-center gap-2.5 px-4 pb-3 pt-4">
            <span class="brand-mark flex size-9 items-center justify-center rounded-xl text-lg" aria-hidden="true">🐾</span>
            <A href="/" class="font-display text-xl font-semibold tracking-tight text-cream">Cat Fleet</A>
          </div>
          <div class="min-h-0 flex-1 overflow-y-auto pb-2">
            <nav class="flex flex-col gap-0.5 px-2" aria-label="Sections">
              <For each={PRIMARY}>{(item) => <NavLink item={item} />}</For>
            </nav>
            <div class="mt-5"><ChannelList /></div>
            <div class="mt-3 px-2">
              <button
                type="button"
                class="side-label flex w-full items-center justify-between rounded-md px-1 py-1 hover:text-cream"
                aria-expanded={integrationsOpen()}
                onClick={() => setIntegrationsOpen((open) => !open)}
              >
                Integrations <span aria-hidden="true">{integrationsOpen() ? "▾" : "▸"}</span>
              </button>
              <Show when={integrationsOpen()}>
                <nav class="mt-1 flex flex-col gap-0.5" aria-label="Integrations">
                  <For each={INTEGRATIONS}>{(item) => <NavLink item={item} />}</For>
                </nav>
              </Show>
            </div>
          </div>
          <div class="border-t border-line px-3 py-3">
            <div class="mb-2 flex items-center justify-between px-1"><ConnectionPill /></div>
            <div class="profile-card flex items-center gap-2.5 rounded-xl px-2 py-2">
              <Avatar name={handle() || "?"} size="sm" />
              <label class="min-w-0 flex-1">
                <span class="sr-only">Display name</span>
                <input
                  class="w-full rounded bg-transparent text-sm font-medium text-cream outline-none placeholder:text-cream-dim focus-visible:ring-1 focus-visible:ring-brass"
                  value={prefs.displayName()}
                  aria-label="Display name"
                  placeholder="Set your handle"
                  onInput={(event) => prefs.setDisplayName(event.currentTarget.value)}
                />
                <ShowHandleWarning value={prefs.displayName()} />
                <Show when={isHandle(handle())}><span class="block truncate text-xs text-cream-dim">Fleet commander</span></Show>
              </label>
              <button type="button" class="icon-btn" title="Appearance" aria-label="Appearance" onClick={() => setAppearanceOpen(true)}>🎨</button>
              <button
                type="button"
                class="icon-btn"
                classList={{ "text-moss": Boolean(session.token()) }}
                title={session.token() ? "Hub token set" : "Hub token"}
                aria-label={session.token() ? "Hub token set" : "Hub token"}
                onClick={() => { setDismissedPrompt(false); setTokenOpen(true) }}
              >🔑</button>
            </div>
          </div>
        </aside>
        <div class="flex min-h-0 min-w-0 flex-1 flex-col">
          <header class="topbar flex h-14 shrink-0 items-center gap-3 border-b border-line px-4">
            <button type="button" class="icon-btn md:hidden" aria-label="Open menu" onClick={() => setDrawer(true)}>☰</button>
            <nav class="flex min-w-0 items-center gap-2 text-sm" aria-label="Breadcrumb">
              <span class="hidden text-cream-dim sm:inline">Workspace</span>
              <span class="hidden text-cream-dim sm:inline" aria-hidden="true">/</span>
              <span class="truncate font-medium text-cream">{TITLES[location.pathname] ?? "Cat Fleet"}</span>
              <Show when={onChat()}><span class="truncate text-cream-dim">#{ui.activeChannel()}</span></Show>
            </nav>
            <div class="ml-auto flex items-center gap-2">
              <A href="/search" class="search-trigger flex h-8 items-center gap-2 rounded-lg border border-line bg-ink-sunken px-2.5 text-sm text-cream-dim hover:border-brass hover:text-cream" title="Search all history">
                <span aria-hidden="true">⌕</span><span class="hidden sm:inline">Search</span>
                <Show when={!onChat()}><kbd class="kbd hidden sm:inline-block">Ctrl K</kbd></Show>
              </A>
            </div>
          </header>
          {props.children}
        </div>
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
      <Appearance open={appearanceOpen() && !showToken()} onOpenChange={setAppearanceOpen} />
    </>
  )
}

function ShowHandleWarning(props: { value: string }) {
  return !isHandle(props.value.trim()) && props.value.trim() !== "" ? <span class="block text-xs text-rust">Handle looks invalid</span> : null
}

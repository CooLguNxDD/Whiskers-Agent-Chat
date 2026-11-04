import { createEffect, createSignal, For, on, onCleanup, Show, type JSX, type ParentProps } from "solid-js"
import { A, useLocation, useNavigate } from "@solidjs/router"
import { setTokenGetter } from "@/api/client"
import ChannelList from "@/components/ChannelList"
import { Avatar } from "@/components/ui/avatar"
import { Button } from "@/components/ui/button"
import { Icon, type IconName } from "@/components/ui/icons"
import { Input } from "@/components/ui/input"
import { Modal } from "@/components/ui/dialog"
import { INFRA_PATHS } from "@/components/ui/page"
import { useFleetStream } from "@/hooks/useFleetStream"
import { useProposals } from "@/hooks/useProposals"
import { useTasks } from "@/hooks/useTasks"
import { usePrefs, useSession, useUIStore } from "@/store"
import { isHandle } from "@/utils/handles"
import Appearance from "@/components/Appearance"
import InboxBadge from "@/components/InboxPage/InboxBadge"

type NavItem = { href: string; label: string; icon: IconName }

const PRIMARY: NavItem[] = [
  { href: "/", label: "Chat", icon: "chat" },
  { href: "/tasks", label: "Tasks", icon: "tasks" },
  { href: "/inbox", label: "Inbox", icon: "inbox" },
  { href: "/feed", label: "Feed", icon: "feed" },
  { href: "/knowledge", label: "Knowledge", icon: "knowledge" },
  { href: "/proposals", label: "Proposals", icon: "proposals" },
  { href: "/search", label: "Search", icon: "search" },
]

const TITLES: Record<string, string> = {
  ...Object.fromEntries(PRIMARY.map((item) => [item.href, item.label])),
  ...Object.fromEntries(INFRA_PATHS.map((href) => [href, "Infrastructure"])),
}

const STATUS_TEXT = {
  connecting: "Connecting…",
  connected: "Fleet connected",
  disconnected: "Reconnecting…",
  unauthorized: "Needs a hub token",
} as const

function isTyping(target: EventTarget | null) {
  const element = target as HTMLElement | null
  return Boolean(element && (element.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName)))
}

function NavLink(props: { href: string; label: string; icon: IconName; active?: boolean; count?: JSX.Element }) {
  return (
    <A
      href={props.href}
      end={props.href === "/"}
      class="nav-item flex items-center gap-3 rounded-xl px-3 py-2 text-[14.5px]"
      classList={{ "is-active font-medium text-cream": props.active }}
      inactiveClass={props.active ? "" : "text-cream-dim hover:bg-ink-raised hover:text-cream"}
      activeClass="is-active font-medium text-cream"
    >
      <Icon name={props.icon} />
      <span class="flex-1">{props.label}</span>
      {props.count}
    </A>
  )
}

function NavCount(props: { value: number | undefined }) {
  return <Show when={props.value}><span class="nav-count">{props.value}</span></Show>
}

/**
 * App shell: a left sidebar (brand, primary nav with counts, channels,
 * Infrastructure, connection, profile) and a slim top bar (breadcrumb, fleet
 * status, search, avatar). On narrow screens the sidebar becomes a drawer.
 * Ctrl/⌘+K opens Search on every page except Chat, where it finds in the channel.
 */
export function RootLayout(props: ParentProps) {
  const ui = useUIStore()
  const prefs = usePrefs()
  const session = useSession()
  const stream = useFleetStream()
  const location = useLocation()
  const navigate = useNavigate()
  const tasks = useTasks(ui.activeChannel)
  const proposals = useProposals(() => "")
  const [tokenOpen, setTokenOpen] = createSignal(false)
  const [appearanceOpen, setAppearanceOpen] = createSignal(false)
  const [dismissedPrompt, setDismissedPrompt] = createSignal(false)
  const [draftToken, setDraftToken] = createSignal("")
  const [drawer, setDrawer] = createSignal(false)
  const showToken = () => tokenOpen() || (session.tokenRequired() && !dismissedPrompt())
  const onChat = () => location.pathname === "/"
  const onInfra = () => INFRA_PATHS.includes(location.pathname)
  const activeTasks = () => (tasks.data?.tasks ?? []).filter((task) => ["open", "claimed", "in_progress", "blocked"].includes(task.status)).length
  const openProposals = () => (proposals.data?.proposals ?? []).filter((item) => item.status === "open").length
  const handle = () => prefs.displayName().trim()

  createEffect(() => setTokenGetter(() => session.token()))
  createEffect(() => { if (showToken()) setAppearanceOpen(false) })
  createEffect(on(() => location.pathname, () => setDrawer(false)))
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

  return (
    <>
      <div class="app-shell flex h-full min-h-0">
        <Show when={drawer()}>
          <div class="fixed inset-0 z-30 bg-black/40 md:hidden" aria-hidden="true" onClick={() => setDrawer(false)} />
        </Show>
        <aside
          class="app-sidebar fixed inset-y-0 left-0 z-40 flex w-[248px] shrink-0 -translate-x-full flex-col border-r border-line transition-transform md:static md:translate-x-0"
          classList={{ "translate-x-0": drawer() }}
          aria-label="Main"
        >
          <A href="/" class="flex items-center gap-3 px-6 pb-5 pt-6">
            <span class="brand-mark flex size-9 items-center justify-center rounded-xl text-lg" aria-hidden="true">🐾</span>
            <span class="text-xl font-bold tracking-tight text-cream">Cat Fleet</span>
          </A>
          <div class="min-h-0 flex-1 overflow-y-auto px-4 pb-3">
            <nav class="flex flex-col gap-0.5" aria-label="Sections">
              <For each={PRIMARY}>
                {(item) => (
                  <NavLink
                    {...item}
                    count={
                      item.href === "/tasks" ? <NavCount value={activeTasks()} />
                        : item.href === "/proposals" ? <NavCount value={openProposals()} />
                          : item.href === "/inbox" ? <InboxBadge /> : undefined
                    }
                  />
                )}
              </For>
            </nav>
            <div class="mt-7"><ChannelList /></div>
            <div class="mt-6">
              <h2 class="side-label mb-2 px-3">Workspace</h2>
              <NavLink href="/pubsub" label="Infrastructure" icon="infra" active={onInfra()} />
            </div>
          </div>
          <div class="border-t border-line px-4 pb-4 pt-3">
            <p class="mb-3 flex items-center gap-2 px-2 text-xs text-cream-dim" title={stream.reconnects() ? `reconnects observed: ${stream.reconnects()}` : undefined}>
              <span class="status-dot size-2 rounded-full" data-status={stream.status()} aria-hidden="true" />
              {STATUS_TEXT[stream.status()]}
            </p>
            <div class="flex items-center gap-3 px-2">
              <Avatar name={handle() || "?"} />
              <label class="min-w-0 flex-1">
                <span class="sr-only">Display name</span>
                <input
                  class="w-full rounded bg-transparent text-sm font-semibold text-cream outline-none placeholder:font-normal placeholder:text-cream-dim focus-visible:ring-1 focus-visible:ring-brass"
                  value={prefs.displayName()}
                  aria-label="Display name"
                  placeholder="Set your handle"
                  onInput={(event) => prefs.setDisplayName(event.currentTarget.value)}
                />
                <ShowHandleWarning value={prefs.displayName()} />
                <Show when={isHandle(handle())}><span class="block truncate text-xs text-cream-dim">Fleet commander</span></Show>
              </label>
              <button type="button" class="icon-btn" title="Appearance" aria-label="Appearance" onClick={() => setAppearanceOpen(true)}><Icon name="sun" /></button>
              <button
                type="button"
                class="icon-btn"
                classList={{ "text-moss": Boolean(session.token()) }}
                title={session.token() ? "Hub token set" : "Hub token"}
                aria-label={session.token() ? "Hub token set" : "Hub token"}
                onClick={() => { setDismissedPrompt(false); setTokenOpen(true) }}
              ><Icon name="key" /></button>
            </div>
          </div>
        </aside>
        <div class="flex min-h-0 min-w-0 flex-1 flex-col">
          <header class="topbar flex h-[72px] shrink-0 items-center gap-3 border-b border-line px-5 sm:px-8">
            <button type="button" class="icon-btn md:hidden" aria-label="Open menu" onClick={() => setDrawer(true)}><Icon name="menu" /></button>
            <nav class="flex min-w-0 items-center gap-2.5 text-sm" aria-label="Breadcrumb">
              <span class="hidden text-cream-dim sm:inline">Workspace</span>
              <span class="hidden text-cream-dim sm:inline" aria-hidden="true">/</span>
              <span class="truncate font-semibold text-cream">{TITLES[location.pathname] ?? "Cat Fleet"}</span>
              <Show when={onChat()}><span class="truncate text-cream-dim">#{ui.activeChannel()}</span></Show>
            </nav>
            <div class="ml-auto flex items-center gap-3">
              <span class="status-pill hidden items-center gap-2 rounded-full px-3 py-1 text-xs sm:inline-flex" data-status={stream.status()}>
                <span class="size-1.5 rounded-full bg-current" aria-hidden="true" />
                {stream.status() === "connected" ? "All systems operational" : STATUS_TEXT[stream.status()]}
              </span>
              <A href="/search" class="icon-btn" title="Search (Ctrl K)" aria-label="Search"><Icon name="search" /></A>
              <button type="button" class="rounded-full" title="Profile and appearance" aria-label="Profile and appearance" onClick={() => setAppearanceOpen(true)}>
                <Avatar name={handle() || "?"} size="sm" />
              </button>
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

import { createEffect, createSignal, type ParentProps } from "solid-js"
import { A } from "@solidjs/router"
import { setTokenGetter } from "@/api/client"
import ConnectionPill from "@/components/ConnectionPill"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Modal } from "@/components/ui/dialog"
import { usePrefs, useSession, useUIStore } from "@/store"
import { isHandle } from "@/utils/handles"
import Appearance from "@/components/Appearance"

export function RootLayout(props: ParentProps) {
  const ui = useUIStore()
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
        <header class="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
          <A href="/" class="font-display text-xl tracking-tight text-cream">Cat Fleet</A>
          <span class="text-sm text-cream-dim">#{ui.activeChannel()}</span>
          <nav class="flex items-center gap-3 text-sm">
            <A href="/" inactiveClass="text-cream-dim hover:text-cream" activeClass="text-brass">Chat</A>
            <A href="/tasks" inactiveClass="text-cream-dim hover:text-cream" activeClass="text-brass">Tasks</A>
          </nav>
          <div class="ml-auto flex flex-wrap items-center gap-2">
            <ConnectionPill />
            <label class="flex items-center gap-2 text-xs text-cream-dim">Name
              <Input class="h-8 w-36" value={prefs.displayName()} aria-label="Display name" placeholder="your-handle" onInput={(event) => prefs.setDisplayName(event.currentTarget.value)} />
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
      <Appearance open={appearanceOpen() && !showToken()} onOpenChange={setAppearanceOpen} />
    </>
  )
}

function ShowHandleWarning(props: { value: string }) {
  return !isHandle(props.value.trim()) && props.value.trim() !== "" ? <span class="text-xs text-rust">Handle looks invalid</span> : null
}

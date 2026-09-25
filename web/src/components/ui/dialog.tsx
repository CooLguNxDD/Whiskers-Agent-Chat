import { createEffect, createUniqueId, onCleanup, type ParentProps } from "solid-js"
import { Show } from "solid-js"
import { cn } from "@/lib/utils"

export function Modal(props: ParentProps<{
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
}>) {
  let content: HTMLDivElement | null = null
  const titleId = createUniqueId()

  createEffect(() => {
    if (!props.open) return
    const previous = document.activeElement as HTMLElement | null
    const focus = () => content?.querySelector<HTMLElement>("input, textarea, button, [tabindex]")?.focus()
    queueMicrotask(focus)
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") props.onOpenChange(false)
      if (event.key === "Tab" && content) {
        const focusable = [...content.querySelectorAll<HTMLElement>("button, input, textarea, [tabindex]:not([tabindex='-1'])")]
        if (focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault()
          last.focus()
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault()
          first.focus()
        }
      }
    }
    window.addEventListener("keydown", closeOnEscape)
    onCleanup(() => {
      window.removeEventListener("keydown", closeOnEscape)
      previous?.focus()
    })
  })

  return (
    <Show when={props.open}>
      <div class="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-4" onClick={() => props.onOpenChange(false)}>
        <div
          ref={(element) => { content = element }}
          class={cn("max-h-[calc(100dvh-2rem)] overflow-y-auto w-[min(32rem,calc(100vw-2rem))] rounded-[var(--radius-panel)] border border-line bg-ink-raised p-5 shadow-xl")}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          onClick={(event) => event.stopPropagation()}
        >
          <div class="flex items-start justify-between gap-3">
            <h2 id={titleId} class="font-display text-xl text-cream">{props.title}</h2>
            <button type="button" class="text-cream-dim hover:text-cream" aria-label="Close" onClick={() => props.onOpenChange(false)}>×</button>
          </div>
          <div class="mt-4">{props.children}</div>
        </div>
      </div>
    </Show>
  )
}

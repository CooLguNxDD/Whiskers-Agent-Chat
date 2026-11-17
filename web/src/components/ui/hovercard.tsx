import { createSignal, createUniqueId, onCleanup, Show, type JSX } from "solid-js"
import { Portal } from "solid-js/web"

export interface HoverCardProps {
  /** The card body; built only while open. */
  content: () => JSX.Element
  /** The trigger. It receives handlers and `aria-describedby`; make it focusable for keyboard users. */
  children: (trigger: HoverCardTrigger) => JSX.Element
  /** Preferred side; flips when there is no room. Default "left" (cards next to right-hand panels). */
  side?: "left" | "right"
  openDelay?: number
  closeDelay?: number
}

export interface HoverCardTrigger {
  ref: (element: HTMLElement) => void
  onPointerEnter: () => void
  onPointerLeave: () => void
  onFocusIn: () => void
  onFocusOut: () => void
  onKeyDown: (event: KeyboardEvent) => void
  "aria-describedby": string | undefined
}

const WIDTH = 288
const GAP = 10

/**
 * A read-only hover/focus card in a portal beside its trigger. Opens after `openDelay`, closes
 * on leave, blur or Escape. The card ignores the pointer (`pointer-events: none`) so it can never
 * block what it overlaps, such as a dock splitter; keep interactive controls out of it.
 */
export function HoverCard(props: HoverCardProps) {
  const id = `hovercard-${createUniqueId()}`
  const [position, setPosition] = createSignal<{ left: number; top: number } | null>(null)
  let trigger: HTMLElement | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  onCleanup(() => clearTimeout(timer))

  function place() {
    if (!trigger) return
    const rect = trigger.getBoundingClientRect()
    const viewport = document.documentElement.clientWidth || window.innerWidth
    const leftSide = rect.left - GAP - WIDTH
    const rightSide = rect.right + GAP
    const preferLeft = (props.side ?? "left") === "left"
    const left = preferLeft ? (leftSide >= 8 ? leftSide : rightSide) : (rightSide + WIDTH <= viewport - 8 ? rightSide : leftSide)
    const top = Math.max(8, Math.min(rect.top, (window.innerHeight || 0) - 220))
    setPosition({ left: Math.max(8, left), top })
  }

  const schedule = (open: boolean) => {
    clearTimeout(timer)
    timer = setTimeout(() => (open ? place() : setPosition(null)), open ? props.openDelay ?? 280 : props.closeDelay ?? 120)
  }

  const handlers: HoverCardTrigger = {
    ref: (element) => { trigger = element },
    onPointerEnter: () => schedule(true),
    onPointerLeave: () => schedule(false),
    onFocusIn: () => { clearTimeout(timer); place() },
    onFocusOut: () => schedule(false),
    onKeyDown: (event) => { if (event.key === "Escape" && position()) { event.stopPropagation(); clearTimeout(timer); setPosition(null) } },
    get "aria-describedby"() { return position() ? id : undefined },
  }

  return (
    <>
      {props.children(handlers)}
      <Show when={position()}>
        {(at) => (
          <Portal>
            <div
              id={id}
              role="tooltip"
              class="hovercard"
              style={{ left: `${at().left}px`, top: `${at().top}px`, width: `${WIDTH}px` }}
            >
              {props.content()}
            </div>
          </Portal>
        )}
      </Show>
    </>
  )
}

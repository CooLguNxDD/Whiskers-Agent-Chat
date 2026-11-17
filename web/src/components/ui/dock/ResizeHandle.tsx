import { onCleanup } from "solid-js"

export interface ResizeHandleProps {
  /** `row` drags horizontally (a vertical bar); `col` drags vertically. */
  dir: "row" | "col"
  label: string
  /** Current position for assistive tech, 0–100. */
  value: number
  onStart: () => void
  /** Cumulative pointer travel in px since `onStart`. */
  onMove: (deltaPx: number) => void
  onEnd: () => void
  /** Arrow-key nudge, -1 (left/up) or +1 (right/down). */
  onStep: (direction: -1 | 1) => void
  class?: string
}

/**
 * A draggable splitter bar (`role="separator"`). Pointer drags report cumulative travel; arrow
 * keys nudge. Used between dock split children and on the app sidebar edge.
 */
export function ResizeHandle(props: ResizeHandleProps) {
  let stop: (() => void) | undefined
  onCleanup(() => stop?.())

  function down(event: PointerEvent) {
    if (event.button !== 0) return
    event.preventDefault()
    const target = event.currentTarget as HTMLElement
    const origin = props.dir === "row" ? event.clientX : event.clientY
    target.setPointerCapture?.(event.pointerId)
    document.body.dataset.resizing = props.dir
    props.onStart()
    const move = (next: PointerEvent) => props.onMove((props.dir === "row" ? next.clientX : next.clientY) - origin)
    const up = () => stop?.()
    stop = () => {
      target.removeEventListener("pointermove", move)
      target.removeEventListener("pointerup", up)
      target.removeEventListener("pointercancel", up)
      delete document.body.dataset.resizing
      stop = undefined
      props.onEnd()
    }
    target.addEventListener("pointermove", move)
    target.addEventListener("pointerup", up)
    target.addEventListener("pointercancel", up)
  }

  function key(event: KeyboardEvent) {
    const back = props.dir === "row" ? "ArrowLeft" : "ArrowUp"
    const forward = props.dir === "row" ? "ArrowRight" : "ArrowDown"
    if (event.key !== back && event.key !== forward) return
    event.preventDefault()
    props.onStep(event.key === back ? -1 : 1)
  }

  return (
    <div
      role="separator"
      tabindex="0"
      aria-label={props.label}
      aria-orientation={props.dir === "row" ? "vertical" : "horizontal"}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(props.value)}
      class={`dock-resizer ${props.class ?? ""}`}
      data-dir={props.dir}
      onPointerDown={down}
      onKeyDown={key}
    />
  )
}

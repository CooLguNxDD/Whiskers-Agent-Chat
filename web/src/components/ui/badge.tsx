import type { JSX } from "solid-js"
import { cn } from "@/lib/utils"

export function Badge(props: JSX.HTMLAttributes<HTMLSpanElement>) {
  const { class: className, ...rest } = props
  return (
    <span
      class={cn(
        "inline-flex items-center rounded-full border border-line bg-ink-raised px-2 py-0.5 text-xs text-cream-dim",
        className,
      )}
      {...rest}
    />
  )
}

import { splitProps, type JSX } from "solid-js"
import { cn } from "@/lib/utils"

export function Input(props: JSX.InputHTMLAttributes<HTMLInputElement>) {
  const [local, rest] = splitProps(props, ["class", "onInput", "onChange"])
  return (
    <input
      class={cn(
        "h-9 w-full rounded-lg border border-line bg-ink-sunken px-3 text-sm text-cream placeholder:text-cream-dim focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brass",
        local.class,
      )}
      onInput={local.onInput}
      onChange={local.onChange}
      {...rest}
    />
  )
}

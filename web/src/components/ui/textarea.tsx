import { splitProps, type JSX } from "solid-js"
import { cn } from "@/lib/utils"

export function Textarea(props: JSX.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const [local, rest] = splitProps(props, ["class", "onInput", "onChange", "onKeyDown", "ref"])
  return (
    <textarea
      class={cn(
        "min-h-20 w-full resize-y rounded-lg border border-line bg-ink-sunken px-3 py-2 text-sm text-cream placeholder:text-cream-dim focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brass",
        local.class,
      )}
      ref={local.ref}
      onInput={local.onInput}
      onChange={local.onChange}
      onKeyDown={local.onKeyDown}
      {...rest}
    />
  )
}

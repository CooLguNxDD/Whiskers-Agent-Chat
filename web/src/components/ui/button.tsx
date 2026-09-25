import { cva, type VariantProps } from "class-variance-authority"
import { splitProps, type JSX } from "solid-js"
import { cn } from "@/lib/utils"

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 rounded-lg text-sm font-medium transition disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brass",
  {
    variants: {
      variant: {
        default: "bg-brass text-on-accent hover:bg-brass/90",
        ghost: "bg-transparent text-cream hover:bg-ink-raised",
        outline: "border border-line bg-ink-sunken text-cream hover:border-brass",
        danger: "bg-rust/15 text-rust hover:bg-rust/25",
      },
      size: {
        default: "h-9 px-3",
        sm: "h-8 px-2.5 text-xs",
        icon: "size-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
)

export function Button(props: JSX.ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof buttonVariants>) {
  const [local, rest] = splitProps(props, ["class", "variant", "size", "onClick", "onKeyDown", "onFocus", "onBlur"])
  return <button class={cn(buttonVariants({ variant: local.variant, size: local.size }), local.class)} onClick={local.onClick} onKeyDown={local.onKeyDown} onFocus={local.onFocus} onBlur={local.onBlur} {...rest} />
}

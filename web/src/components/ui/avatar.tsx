import { avatarHue, initials } from "@/utils/chatLayout"

/** Initials avatar with a stable per-author hue. Decorative; the name is always shown as text too. */
export function Avatar(props: { name: string; size?: "sm" | "md" }) {
  return (
    <span
      aria-hidden="true"
      class="avatar inline-flex shrink-0 select-none items-center justify-center rounded-xl font-semibold"
      classList={{ "size-9 text-xs": props.size !== "sm", "size-8 text-[11px]": props.size === "sm" }}
      style={{ "--avatar-hue": String(avatarHue(props.name)) }}
    >
      {initials(props.name)}
    </span>
  )
}

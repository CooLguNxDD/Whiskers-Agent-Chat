import { Show } from "solid-js"
import { useInboxHandle, useMailCounts } from "@/hooks/useMailbox"
import { unreadBadge } from "@/utils/unread"

/** Unread count next to the Inbox nav entry. Hidden at zero or without a handle. */
export default function InboxBadge() {
  const handle = useInboxHandle()
  const counts = useMailCounts(handle)
  const label = () => unreadBadge(counts.data?.unread)
  return (
    <Show when={label()}>
      {(text) => (
        <span class="ml-1 rounded-full bg-brass px-1.5 text-[10px] font-semibold text-on-accent" aria-label={`${counts.data?.unread} unread`}>
          {text()}
        </span>
      )}
    </Show>
  )
}

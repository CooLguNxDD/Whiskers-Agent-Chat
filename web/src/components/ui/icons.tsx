import type { JSX } from "solid-js"

/** Factories, not elements: a Solid JSX node can live in one place only, so each icon draws fresh paths. */
/** Line icons drawn on a 24px grid with a 1.75 stroke in currentColor. Decorative unless given a label. */
const PATHS = {
  chat: () => <path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12Z" />,
  tasks: () => <><rect x="3.5" y="3.5" width="17" height="17" rx="3" /><path d="m8 12 3 3 5-6" /></>,
  inbox: () => <><path d="M3 13h5l1.5 3h5L16 13h5" /><path d="M5.5 5h13L21 13v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5Z" /></>,
  feed: () => <path d="M13 2 4 14h7l-1 8 9-12h-7Z" />,
  knowledge: () => <><path d="M3 5.5A1.5 1.5 0 0 1 4.5 4H10a2 2 0 0 1 2 2v14a2 2 0 0 0-2-2H3Z" /><path d="M21 5.5A1.5 1.5 0 0 0 19.5 4H14a2 2 0 0 0-2 2v14a2 2 0 0 1 2-2h7Z" /></>,
  proposals: () => <><path d="M9 18h6" /><path d="M10 21h4" /><path d="M12 3a6 6 0 0 0-3.5 10.9c.6.4 1 1.1 1 1.8V16h5v-.3c0-.7.4-1.4 1-1.8A6 6 0 0 0 12 3Z" /></>,
  search: () => <><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></>,
  infra: () => <><rect x="3.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="3.5" y="13.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="13.5" width="7" height="7" rx="1.5" /></>,
  plus: () => <path d="M12 5v14M5 12h14" />,
  sun: () => <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></>,
  key: () => <><circle cx="8" cy="15" r="4" /><path d="m11 12 9-9M17 6l3 3M15 8l2 2" /></>,
  arrow: () => <path d="M7 17 17 7M8 7h9v9" />,
  menu: () => <path d="M4 7h16M4 12h16M4 17h16" />,
  archive: () => <><rect x="3" y="4" width="18" height="4" rx="1" /><path d="M5 8v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8M10 12h4" /></>,
  board: () => <><rect x="3.5" y="4" width="5" height="16" rx="1.5" /><rect x="9.5" y="4" width="5" height="11" rx="1.5" /><rect x="15.5" y="4" width="5" height="7" rx="1.5" /></>,
  list: () => <path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01" />,
  relay: () => <path d="M7 4 3 8l4 4M3 8h14M17 12l4 4-4 4M21 16H7" />,
  hook: () => <><path d="M18 16.5a3.5 3.5 0 1 1-5-3.2V7" /><circle cx="13" cy="5" r="2" /><path d="M6 16.5a3.5 3.5 0 0 0 6.5 1.8" /></>,
  signal: () => <path d="M2 12h4l3-8 6 16 3-8h4" />,
} satisfies Record<string, () => JSX.Element>

export type IconName = keyof typeof PATHS

export function Icon(props: { name: IconName; class?: string; label?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.75"
      stroke-linecap="round"
      stroke-linejoin="round"
      class={props.class ?? "size-[18px]"}
      aria-hidden={props.label ? undefined : "true"}
      aria-label={props.label}
      role={props.label ? "img" : undefined}
    >
      {PATHS[props.name]()}
    </svg>
  )
}

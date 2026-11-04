import { createSignal } from "solid-js"
import { DEFAULT_THEME_ID, themeRegistry } from "@/themes/registry"
import { accents, type Accent } from "@/themes/appearance"

export interface PrefsSlice {
  displayName: () => string
  setDisplayName: (name: string) => void
  theme: () => string
  accent: () => Accent
  setTheme: (value: string) => void
  setAccent: (value: Accent) => void
  /** Chat backdrop pattern, device-local. */
  chatBackground: () => ChatBackground
  setChatBackground: (value: ChatBackground) => void
}

export const chatBackgrounds = ["paws", "dots", "grid", "glow", "plain"] as const
export type ChatBackground = typeof chatBackgrounds[number]

let saved: string | null = null
try { saved = localStorage.getItem("cat-fleet-prefs") } catch { /* Storage may be unavailable. */ }
let initial = ""
let initialTheme = DEFAULT_THEME_ID
let initialAccent: Accent = "amber"
let initialBackground: ChatBackground = "paws"
if (saved) {
  try {
    const parsed = JSON.parse(saved) as { state?: { displayName?: unknown; theme?: string; accent?: Accent; chatBackground?: ChatBackground } }
    if (typeof parsed.state?.displayName === "string") initial = parsed.state.displayName
    if (parsed.state?.theme && Object.hasOwn(themeRegistry, parsed.state.theme)) initialTheme = parsed.state.theme
    if (parsed.state?.accent && accents.includes(parsed.state.accent)) initialAccent = parsed.state.accent
    if (parsed.state?.chatBackground && chatBackgrounds.includes(parsed.state.chatBackground)) initialBackground = parsed.state.chatBackground
  } catch {
    // An invalid preference should not prevent the portal from starting.
  }
}

const [displayName, setDisplayNameSignal] = createSignal(initial)
const [theme, setThemeSignal] = createSignal(initialTheme)
const [accent, setAccentSignal] = createSignal<Accent>(initialAccent)
const [chatBackground, setChatBackgroundSignal] = createSignal<ChatBackground>(initialBackground)

function persist() {
  try {
    localStorage.setItem("cat-fleet-prefs", JSON.stringify({ state: { displayName: displayName(), theme: theme(), accent: accent(), chatBackground: chatBackground() }, version: 0 }))
  } catch { /* Keep working in memory if storage is blocked. */ }
}

export const prefs: PrefsSlice = {
  displayName,
  theme,
  accent,
  setTheme(value) { setThemeSignal(Object.hasOwn(themeRegistry, value) ? value : DEFAULT_THEME_ID); persist() },
  setAccent(value) { setAccentSignal(accents.includes(value) ? value : "amber"); persist() },
  chatBackground,
  setChatBackground(value) { setChatBackgroundSignal(chatBackgrounds.includes(value) ? value : "paws"); persist() },
  setDisplayName(name) {
    setDisplayNameSignal(name)
    persist()
  },
}

export function usePrefs() {
  return prefs
}

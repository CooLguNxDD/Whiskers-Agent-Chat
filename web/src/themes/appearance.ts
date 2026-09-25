import { DEFAULT_THEME_ID, themeRegistry } from "./registry"

export const accents = ["amber", "pink", "neon", "cyan", "violet"] as const
export type Accent = typeof accents[number]
let appliedKeys = new Set<string>()

/** Apply base tokens separately from effective accents so CSS overrides remain effective. */
export function applyAppearance(theme: string, accent: Accent, root = document.documentElement) {
  const def = themeRegistry[theme] ?? themeRegistry[DEFAULT_THEME_ID]
  const keys = new Set(Object.keys(def.vars).map(key => `--theme-${key}`))
  for (const key of appliedKeys) if (!keys.has(key)) root.style.removeProperty(key)
  for (const [key, value] of Object.entries(def.vars)) {
    root.style.setProperty(`--theme-${key}`, value.replace(/var\(--([\w-]+)\)/g, "var(--theme-$1)"))
  }
  appliedKeys = keys
  root.dataset.theme = def.id
  root.dataset.light = String(def.isLight)
  root.dataset.accent = accent
  root.style.colorScheme = def.isLight ? "light" : "dark"
}

export interface ThemeDef {
  id: string
  label: string
  description?: string
  default?: boolean
  vars: Record<string, string>
  /** True when ``vars.bg`` OKLCH lightness is above 0.6. */
  isLight: boolean
}

/** Parse OKLCH lightness (L) from a CSS color string. */
export function oklchLightness(color: string | undefined | null): number | null {
  if (!color || typeof color !== "string") return null
  const m = color.trim().match(/^oklch\(\s*([0-9.]+)/i)
  if (!m) return null
  const L = Number(m[1])
  return Number.isFinite(L) ? L : null
}

function isLightFromVars(vars: Record<string, string>): boolean {
  const L = oklchLightness(vars.bg)
  return L != null && L > 0.6
}

export interface RawThemeFile {
  id: string
  label: string
  description?: string
  default?: boolean
  extends?: string
  vars: Record<string, string>
}

/**
 * Hand-rolled type guard validating a raw theme module.
 * It ensures id, label, and vars are present and of correct types,
 * and that all variables in the vars object are string values.
 * Optional fields extends, description, and default are validated if present.
 */
export function isValidRawTheme(obj: unknown): obj is RawThemeFile {
  if (typeof obj !== "object" || obj === null) {
    return false
  }

  const raw = obj as Record<string, unknown>

  if (typeof raw.id !== "string" || !raw.id) {
    return false
  }

  if (typeof raw.label !== "string" || !raw.label) {
    return false
  }

  if (typeof raw.vars !== "object" || raw.vars === null) {
    return false
  }

  // Validate vars: all keys must map to string values
  const vars = raw.vars as Record<string, unknown>
  for (const key of Object.keys(vars)) {
    if (typeof vars[key] !== "string") {
      return false
    }
  }

  if ("description" in raw && raw.description !== undefined && typeof raw.description !== "string") {
    return false
  }

  if ("default" in raw && raw.default !== undefined && typeof raw.default !== "boolean") {
    return false
  }

  if ("extends" in raw && raw.extends !== undefined && typeof raw.extends !== "string") {
    return false
  }

  return true
}

/**
 * Resolves the extends chain for a theme.
 * Recursively climbs the extends hierarchy to merge variable overrides.
 * Catches cyclic references and missing ancestor targets, logging via console.error.
 */
export function resolveThemeVars(
  themeId: string,
  rawThemes: Record<string, RawThemeFile>
): Record<string, string> {
  const visited = new Set<string>()
  const chain: RawThemeFile[] = []
  let currentId: string | undefined = themeId
  const maxDepth = 10

  while (currentId) {
    if (visited.has(currentId)) {
      console.error(
        `Cycle detected in theme extends chain starting from theme "${themeId}" involving theme "${currentId}".`
      )
      return rawThemes[themeId].vars
    }

    const currentTheme: RawThemeFile | undefined = rawThemes[currentId]
    if (!currentTheme) {
      console.error(
        `Theme "${currentId}" extended by another theme is missing in the registry.`
      )
      return rawThemes[themeId].vars
    }

    visited.add(currentId)
    chain.push(currentTheme)

    if (chain.length > maxDepth) {
      console.error(`Max inheritance depth reached resolving theme "${themeId}".`)
      return rawThemes[themeId].vars
    }

    currentId = currentTheme.extends
  }

  // Merge vars base-first (ancestors first, then overrides)
  let resolvedVars: Record<string, string> = {}
  for (let i = chain.length - 1; i >= 0; i--) {
    resolvedVars = { ...resolvedVars, ...chain[i].vars }
  }

  return resolvedVars
}

/**
 * Builds the final theme registry from a record of globbed modules.
 * Filters out malformed modules, resolves inheritance chains, and builds final ThemeDefs.
 */
export function buildRegistry(rawModules: Record<string, unknown>): Record<string, ThemeDef> {
  const rawThemes: Record<string, RawThemeFile> = {}

  for (const [path, mod] of Object.entries(rawModules)) {
    if (isValidRawTheme(mod)) {
      rawThemes[mod.id] = mod
    } else {
      console.error(`Theme file at ${path} is malformed or invalid.`)
    }
  }

  const registry: Record<string, ThemeDef> = {}

  for (const id of Object.keys(rawThemes)) {
    const rawTheme = rawThemes[id]
    const resolvedVars = resolveThemeVars(id, rawThemes)

    registry[id] = {
      id: rawTheme.id,
      label: rawTheme.label,
      description: rawTheme.description,
      default: rawTheme.default,
      vars: resolvedVars,
      isLight: isLightFromVars(resolvedVars),
    }
  }

  return registry
}

// Vite glob import of all theme files in the directory
const rawModules = import.meta.glob<unknown>("./*.theme.json", {
  eager: true,
  import: "default",
})

/** All `*.theme.json` files in this directory, keyed by theme id, with `extends` chains resolved. */
export const themeRegistry: Record<string, ThemeDef> = buildRegistry(rawModules)
/** `themeRegistry`'s values as an array, for iteration (e.g. rendering a theme picker). */
export const themeList: ThemeDef[] = Object.values(themeRegistry)
/** Theme id applied when no theme is persisted/selected yet. */
export const DEFAULT_THEME_ID = "mocha"

import { isApiError } from "@/api/client"
import { SEARCH_MODES, type EmbedStatus, type SearchMode } from "@/api/types"

export interface ModeOption {
  mode: SearchMode
  enabled: boolean
  /** Why the mode is off, or a warning while it is on. */
  hint: string | null
}

/** Error codes that mean the embedder cannot serve this query. */
const EMBEDDER_ERRORS = new Set(["not_available", "embedder_unavailable"])

export function isEmbedderError(err: unknown): boolean {
  return isApiError(err) && EMBEDDER_ERRORS.has(err.code)
}

/**
 * Which search modes the toggle offers. BM25 is always on. Vector and hybrid
 * need an embedder: they are off while the status is unknown or failed, when
 * the hub reports `provider: "off"`, or after a search answered that the
 * embedder is unavailable.
 */
export function searchModeOptions(input: {
  status: EmbedStatus | undefined
  statusError?: unknown
  searchError?: unknown
}): ModeOption[] {
  let off: string | null = null
  let warning: string | null = null
  if (input.statusError) off = "Embedder status unavailable; only BM25 search works."
  else if (!input.status) off = "Checking the embedder…"
  else if (input.status.provider === "off") off = "No embedder is configured on the hub. Vector and hybrid search are off."
  else if (isEmbedderError(input.searchError)) off = `Embedder error: ${(input.searchError as Error).message}`
  else if (input.status.last_error) warning = `Last embedder error: ${input.status.last_error}`
  return SEARCH_MODES.map((mode) =>
    mode === "bm25"
      ? { mode, enabled: true, hint: null }
      : { mode, enabled: off === null, hint: off ?? warning },
  )
}

/** The requested mode if the toggle allows it, else BM25. */
export function effectiveMode(requested: SearchMode, options: ModeOption[]): SearchMode {
  return options.find((option) => option.mode === requested)?.enabled ? requested : "bm25"
}

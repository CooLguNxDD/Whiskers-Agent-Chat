import type { ApiErrorBody } from "./types"

let tokenGetter: () => string = () => ""

/** Register the in-memory token reader. Called once from the app shell. */
export function setTokenGetter(getter: () => string) {
  tokenGetter = getter
}

export class ApiError extends Error {
  status: number
  code: string
  details?: Record<string, unknown>

  constructor(status: number, body: ApiErrorBody | null, fallback: string) {
    super(body?.message || fallback)
    this.status = status
    this.code = body?.code || "request_failed"
    this.details = body?.details
  }
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError
}

async function readError(response: Response): Promise<ApiErrorBody | null> {
  try {
    const parsed = (await response.json()) as { error?: ApiErrorBody }
    return parsed.error ?? null
  } catch {
    return null
  }
}

/** JSON fetch against the hub. Sends the bearer token when one is in memory. */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  const token = tokenGetter()
  if (token) headers.set("Authorization", `Bearer ${token}`)
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json")
  const response = await fetch(path, { ...init, headers })
  if (!response.ok) {
    throw new ApiError(response.status, await readError(response), response.statusText)
  }
  return (await response.json()) as T
}
